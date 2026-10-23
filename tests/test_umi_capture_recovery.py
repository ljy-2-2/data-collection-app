"""Real MCAP crash recovery, durable discard, watchdog and upload boundaries."""
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time

import httpx
import pytest
from mcap.writer import Writer, CompressionType
from mcap.reader import make_reader

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT/'umi_collection/vendor/galaxis/galaxis-core/galaxis-core'
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT/'teleop_control/src'))
from galaxis_core.capture_recovery import (begin_capture, interrupt_capture, update_journal,
                                         pending_recoveries, recover_episode, complete_messages)
from galaxis_core.mp4_worker import BackgroundFinalizer, atomic_json
from galaxis_core.recording_priority import CapturePriority
from teleop_control.umi_uploader import UmiRosbagUploader


def capture(tmp_path, *, missing=False, damage=None):
    folder = tmp_path/'task'/'round'
    journal = begin_capture(tmp_path, folder, {
        'uuid': 'capture-original-uuid', 'author': 'original-operator',
        'upload_destination': {'api_url': 'https://platform.test'},
        'episode': {'label': 'task', 'start_time': 100., 'end_time': 999.},
        'segments': [{'start_time': 100., 'end_time': 999., 'success': True}]}, ['/camera', '/hand'])
    folder.mkdir(parents=True)
    source = folder/'source.mcap'
    with source.open('wb') as stream:
        writer = Writer(stream, compression=CompressionType.NONE, chunk_size=1)
        writer.start(profile='ros2')
        schema = writer.register_schema('sensor_msgs/msg/JointState', 'ros2msg', b'test schema')
        camera = writer.register_channel('/camera', 'cdr', schema)
        hand = writer.register_channel('/hand', 'cdr', schema)
        for i in range(8):
            writer.add_message(camera, (100+i)*10**9, b'frame'+bytes([i]), (100+i)*10**9, i)
            if i <= 4 and not missing:
                writer.add_message(hand, (100+i)*10**9, b'pose'+bytes([i]), (100+i)*10**9, i)
        writer.finish()
    data = bytearray(source.read_bytes())
    if damage:
        chunks = []
        offset = 8
        while offset + 9 < len(data):
            opcode, length = struct.unpack('<BQ', data[offset:offset+9])
            if opcode == 6: chunks.append((offset, length))
            offset += 9 + length
        last, size = chunks[-1]
        if damage == 'tail': data = data[:last+9+size//2]
        if damage == 'crc': data[last+9+24] ^= 1
        if damage == 'footer': data = data[:last+9+size]
        source.write_bytes(data)
    return journal, folder, source


@pytest.mark.parametrize('damage', [None, 'tail', 'crc', 'footer'])
def test_recovery_uses_last_common_sample_and_preserves_original(tmp_path, damage):
    journal, folder, source = capture(tmp_path, damage=damage)
    original = source.read_bytes()
    interrupt_capture(journal, 'camera_disconnected', detected_ns=110*10**9)
    recover_episode(journal)
    meta = json.loads((folder/'meta.json').read_text())
    assert meta['episode']['start_time'] == 100
    assert meta['episode']['end_time'] == 104  # not detection at 110 or restart time
    assert meta['episode']['success'] is False
    import yaml
    info = yaml.safe_load((folder/'metadata.yaml').read_text())['rosbag2_bagfile_information']
    assert info['version'] == 9 and 'ros_distro' in info
    assert all('type_description_hash' in row['topic_metadata'] for row in info['topics_with_message_count'])
    assert meta['author'] == 'original-operator'
    assert meta['uuid'] == 'capture-original-uuid'
    assert meta['segments'][0]['end_time'] == 104
    assert meta['interruption']['reason'] == 'camera_disconnected'
    assert source.read_bytes() == original
    with (folder/'recovered.mcap').open('rb') as stream:
        rows = list(make_reader(stream, validate_crcs=True).iter_messages())
    assert len(rows) == 10 and max(m.log_time for _,_,m in rows) == 104*10**9
    before = (folder/'recovered.mcap').read_bytes()
    recover_episode(journal)
    assert (folder/'recovered.mcap').read_bytes() == before
    assert not list(pending_recoveries(tmp_path))
    assert not UmiRosbagUploader._processing(folder)


def test_missing_stream_is_reported_and_existing_data_is_still_saved(tmp_path):
    journal, folder, _ = capture(tmp_path, missing=True)
    recover_episode(journal)
    meta = json.loads((folder/'meta.json').read_text())
    assert meta['interruption']['missing_topics'] == ['/hand']
    assert meta['episode']['end_time'] == 107
    assert not meta['episode']['success']


def test_detection_caps_late_flushed_samples(tmp_path):
    journal, folder, _ = capture(tmp_path)
    interrupt_capture(journal, 'desktop_lost', 102*10**9)
    recover_episode(journal)
    assert json.loads((folder/'meta.json').read_text())['episode']['end_time'] == 102


def test_discard_survives_crash_before_deletion_and_cannot_be_reopened(tmp_path):
    journal, folder, source = capture(tmp_path)
    update_journal(journal, state='discarded')
    (folder/'.discarded').touch()
    interrupt_capture(journal, 'late_parent_death')
    recover_episode(journal)
    assert json.loads(journal.read_text())['state'] == 'discarded'
    assert not list(pending_recoveries(tmp_path))
    assert source.exists() and not (folder/'meta.json').exists()
    assert UmiRosbagUploader._processing(folder)


def test_empty_or_damaged_capture_is_not_fabricated(tmp_path):
    journal, folder, source = capture(tmp_path)
    source.write_bytes(b'not an MCAP')
    with pytest.raises(ValueError, match='No complete'):
        recover_episode(journal)
    assert not (folder/'meta.json').exists()
    assert UmiRosbagUploader._processing(folder)
    assert source.read_bytes() == b'not an MCAP'


def test_partial_publication_retries_and_upload_barrier_stays(tmp_path, monkeypatch):
    journal, folder, _ = capture(tmp_path)
    import galaxis_core.capture_recovery as recovery
    original = recovery.atomic_json
    def fail(path, value):
        if Path(path).name == 'meta.json': raise OSError('simulated power loss')
        return original(path, value)
    monkeypatch.setattr(recovery, 'atomic_json', fail)
    with pytest.raises(OSError): recover_episode(journal)
    assert UmiRosbagUploader._processing(folder)
    monkeypatch.setattr(recovery, 'atomic_json', original)
    recover_episode(journal)
    assert not UmiRosbagUploader._processing(folder)


def test_worker_never_recovers_a_live_capture(tmp_path):
    journal, folder, _ = capture(tmp_path)
    priority = CapturePriority(tmp_path); priority.acquire()
    worker = BackgroundFinalizer(tmp_path)
    try:
        worker.tick()
        assert worker.process is None and not (folder/'meta.json').exists()
    finally:
        priority.release()


def test_background_worker_repairs_orphan_after_restart(tmp_path, monkeypatch):
    journal, folder, _ = capture(tmp_path, damage='tail')
    monkeypatch.setenv('PYTHONPATH', str(CORE) + os.pathsep + os.environ.get('PYTHONPATH', ''))
    worker = BackgroundFinalizer(tmp_path)
    try:
        deadline = time.monotonic()+10
        while not (folder/'meta.json').exists() or worker.process is not None:
            worker.tick()
            assert time.monotonic() < deadline, (folder/'.mp4-finalize.log').read_text()
            time.sleep(.03)
        assert json.loads(journal.read_text())['state'] == 'recovered'
    finally:
        worker._stop_conversion()


@pytest.mark.parametrize('local_raw', [False, True])
def test_guard_flushes_when_desktop_lease_disappears(tmp_path, local_raw):
    journal, folder, _ = capture(tmp_path)
    lease = tmp_path/'lease.json'
    atomic_json(lease, {'available': True, 'checked_at': time.time()})
    update_journal(journal, lease_file=str(lease))
    if local_raw:
        value = json.loads(journal.read_text())
        update_journal(journal, metadata={**value['metadata'], 'recording_format': 'umi-mcap-h264-v1'})
    ready, flushed = tmp_path/'ready', tmp_path/'flushed'
    script = f'''import signal,time,pathlib
signal.signal(signal.SIGINT, lambda *_: (pathlib.Path({str(flushed)!r}).write_text('flushed'), exit(0)))
pathlib.Path({str(ready)!r}).touch()
while True: time.sleep(.1)
'''
    guard = subprocess.Popen([sys.executable, '-m', 'galaxis_core.capture_guard',
        '--journal', str(journal), '--parent-pid', str(os.getpid()), '--', sys.executable, '-c', script],
        env={**os.environ, 'PYTHONPATH': str(CORE)+os.pathsep+os.environ.get('PYTHONPATH','')})
    try:
        deadline=time.monotonic()+5
        while not ready.exists():
            assert time.monotonic()<deadline; time.sleep(.02)
        atomic_json(lease, {'available': False, 'checked_at': time.time()})
        if local_raw:
            time.sleep(.6)
            assert guard.poll() is None  # Platform outage cannot stop local raw recording.
            atomic_json(lease, {'available': False, 'checked_at': time.time()-20})
        assert guard.wait(timeout=5) == 0
        assert flushed.read_text() == 'flushed'
        assert json.loads(journal.read_text())['interruption']['reason'] == (
            'desktop_heartbeat_lost' if local_raw else 'platform_connection_lost')
    finally:
        if guard.poll() is None: guard.kill(); guard.wait()


def test_recovered_episode_retries_offline_then_uploads_once_with_real_end(tmp_path):
    root = tmp_path/'rosbags'; root.mkdir()
    journal, folder, _ = capture(root)
    recover_episode(journal)
    offline = [True]; registered=[]; uploads=[]
    def handle(request):
        if offline[0]: raise httpx.ConnectError('offline', request=request)
        if request.url.path == '/uploads/presign':
            names=json.loads(request.content)['filenames']
            return httpx.Response(200,json={'urls':[{'filename':name,'put_url':f'https://storage.test/{name}','key':f'raw/{name}'} for name in names]})
        if request.url.host == 'storage.test':
            uploads.append(request.url.path);return httpx.Response(200)
        if request.url.path == '/episodes' and request.method == 'POST':
            registered.append(json.loads(request.content));return httpx.Response(201,json={'id':registered[-1]['id']})
        if request.url.path.startswith('/episodes/import-batches/'):
            return httpx.Response(200,json={})
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        uploader=UmiRosbagUploader(tmp_path, 'https://platform.test', client=client)
        uploader.scan_once()
        assert not registered
        offline[0]=False
        uploader.scan_once(); uploader.scan_once()
    assert len(registered)==1
    assert registered[0]['duration_s']==4
    assert registered[0]['author']=='original-operator'
    assert registered[0]['extra']['interruption']['recovered']
    assert set(uploads)=={'/meta.json','/metadata.yaml','/recovered.mcap'}


def test_parent_crash_flushes_writer_and_holds_lock_until_writer_exits(tmp_path):
    journal, folder, _ = capture(tmp_path)
    ready, flushed = tmp_path/'ready', tmp_path/'flushed'
    child = f'''import signal,time,pathlib
signal.signal(signal.SIGINT, lambda *_: (time.sleep(.4), pathlib.Path({str(flushed)!r}).touch(), exit(0)))
pathlib.Path({str(ready)!r}).touch()
while True: time.sleep(.1)
'''
    parent_code = f'''import os,sys,subprocess,time
from galaxis_core.recording_priority import CapturePriority
priority=CapturePriority({str(tmp_path)!r});priority.acquire()
fds=(priority.intent.fileno(),priority.resources.fileno())
subprocess.Popen([sys.executable,'-m','galaxis_core.capture_guard','--journal',{str(journal)!r},'--parent-pid',str(os.getpid()),'--',sys.executable,'-c',{child!r}],pass_fds=fds,env={{**os.environ,'GALAXIS_CAPTURE_FDS':','.join(map(str,fds))}})
while True: time.sleep(.1)
'''
    parent = subprocess.Popen([sys.executable,'-c',parent_code],
        env={**os.environ,'PYTHONPATH':str(CORE)+os.pathsep+os.environ.get('PYTHONPATH','')})
    from galaxis_core.recording_priority import open_lock, try_lock
    try:
        deadline=time.monotonic()+5
        while not ready.exists():
            assert time.monotonic()<deadline;time.sleep(.01)
        parent.kill();parent.wait()
        with open_lock(tmp_path,'.capture-intent.lock') as lock:
            assert not try_lock(lock), 'orphan writer released lock before flush'
        while not flushed.exists():
            assert time.monotonic()<deadline;time.sleep(.02)
        with open_lock(tmp_path,'.capture-intent.lock') as lock:
            while not try_lock(lock):
                assert time.monotonic()<deadline;time.sleep(.01)
        assert json.loads(journal.read_text())['interruption']['reason']=='record_manager_exited'
    finally:
        if parent.poll() is None:parent.kill();parent.wait()


def test_encoder_quality_failure_uploads_flagged_raw_data(tmp_path):
    journal, folder, source = capture(tmp_path)
    # This synthetic bag has no valid JPEG camera stream: MP4 validation fails.
    update_journal(journal, state='saved')
    meta = json.loads(journal.read_text())['metadata']
    atomic_json(folder/'.mp4-pending.json', meta)
    (folder/'metadata.yaml').write_text('rosbag2_bagfile_information:\n  relative_file_paths: [source.mcap]\n')
    run = subprocess.run([sys.executable, '-m', 'galaxis_core.mp4_worker', '--episode', str(folder)],
        env={**os.environ,'PYTHONPATH':str(CORE)+os.pathsep+os.environ.get('PYTHONPATH','')},
        capture_output=True,text=True,timeout=15)
    assert run.returncode==0, run.stderr
    assert source.exists() and not UmiRosbagUploader._processing(folder)
    result=json.loads((folder/'meta.json').read_text())
    assert result['recording_format']=='rosbag2-mcap'
    assert result['interruption']['reason'].startswith('video_finalization_failed')


def test_real_signal_gap_preserves_prefix_instead_of_bridging_missing_actions(tmp_path):
    folder=tmp_path/'task/round'
    journal=begin_capture(tmp_path,folder,{'uuid':'gap-test','author':'operator','episode':{},'segments':[]},['/galaxis/gripper/left/joint_state'])
    folder.mkdir(parents=True)
    source=folder/'original.mcap'
    with source.open('wb') as stream:
        writer=Writer(stream,compression=CompressionType.NONE);writer.start(profile='ros2')
        schema=writer.register_schema('sensor_msgs/msg/JointState','ros2msg',b'test')
        channel=writer.register_channel('/galaxis/gripper/left/joint_state','cdr',schema)
        for offset in (0,10_000_000,20_000_000,250_000_000,260_000_000):
            data=b'\x00\x01\x00\x00'+struct.pack('<iI',100,offset)
            writer.add_message(channel,100_000_000_000+offset,data,100_000_000_000+offset)
        writer.finish()
    original=source.read_bytes()
    interrupt_capture(journal,'sensor_gap');recover_episode(journal)
    meta=json.loads((folder/'meta.json').read_text())
    assert meta['episode']['end_time']==100.02
    assert meta['interruption']['signal_timestamp_gaps'][0]['gap_ns']==230_000_000
    with (folder/'recovered.mcap').open('rb') as stream:
        assert len(list(make_reader(stream).iter_messages()))==3
    assert source.read_bytes()==original


@pytest.mark.parametrize('topic', ['/joint_states', '/pico/hmd/state',
    '/left_camera/image_raw/compressed', '/center_camera/image_raw/compressed',
    '/right_camera/image_raw/compressed'])
def test_manual_save_checks_source_gaps_before_encoding_or_cleanup(tmp_path, topic):
    from galaxis_core.mp4_worker import finalize_episode
    folder=tmp_path/'round';folder.mkdir()
    atomic_json(folder/'.mp4-pending.json', {'episode': {}})
    source=folder/'original.mcap'
    with source.open('wb') as stream:
        writer=Writer(stream,compression=CompressionType.NONE);writer.start(profile='ros2')
        channel=writer.register_channel(topic,'cdr',0)
        for offset in (0,10_000_000,120_000_000):
            payload=b'\x00\x01\x00\x00'+struct.pack('<iI',100,offset)
            writer.add_message(channel,100_000_000_000+offset,payload,100_000_000_000+offset)
        writer.finish()
    before=source.read_bytes()
    with pytest.raises(ValueError, match='源数据连续性不合格'):
        finalize_episode(folder)
    assert source.read_bytes()==before
    assert not (folder/'meta.json').exists()
    assert (folder/'.mp4-pending.json').exists()


def test_delayed_arrival_with_continuous_source_time_is_not_data_loss(tmp_path):
    from galaxis_core.capture_recovery import validate_recorded_continuity
    with (tmp_path/'original.mcap').open('wb') as stream:
        writer=Writer(stream,compression=CompressionType.NONE);writer.start(profile='ros2')
        channel=writer.register_channel('/joint_states','cdr',0)
        for i,arrival in enumerate((0,200_000_000,201_000_000)):
            payload=b'\x00\x01\x00\x00'+struct.pack('<iI',100,i*10_000_000)
            writer.add_message(channel,100_000_000_000+arrival,payload,100_000_000_000+arrival)
        writer.finish()
    validate_recorded_continuity(tmp_path)


def test_recovery_retains_latched_calibration_before_live_stream_start(tmp_path):
    folder=tmp_path/'round'
    journal=begin_capture(tmp_path,folder,{'episode':{},'segments':[]},['/joint_states'])
    folder.mkdir()
    with (folder/'original.mcap').open('wb') as stream:
        writer=Writer(stream,compression=CompressionType.NONE);writer.start(profile='ros2')
        calibration=writer.register_channel('/galaxis/gripper/left/encoder_offset','cdr',0)
        joints=writer.register_channel('/joint_states','cdr',0)
        writer.add_message(calibration,99_000_000_000,b'original calibration',99_000_000_000)
        for i in range(4):
            payload=b'\x00\x01\x00\x00'+struct.pack('<iI',100,i*10_000_000)
            writer.add_message(joints,100_000_000_000+i*10_000_000,payload,100_000_000_000+i*10_000_000)
        writer.finish()
    recover_episode(journal)
    with (folder/'recovered.mcap').open('rb') as stream:
        rows=list(make_reader(stream).iter_messages())
    assert rows[0][1].topic.endswith('/encoder_offset')
    assert rows[0][2].data==b'original calibration'
    assert json.loads((folder/'meta.json').read_text())['episode']['start_time']==100


def test_gap_recovery_preserves_and_uploads_both_sides_of_missing_interval(tmp_path):
    root=tmp_path/'rosbags';folder=root/'task/round'
    journal=begin_capture(root,folder,{'uuid':'split-source','author':'operator',
        'upload_destination':{'api_url':'https://platform.test'},'episode':{},'segments':[]},['/joint_states'])
    folder.mkdir(parents=True)
    source=folder/'original.mcap'
    times=(0,10_000_000,20_000_000,250_000_000,260_000_000,270_000_000)
    with source.open('wb') as stream:
        writer=Writer(stream,compression=CompressionType.NONE);writer.start(profile='ros2')
        channel=writer.register_channel('/joint_states','cdr',0)
        for offset in times:
            writer.add_message(channel,100_000_000_000+offset,
                b'\x00\x01\x00\x00'+struct.pack('<iI',100,offset),100_000_000_000+offset)
        writer.finish()
    original=source.read_bytes()
    recover_episode(journal)
    parts=[folder,folder.with_name('round__part002')]
    assert json.loads(journal.read_text())['recovered_parts']==['round','round__part002']
    recovered=[]
    for part in parts:
        with (part/'recovered.mcap').open('rb') as stream:
            rows=list(make_reader(stream).iter_messages())
        stamps=[struct.unpack_from('<iI',msg.data,4)[1] for _,_,msg in rows]
        assert max(b-a for a,b in zip(stamps,stamps[1:]))==10_000_000
        recovered.extend(stamps)
        meta=json.loads((part/'meta.json').read_text())
        assert meta['author']=='operator' and meta['interruption']['part_count']==2
        assert meta['upload_destination']=={'api_url':'https://platform.test'}
        assert not UmiRosbagUploader._processing(part)
    assert recovered==list(times) and source.read_bytes()==original
    registered=[]
    def handle(request):
        if request.url.path=='/uploads/presign':
            return httpx.Response(200,json={'urls':[{'filename':n,'put_url':f'https://storage.test/{n}','key':f'raw/{n}'} for n in json.loads(request.content)['filenames']]})
        if request.url.host=='storage.test':return httpx.Response(200)
        if request.url.path=='/episodes':
            registered.append(json.loads(request.content));return httpx.Response(201,json={'id':registered[-1]['id']})
        return httpx.Response(200,json={})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        uploader=UmiRosbagUploader(tmp_path,'https://platform.test',client=client)
        uploader.scan_once();uploader.scan_once()
    assert len(registered)==2 and len({r['id'] for r in registered})==2
    assert all(abs(r['duration_s']-.02)<1e-6 for r in registered)


def test_split_intervals_exclude_invalid_tracking_and_overlapping_gaps():
    from galaxis_core.capture_recovery import continuous_intervals
    assert continuous_intervals(0,1000,[
        {'valid_until_log_ns':100,'resume_log_ns':300},
        {'valid_until_log_ns':250,'resume_log_ns':500},
        {'valid_until_log_ns':700,'resume_log_ns':801}])==[(0,100),(500,700),(801,1000)]
