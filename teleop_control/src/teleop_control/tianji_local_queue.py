"""Durable background finalization for Tianji captures. No hardware connections."""
from __future__ import annotations
from array import array
import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

REQUEST = '.tianji-finalize-request.json'
RESULT = '.tianji-local-save.json'
READY = '.data-platform-ready.json'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False, separators=(',', ':'))
            out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def read_json(path, default=None):
    try: return json.loads(Path(path).read_text())
    except (OSError, ValueError): return default


def configured_root():
    value = os.environ.get('TIANJI_LOCAL_QUEUE_ROOT')
    return Path(value).expanduser().resolve() if value else None


def configure(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    for folder in (root, root/'pending', root/'jobs'):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.mkdir(parents=True, exist_ok=True)
    atomic_json(root/'config.json', dict(version=1, destination=str(destination)))


def destination(root):
    config = read_json(Path(root)/'config.json')
    if not config or config.get('version') != 1:
        raise RuntimeError('本地保存队列未配置')
    target = Path(config['destination'])
    if not target.is_absolute(): raise RuntimeError('本地保存目标必须为绝对路径')
    return target


def require_worker(root):
    state = read_json(Path(root)/'worker-state.json', {})
    try:
        os.kill(int(state['pid']), 0)
        if 0 <= time.time()-state['updated_at'] < 8: return
    except (OSError, KeyError, ValueError, TypeError): pass
    raise RuntimeError('独立本地保存服务未就绪，请重新打开 App；已排队的数据保留')


def queue_status(root, session=''):
    status = read_json(Path(root)/'status.json', {})
    result = dict(pending=0, processing=0, failed=0, saved=0, incomplete=0, error='', **{})
    visible = status.get('sessions', {}).get(session, {}) if session else status
    result.update({k:visible[k] for k in result if k in visible})
    result['scope'] = 'session' if session else 'all'
    result['session_saved'] = status.get('sessions', {}).get(session, {}).get('saved', 0)
    result['destination'] = str(destination(root))
    try: require_worker(root); result['worker_online'] = True
    except RuntimeError: result['worker_online'] = False
    return result


def enqueue(episode, cameras, joints, grippers, before, after, final, stream_start, meta):
    """Commit immutable raw files + row indices before relinquishing the recorder."""
    root = Path(episode.args.background_finalize_root).resolve()
    source = episode.directory.resolve()
    if source.parent != root/'pending': raise RuntimeError('采集缓存不在本地队列目录')
    stores = {}
    for rows in episode.record_stores:
        rows.flush(); os.fsync(rows.writer.fileno())
        relative = str(rows.path.relative_to(source))
        offsets = rows.path.with_suffix(rows.path.suffix+'.offsets')
        with offsets.open('wb') as out:
            rows.offsets.tofile(out); out.flush(); os.fsync(out.fileno())
        stores[relative] = dict(offsets=str(offsets.relative_to(source)), length=len(rows))
    args = {k:v for k,v in vars(episode.args).items()
            if k not in ('on_stream_released','on_frame') and not callable(v)}
    fields = {name:getattr(episode,name) for name in (
        'error','stop_reason','stop_pc_ns','telemetry_gaps','joint_timing_gaps',
        'gripper_integrity','started_monotonic','recording_ended_monotonic')}
    path = lambda rows: str(rows.path.relative_to(source))
    request = dict(version=1, job_id=uuid.uuid4().hex, episode=source.name,
        queued_at_ns=time.time_ns(), destination=str(destination(root)),
        capture_finished_unix_ns=time.time_ns()-time.monotonic_ns()+round((episode.recording_ended_monotonic or time.monotonic())*1e9),
        byteorder=sys.byteorder, stores=stores, args=args, fields=fields,
        trigger=episode.trigger, mapping=episode.mapping, episode_index=episode.episode_index,
        cameras={key:path(rows) for key,rows in cameras.items()}, joints=path(joints),
        grippers={key:path(rows) for key,rows in grippers.items()},
        action_rows=path(episode.action_rows), control_cycles=path(episode.control_cycles),
        before=before, after=after, final=final, stream_start=stream_start, meta=meta,
        annotations=(dict(config=episode.annotations.config,events=episode.annotations.events)
                     if episode.annotations else None), info=episode.args.background_finalize_info)
    # Open writers are closed immediately after return; no recorder writes after this commit.
    meta.update(status='queued', complete=False, local_save_queued=True)
    atomic_json(source/'meta_info.json',meta)
    atomic_json(source/REQUEST,request)
    episode.background_queued = True
    episode.notice('已提交独立后台保存队列；可继续采集下一条，退出 App 不会中断队列。')


def _relative(base, relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts: raise ValueError('Invalid queue-relative path')
    candidate = base/path
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(base.resolve()):
        raise ValueError('Queue source escapes cache')
    return candidate


def _restore_rows(source, attempt, relative, descriptor):
    from FusionDataCollector.collector import RecordRows
    raw = _relative(source,relative)
    target = _relative(attempt,relative)
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(raw,target)
    rows = RecordRows.__new__(RecordRows)
    rows.path = target
    rows.writer = target.open('ab')
    rows.reader = target.open('rb')
    rows.offsets = array('Q')
    try:
        with _relative(source,descriptor['offsets']).open('rb') as offsets:
            rows.offsets.fromfile(offsets,descriptor['length'])
        if any(offset>=target.stat().st_size for offset in rows.offsets):
            raise ValueError('Saved record offset outside raw data')
    except BaseException:
        rows.close(); raise
    rows.last_index=rows.last_data=None
    return rows


def _copy_raw(source,attempt):
    for path in source.rglob('*'):
        relative=path.relative_to(source)
        if any(part.startswith('.') for part in relative.parts): continue
        if path.is_symlink(): raise ValueError('Unexpected symlink in capture cache')
        target=attempt/relative
        if path.is_dir(): target.mkdir(parents=True,exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True,exist_ok=True)
            try: os.link(path,target)
            except OSError as exc:
                if exc.errno != errno.EXDEV: raise
                shutil.copyfile(path,target)
    # Metadata will be rewritten; it must never share its inode with the raw checkpoint.
    meta=attempt/'meta_info.json'
    meta.unlink(missing_ok=True)


def _finish_attempt(source,attempt,request):
    collector_root = str(Path(__file__).resolve().parents[3] / 'fusion_data_collection')
    if collector_root not in sys.path:
        sys.path.insert(0, collector_root)
    from FusionDataCollector.collector import Episode, RecordRows
    args=SimpleNamespace(**request['args'])
    ep=Episode(args,request['mapping'],request['trigger'],episode_index=request['episode_index'])
    ep.directory=attempt
    for key,value in request['fields'].items(): setattr(ep,key,value)
    loaded={}
    try:
        for relative,descriptor in request['stores'].items():
            loaded[relative]=_restore_rows(source,attempt,relative,descriptor)
        ep.record_stores=list(loaded.values())
        ep.action_rows=loaded[request['action_rows']]
        ep.control_cycles=loaded[request['control_cycles']]
        if request['annotations']:
            from .subtask_annotations import AnnotationCycle
            ep.annotations=AnnotationCycle(request['annotations']['config'])
            ep.annotations.events=request['annotations']['events']
        def store(name,initial=()):
            rows=RecordRows(attempt/'record'/('.working-'+name+'.jsonl'))
            ep.record_stores.append(rows)
            for row in initial: rows.append(row)
            return rows
        ep._finalize({k:loaded[v] for k,v in request['cameras'].items()},
            loaded[request['joints']],{k:loaded[v] for k,v in request['grippers'].items()},
            request['before'],request['after'],request['final'],request['stream_start'],
            dict(request['meta']),store)
        metadata=read_json(attempt/'meta_info.json')
        info=request['info']
        for key in ('author','teleop_session_id','upload_sides','upload_camera_policy','upload_destination'):
            if key in info: metadata[key]=info[key]
        metadata.pop('local_save_queued',None)
        metadata['pc_stop_ns']=request['capture_finished_unix_ns']
        metadata['local_finalized_unix_ns']=time.time_ns()
        atomic_json(attempt/'meta_info.json',metadata)
        return metadata
    finally:
        for rows in list({id(row):row for row in [*loaded.values(),*ep.record_stores]}.values()):
            rows.close(remove=True)


def publish_noreplace(source,target):
    # Linux renameat2 makes the no-overwrite promise atomic, including concurrent user writes.
    libc=ctypes.CDLL(None,use_errno=True)
    rename=libc.renameat2
    rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    rename.restype=ctypes.c_int
    if rename(-100,os.fsencode(source),-100,os.fsencode(target),1):
        code=ctypes.get_errno()
        raise OSError(code,os.strerror(code),str(target))


def process_request(root,source,request):
    with (root / 'job.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (source/REQUEST).exists():
            return None
        return _process_request(root, source, request)


def _process_request(root,source,request):
    """Retry from immutable inputs; publish one directory atomically, never overwrite."""
    if request.get('version') != 1 or request.get('episode') != source.name:
        raise ValueError('Unsupported or mismatched finalization checkpoint')
    if request['byteorder']!=sys.byteorder: raise ValueError('Record index byte order mismatch')
    target_root=Path(request['destination'])
    if not target_root.is_absolute(): raise ValueError('Invalid destination')
    target_root.mkdir(parents=True,exist_ok=True)
    target=_relative(target_root, request.get('output_relative', source.name))
    target_root=target.parent
    request_hash=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
    expected=dict(job_id=request['job_id'],request_sha256=request_hash)
    attempt=target_root/('.tianji-incoming-'+source.name+'-'+request['job_id'])
    target_root.mkdir(parents=True,exist_ok=True)
    marker=read_json(target/RESULT,{})
    if target.exists():
        if any(marker.get(k)!=v for k,v in expected.items()):
            raise RuntimeError('目标目录已存在且不是本任务，保留两边数据：'+str(target))
        metadata=read_json(target/'meta_info.json')
        if not metadata: raise RuntimeError('已发布目录缺少元数据')
    else:
        if attempt.exists():
            if read_json(attempt/'.queue-owner.json')!=expected:
                raise RuntimeError('临时输出目录归属不符，已保留')
            shutil.rmtree(attempt)
        attempt.mkdir(mode=0o700)
        atomic_json(attempt/'.queue-owner.json',expected)
        _copy_raw(source,attempt)
        metadata=_finish_attempt(source,attempt,request)
        atomic_json(attempt/RESULT,dict(expected,finished_at_ns=time.time_ns(),complete=metadata['complete']))
        # Persist completed files before publishing the directory.
        for file in attempt.rglob('*'):
            if file.is_file():
                with file.open('rb') as handle: os.fsync(handle.fileno())
        for folder in sorted((p for p in attempt.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True): sync_dir(folder)
        sync_dir(attempt)
        if target.exists(): raise RuntimeError('发布前目标目录已存在，拒绝覆盖')
        publish_noreplace(attempt,target)
        sync_dir(target_root)
    # Platform uploads see only published, valid episodes, never an in-progress folder.
    if metadata.get('complete') and metadata.get('actions_recorded'):
        atomic_json(target/READY,request['info'])
    result=dict(episode=source.name,job_id=request['job_id'],
        session=request['info'].get('teleop_session_id',''),
        state='saved' if metadata.get('complete') else 'incomplete',
        error=metadata.get('error') or '',destination=str(target),queued_at_ns=request['queued_at_ns'],
        finished_at_ns=time.time_ns())
    atomic_json(root/'jobs'/(source.name+'.json'),result)
    # Only remove this immutable cache after the complete output is durably published.
    shutil.rmtree(source)
    sync_dir(root/'pending')
    return result


def refresh_status(root):
    rows=[read_json(p,{}) for p in (root/'jobs').glob('*.json')]
    known={row.get('episode') for row in rows}
    for file in (root/'pending').glob('*/'+REQUEST):
        if file.parent.name not in known:
            request=read_json(file,{})
            rows.append(dict(episode=file.parent.name,state='pending',session=request.get('info',{}).get('teleop_session_id','')))
    status=dict(pending=0,processing=0,failed=0,saved=0,incomplete=0,error='',sessions={})
    for row in rows:
        state=row.get('state')
        if state not in ('pending','processing','failed','saved','incomplete'): continue
        status[state]+=1
        session=status['sessions'].setdefault(row.get('session',''),dict(pending=0,processing=0,failed=0,saved=0,incomplete=0,error=''))
        session[state]+=1
        if row.get('error') and state in ('failed','incomplete'): session['error']=row['error']
        if row.get('error') and state in ('failed','incomplete'): status['error']=row['error']
    status['updated_at']=time.time()
    atomic_json(root/'status.json',status)
    return status


def scan_once(root,stop=None):
    requests=[]
    for file in (root/'pending').glob('*/'+REQUEST):
        request=read_json(file)
        if (not isinstance(request,dict) or not isinstance(request.get('queued_at_ns'),int)
                or not isinstance(request.get('info'),dict)):
            atomic_json(root/'jobs'/(file.parent.name+'.json'),dict(episode=file.parent.name,state='failed',error='排队记录损坏，保留缓存'))
            continue
        requests.append((request.get('queued_at_ns',0),file.parent,request))
    for _,source,request in sorted(requests,key=lambda item:item[0]):
        if stop is not None and stop.is_set(): break
        if not (source/REQUEST).exists(): continue
        previous=read_json(root/'jobs'/(source.name+'.json'),{})
        if previous.get('retry_at',0)>time.time(): break  # A newer capture must not overtake the FIFO head.
        row=dict(episode=source.name,session=request.get('info',{}).get('teleop_session_id',''),
            state='processing',error='',queued_at_ns=request['queued_at_ns'],attempts=previous.get('attempts',0)+1)
        atomic_json(root/'jobs'/(source.name+'.json'),row); refresh_status(root)
        try:
            process_request(root,source,request)
        except Exception as exc:
            logging.exception('Local finalization failed for %s',source.name)
            row.update(state='failed',error=str(exc),retry_at=time.time()+min(300,5*2**min(row['attempts'],6)))
            atomic_json(root/'jobs'/(source.name+'.json'),row)
            refresh_status(root)
            break  # Retain good raw input and retry it before later jobs.
        refresh_status(root)
    return refresh_status(root)


def run_worker(root):
    root=Path(root).resolve(); root.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.umask(0o077)
    # Import the passive collector; never create managers, SDKs, sockets or camera clients.
    sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'fusion_data_collection'))
    with (root/'worker.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
        logging.basicConfig(level=logging.INFO,handlers=[RotatingFileHandler(root/'worker.log',maxBytes=2*1024**2,backupCount=2)])
        stop=threading.Event()
        for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,lambda *_:stop.set())
        def heartbeat():
            while not stop.is_set():
                try:
                    atomic_json(root/'worker-state.json',dict(pid=os.getpid(),updated_at=time.time()))
                    refresh_status(root)
                except Exception:
                    logging.exception('Unable to refresh local queue status')
                stop.wait(1)
        thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
        try:
            while not stop.is_set():
                scan_once(root,stop);stop.wait(.5)
        finally:
            stop.set();thread.join(timeout=2)
            (root/'worker-state.json').unlink(missing_ok=True)


def ensure_service(root,*,python=None,source=None):
    root=Path(root).resolve()
    source=Path(source or Path(__file__).resolve().parents[1])
    python=str(python or sys.executable)
    from .background_upload import unit_quote
    name='xiaokai-tianji-save-'+hashlib.sha256(str(root).encode()).hexdigest()[:12]+'.service'
    unit_path=Path.home()/'.config/systemd/user'/name
    unit_path.parent.mkdir(parents=True,exist_ok=True)
    implementation=hashlib.sha256(Path(__file__).read_bytes()+(source.parent.parent/'fusion_data_collection/FusionDataCollector/collector.py').read_bytes()).hexdigest()
    command=[python,'-u','-m','teleop_control.tianji_local_queue','worker','--root',str(root)]
    unit=('# Package implementation: '+implementation+'\n[Unit]\nDescription=Tianji durable local save queue\n'
          '[Service]\nType=simple\nExecStart='+' '.join(map(unit_quote,command))+'\n'
          'Environment='+unit_quote('PYTHONPATH='+str(source))+'\n'
          'UnsetEnvironment=DATA_COLLECTION_GATE_FILE\nRestart=always\nRestartSec=2\n'
          'UMask=0077\nNice=10\nIOSchedulingClass=idle\nTimeoutStopSec=20\nKillMode=control-group\n'
          '[Install]\nWantedBy=default.target\n')
    changed=not unit_path.exists() or unit_path.read_text()!=unit
    if changed: unit_path.write_text(unit)
    try:
        active=subprocess.run(['systemctl','--user','is-active','--quiet',name],capture_output=True,timeout=10).returncode==0
        subprocess.run(['systemctl','--user','daemon-reload'],check=True,capture_output=True,timeout=15)
        subprocess.run(['systemctl','--user','enable','--now',name],check=True,capture_output=True,timeout=20)
        if changed and active:
            subprocess.run(['systemctl','--user','restart',name],check=True,capture_output=True,timeout=30)
    except (OSError,subprocess.SubprocessError) as exc:
        raise RuntimeError('无法启动独立本地保存服务，未启动采集；请检查用户 systemd 服务') from exc
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        try: require_worker(root);return name
        except RuntimeError: time.sleep(.1)
    require_worker(root)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['worker'])
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args()
    run_worker(args.root)
