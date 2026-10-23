"""Capture must stay independent of video encoding and platform availability."""
import fcntl
import json
from pathlib import Path
import sys
import threading
import time

import pytest
from mcap.reader import NonSeekingReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'umi_collection/vendor/galaxis/galaxis-core/galaxis-core'))
sys.path.insert(0, str(ROOT/'teleop_control/src'))
from galaxis_core.compressed_recorder import CompressedBag
from galaxis_core.deferred_raw import finalize_raw
from galaxis_core.mcap_video import IMAGE_TYPE, IMAGE_SCHEMA, VIDEO_TYPE
from galaxis_core.mp4_worker import atomic_json, finalize_episode
from test_umi_mp4_bundle import image_message


def staged(folder, monkeypatch):
    # No encoder may be started during capture, even when CPU is overloaded.
    with monkeypatch.context() as patch:
        patch.setattr('galaxis_core.compressed_recorder.LiveEncoder',
                      lambda *a, **k: pytest.fail('Capture started an encoder'))
        bag = CompressedBag(folder, defer_video=True)
        topic = '/center_camera/image_raw/compressed'
        bag.topic(topic, IMAGE_TYPE, IMAGE_SCHEMA)
        bag.topic('/exact', 'std_msgs/msg/String', b'string data\n')
        for i in range(80):
            stamp = 100_000_000_000+i*33_333_333
            bag.message(topic, image_message(i*33_333_333, 'red'), stamp, stamp-1, i)
            bag.message('/exact', b'\0\1\0\0\x04\0\0\0abc\0', stamp, stamp-2, i)
        bag.close()
    assert not list(folder.glob('*.mcap'))
    assert not (folder/'metadata.yaml').exists()
    assert (folder/'.deferred-raw.json').exists()
    return bag


def test_only_completed_compression_publishes_raw_and_preserves_messages(tmp_path, monkeypatch):
    folder = tmp_path/'round'
    capture = staged(folder, monkeypatch)
    atomic_json(folder/'.mp4-pending.json', dict(_deferred_raw=True, author='operator'))
    finalize_episode(folder)
    assert not capture.partial.exists()
    assert not (folder/'.mp4-pending.json').exists()
    status = json.loads((folder/'raw-data-status.json').read_text())
    assert status['state'] == 'ready'
    final = folder/status['file']
    with final.open('rb') as stream:
        rows = list(NonSeekingReader(stream, validate_crcs=True).iter_messages(log_time_order=False))
    images = [(s, m) for s, c, m in rows if c.topic.endswith('/compressed')]
    signals = [m for s, c, m in rows if c.topic == '/exact']
    assert len(images) == len(signals) == 80
    assert all(s.name == VIDEO_TYPE for s, m in images)
    assert [m.sequence for m in signals] == list(range(80))
    assert all(m.data == b'\0\1\0\0\x04\0\0\0abc\0' for m in signals)
    assert all(m.publish_time == m.log_time-2 for m in signals)
    # Retrying after the publish/queue-ack boundary cannot reencode or lose it.
    before = final.read_bytes()
    finalize_raw(folder, {'author': 'operator'})
    assert final.read_bytes() == before


def test_failed_compression_keeps_staging_and_never_publishes_ready(tmp_path, monkeypatch):
    folder = tmp_path/'round'; bag = staged(folder, monkeypatch)
    original = bag.partial.read_bytes()
    monkeypatch.setattr('galaxis_core.deferred_raw.CompressedBag.message',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('encoder failure')))
    with pytest.raises(RuntimeError, match='encoder failure'):
        finalize_raw(folder, {})
    assert bag.partial.read_bytes() == original
    assert not list(folder.glob('*.mcap'))
    assert not (folder/'.compressed-raw.json').exists()


def test_crashed_deferred_capture_recovers_compressed_raw_without_deleting_prefix(tmp_path, monkeypatch):
    from galaxis_core.capture_recovery import begin_capture, interrupt_capture, recover_episode
    root = tmp_path/'rosbags'; folder = root/'task'/'round'; folder.parent.mkdir(parents=True)
    journal = begin_capture(root, folder, {'_deferred_raw': True, 'episode': {}},
                            ['/center_camera/image_raw/compressed', '/exact'])
    bag = staged(folder, monkeypatch)
    original = bag.partial.read_bytes()
    interrupt_capture(journal, 'test-crash')
    recover_episode(journal)
    assert bag.partial.read_bytes() == original
    assert json.loads(journal.read_text())['state'] == 'recovered'
    meta = json.loads((folder/'meta.json').read_text())
    assert meta['recording_format'] == 'umi-mcap-h264-v1'
    assert meta['interruption']['reason'] == 'test-crash'
    assert not (folder/'recovered.mcap').exists()
    # Simulate power loss after the last output publishes but before journal
    # acknowledgement. Retrying must neither include its own output as input
    # nor leave metadata pointing at the disposable JPEG repair copy.
    value = json.loads(journal.read_text()); value['state'] = 'interrupted'
    journal.write_text(json.dumps(value))
    recover_episode(journal)
    import yaml
    info = yaml.safe_load((folder/'metadata.yaml').read_text())['rosbag2_bagfile_information']
    assert info['relative_file_paths'] == [folder.name+'_0.mcap']
    assert (folder/info['relative_file_paths'][0]).is_file()
    assert bag.partial.read_bytes() == original


def test_upload_stops_reading_during_capture_and_resumes_without_losing_bytes(tmp_path):
    import httpx
    from teleop_control.umi_uploader import UmiRosbagUploader, _chunks
    uploader = UmiRosbagUploader(tmp_path, 'http://test', client=httpx.Client())
    uploader.root.mkdir()
    source = tmp_path/'payload'; source.write_bytes(b'a'*4096)
    lock = (uploader.root/'.capture-intent.lock').open('wb')
    fcntl.flock(lock, fcntl.LOCK_EX)
    result = []
    reader = threading.Thread(target=lambda: result.append(b''.join(
        _chunks(source, size=128, before_read=uploader._wait_for_capture))))
    reader.start()
    time.sleep(.05)
    assert reader.is_alive() and not result
    lock.close(); reader.join(3)
    assert not reader.is_alive() and result == [source.read_bytes()]
    uploader.client.close()


def test_old_short_gap_marker_is_finalized_as_one_round_with_quality_evidence(tmp_path):
    import struct
    folder=tmp_path/'round';bag=CompressedBag(folder,defer_video=True)
    topic='/joint_states';bag.topic(topic,'sensor_msgs/msg/JointState',b'std_msgs/Header header')
    times=(0,10_000_000,250_000_000,260_000_000)
    payloads=[]
    for offset in times:
        payload=b'\0\1\0\0'+struct.pack('<iI',100,offset);payloads.append(payload)
        bag.message(topic,payload,100_000_000_000+offset)
    bag.close()
    marker=folder/'.deferred-raw.json';old=json.loads(marker.read_text())
    old['continuity_issue']={'topic':topic,'gap_ns':240_000_000}
    atomic_json(marker,old)
    finalize_raw(folder,{})
    status=json.loads((folder/'raw-data-status.json').read_text())
    assert status['state']=='ready' and status['quality']=='source_gaps_require_review'
    assert status['source_continuity'][topic]['warnings_over_200ms']==1
    assert len(list(folder.glob('*.mcap')))==1
    with (folder/status['file']).open('rb') as f:
        assert [m.data for _,_,m in NonSeekingReader(f).iter_messages(log_time_order=False)]==payloads
    assert not list(tmp_path.glob('*__part*'))
