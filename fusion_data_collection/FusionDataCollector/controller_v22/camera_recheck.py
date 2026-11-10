"""Fresh, explicit camera recovery. Python 2.7; never controls the robot."""
from __future__ import print_function
import glob
import json
import os
import signal
import subprocess
import sys
import time



def remember_failure(service):
    path=os.path.join(service.runtime,'camera-failure-context.json')
    if not service.fault:
        try:os.unlink(path)
        except OSError:pass
        return
    record={'fault':service.fault,'last_stream':service.last_stream}
    with open(path+'.new','w') as f:json.dump(record,f)
    os.chmod(path+'.new',0o600);os.rename(path+'.new',path)


def restore_failure(service):
    path=os.path.join(service.runtime,'camera-failure-context.json')
    try:
        if os.path.getsize(path)>32768:return
        with open(path) as f:record=json.load(f)
        if isinstance(record.get('fault'),(str,type(u''))) and isinstance(record.get('last_stream'),dict):
            service.fault=record['fault'];service.last_stream=record['last_stream']
    except (IOError,OSError,ValueError,TypeError):pass


def live_capture(api):
    if api.ACTIVE is None:
        return False
    if api.ACTIVE.poll() is None:
        return True
    api.ACTIVE = None
    return False


def cleanup_orphans(service, manifest):
    """Only terminate verified camera helpers orphaned by a dead service."""
    from streaming import sha
    root = os.path.realpath(os.path.dirname(service.config['helper']))
    allowed = {os.path.realpath(p):digest for p,digest in manifest['sha256'].items()
               if os.path.dirname(os.path.realpath(p)) == root
               and os.path.basename(p) in ('capture','capture-three','capture-three-teleop')}
    cleaned = []
    for proc in glob.glob('/proc/[0-9]*'):
        try:
            exe = os.path.realpath(os.readlink(proc+'/exe'))
        except OSError:
            continue  # Kernel threads and already exited processes have no exe.
        if exe not in allowed:
            continue
        try:
            pid = int(proc.rsplit('/',1)[-1])
            stat = open(proc+'/stat').read().rsplit(')',1)[1].split()
            if int(stat[1]) != 1 or os.getpgid(pid) != pid:
                raise RuntimeError('Camera helper is owned by another active session')
            if sha(exe) != allowed[exe]:
                raise RuntimeError('Camera helper integrity mismatch')
            os.killpg(pid,signal.SIGTERM)
            deadline=time.time()+1.0
            while os.path.exists(proc) and time.time()<deadline:time.sleep(.05)
            if os.path.exists(proc):
                os.killpg(pid,signal.SIGKILL)
                deadline=time.time()+1.0
                while os.path.exists(proc) and time.time()<deadline:time.sleep(.05)
            if os.path.exists(proc):raise RuntimeError('Camera helper has not released USB resources')
            cleaned.append(pid)
        except OSError:
            if os.path.exists(proc):raise
    return cleaned



def needs_usb_recovery(service):
    return bool(service.fault and (service.last_stream or {}).get('native_failure_reason') in (
        'Camera did not produce first frame after bounded startup retry; stop',
        'Camera has no new frame for 2 seconds; stop',
        'Camera open/start or negotiation failed'))


def controller_tasks_unchanged(before,after):
    from streaming import rt_snapshot
    if any(before.get(k)!=after.get(k) for k in ('robot_pids','modules','mounts','config_hashes')):return False
    old,new=rt_snapshot(before['xenomai']),rt_snapshot(after['xenomai'])
    for name in old:
        a,b=old[name],new[name]
        if a[:2]!=b[:2] or a[4]!=b[4] or b[2]<a[2] or b[3]<a[3]:return False
        if name=='FX_RtTask' and a[2]!=b[2]:return False
    return True


def recover_usb(service, api):
    helper=os.path.join(os.path.dirname(service.config['helper']),'camera_usb_recovery.py')
    output=open(os.path.join(service.runtime,'last-usb-recovery.json'),'w+')
    try:
        process=subprocess.Popen([sys.executable,'-B',helper],stdout=output,stderr=subprocess.STDOUT,preexec_fn=os.setsid)
        deadline=api.monotonic()+14
        while process.poll() is None and api.monotonic()<deadline:time.sleep(.05)
        if process.poll() is None:
            process.terminate()  # The worker's finally restores the driver binding.
            deadline=api.monotonic()+3
            while process.poll() is None and api.monotonic()<deadline:time.sleep(.05)
            raise RuntimeError('Camera USB recovery has not completed; retry after cleanup')
        output.seek(0)
        try:result=json.loads(output.read())
        except ValueError:raise RuntimeError('Camera USB recovery returned an invalid result')
        if process.returncode or not result.get('ok'):
            raise RuntimeError('Camera USB recovery refused: '+str(result.get('error','unknown error')))
        return result
    finally:output.close()


def inventory(service, api):
    result=[]
    for camera in service.config['cameras']:
        info=api.device_info(camera)
        if info['available']:
            if not api.interface_idle(camera):
                raise RuntimeError('Camera USB interface is still active: '+camera['id'])
            node='/dev/bus/usb/%03d/%03d' % (info['bus'],info['address'])
            with open(os.devnull,'wb') as null:
                state=subprocess.call(['/bin/fuser',node],stdout=null,stderr=null)
            if state != 1:raise RuntimeError('Camera USB device is busy: '+camera['id'])
        result.append(dict(id=camera['id'],available=info['available'],bus=info['bus'],address=info['address']))
    return result


def archive(service, record):
    path=os.path.join(service.runtime,'camera-recovery-history.jsonl')
    if os.path.exists(path) and os.path.getsize(path)>1024*1024:
        os.rename(path,path+'.previous')
    with open(path,'a') as handle:
        handle.write(json.dumps(record,separators=(',',':'))+'\n')


def recheck(service, api, manual=False):
    """An old failed audit is evidence, not a permanent veto on a new check."""
    if manual is not True:return {'ok':False,'error':'manual_camera_check_required'}
    if live_capture(api):return {'ok':False,'error':'camera_capture_active'}
    from streaming import audit, unchanged_for_recovery, sha
    record={'time':time.time(),'previous_fault':service.fault,
            'previous_stream':service.last_stream,'policy':'fresh-camera-check-v1'}
    try:
        with open(os.path.join(os.path.dirname(service.config['helper']),'stream-manifest.json')) as f:
            manifest=json.load(f)
        for path,digest in manifest['sha256'].items():
            if sha(path)!=digest:raise RuntimeError('Camera program integrity mismatch: '+os.path.basename(path))
        record['orphan_helpers_cleaned']=cleanup_orphans(service,manifest)
        cooldown=max(0,service.config['cooldown_seconds']-(api.monotonic()-service.last_finish))
        if cooldown>3:raise RuntimeError('Camera cleanup is still in progress')
        if cooldown:time.sleep(cooldown)
        # Compare CURRENT state across a bounded observation interval. Historical
        # xHCI errors remain in logs, but cannot permanently poison this baseline.
        before=audit()
        if needs_usb_recovery(service):
            inventory(service,api)
            record['before_usb_recovery']=before
            record['usb_controller_recovery']=recover_usb(service,api)
            after_usb=audit()
            if not controller_tasks_unchanged(before,after_usb):
                raise RuntimeError('Controller tasks changed during camera USB recovery')
            before=after_usb
        first=inventory(service,api)
        time.sleep(1.0)
        after=audit();second=inventory(service,api)
        if first!=second:raise RuntimeError('Camera USB enumeration is still changing; retry check')
        if not unchanged_for_recovery(before,after,'teleop_observation',camera_hotplug=True):
            raise RuntimeError('Controller state is changing during camera check')
        record.update(ok=True,before=before,after=after,cameras=second)
        archive(service,record)
    except Exception as exc:
        record.update(ok=False,error=str(exc))
        try:archive(service,record)
        except (IOError,OSError):pass
        return {'ok':False,'error':'camera_check_failed','detail':str(exc)}
    service.fault=None
    service.recovery_audit=None
    remember_failure(service)
    service.recovery_count=getattr(service,'recovery_count',0)+1
    service.last_manual_recheck={'time':record['time'],'cameras':second,'recovery_count':service.recovery_count}
    return {'ok':True,'recovered':True,'verified_stream':False,
            'recovery_count':service.recovery_count,'cameras':second,
            'usb_controller_reinitialized':bool(record.get('usb_controller_recovery'))}
