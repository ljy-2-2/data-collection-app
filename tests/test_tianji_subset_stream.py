"""Protocol selection and native slot identity, including absent head camera."""
import base64
import io
import itertools
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
import update_tianji_camera_stream as installer
sys.path.insert(0, str(installer.ROOT/'fusion_camera_stream/controller'))

streaming = SimpleNamespace()
exec(compile(base64.b64decode(installer.source_files()[
    'FusionDataCollector/controller_v22/streaming.py']), 'streaming.py', 'exec'), streaming.__dict__)


def test_long_stream_keeps_final_overflow_counts_and_rt_startup_evidence():
    baseline = json.dumps(dict(event='rt_baseline', task='RTPSI'))
    early = dict(event='queue_overflow', camera=1, dropped=2)
    final = dict(early, dropped=9, first_sequence=42, last_sequence=80,
                 first_timestamp_ns=100, last_timestamp_ns=900)
    data = '\n'.join([baseline, json.dumps(early)] +
                     [json.dumps(dict(event='teleop_health', padding='x'*200))]*6000 +
                     [json.dumps(final), json.dumps(dict(event='teleop_final'))])
    assert len(data) > 1024*1024
    diagnostics = streaming.read_native_diagnostics(io.BytesIO(data.encode()))
    assert baseline in diagnostics and len(diagnostics) <= 132
    assert streaming.native_queue_overflow(diagnostics) == {'usb-2':final}
    assert streaming.native_queue_overflow([json.dumps(early), 'unstructured log', json.dumps(final)]) == {'usb-2':final}


@pytest.mark.parametrize('ids', [list(c) for n in (1, 2, 3)
                               for c in itertools.combinations(('usb-1','usb-2','usb-4'), n)])
def test_every_subset_retains_native_indices(ids):
    chosen = streaming.selection(dict(camera_ids=ids[::-1], observation_mode='teleop_observation'))
    assert chosen == ids
    infos = [dict(id=c, address=i+2) for i, c in enumerate(streaming.CAMERA_IDS)]
    command = streaming.native_command('capture', 10, infos, chosen, 'mjpeg', 'teleop_observation')
    assert command[2:5] == [str(i+2) if c in ids else '0' for i,c in enumerate(streaming.CAMERA_IDS)]
    assert command[-1] == 'mask=%d' % sum(1 << i for i,c in enumerate(streaming.CAMERA_IDS) if c in ids)


@pytest.mark.parametrize('ids', [[], ['usb-2','usb-2'], ['usb-3'], None, 'usb-2'])
def test_invalid_selection_rejected_before_native_process(ids):
    with pytest.raises(ValueError): streaming.selection(dict(camera_ids=ids, observation_mode='teleop_observation'))
    api = SimpleNamespace(ACTIVE=None, reply=MagicMock())
    streaming.run(SimpleNamespace(fault=None), None,
        dict(camera_ids=ids, observation_mode='teleop_observation'), api)
    assert api.reply.call_args.args[1]['error'] == 'invalid_camera_selection'


def test_legacy_stationary_capture_keeps_original_command():
    ids = streaming.selection({})
    infos = [dict(id=c, address=i+2) for i,c in enumerate(ids)]
    assert streaming.native_command('capture', 10, infos, ids, 'mjpeg', 'stationary') == [
        'capture','10','2','3','4','mjpeg']
    with pytest.raises(ValueError): streaming.selection(dict(camera_ids=['usb-2']))


def test_installer_payload_preserves_recovery_and_advertises_subset_capability():
    files = installer.source_files()
    server = base64.b64decode(files['FusionDataCollector/controller_v22/server.py'])
    assert b"'version':'2.3.2'" in server
    assert b"'stream_camera_selection':True" in server
    assert b"if method == 'recover':" in server
    assert b'last_snapshot_error' in server
    assert b'audit_changed(before, after, key, mode)' in base64.b64decode(
        files['FusionDataCollector/controller_v22/streaming.py'])


@pytest.mark.parametrize('seconds,mode,expected', [
    (0, 'teleop_observation', 'capture_paused'),
    (600, 'teleop_observation', 'capture_paused'),
    (0, 'stationary', 'invalid_stream_limits'),
    (-1, 'teleop_observation', 'invalid_stream_limits'),
    (601, 'teleop_observation', 'capture_paused'),
    (86400, 'teleop_observation', 'capture_paused'),
    (601, 'stationary', 'invalid_stream_limits'),
    (False, 'teleop_observation', 'invalid_stream_limits'),
])
def test_duration_protocol_validation_without_hardware(seconds, mode, expected):
    from fusion_camera.stream import FusionStreamClient
    api = SimpleNamespace(ACTIVE=None, reply=MagicMock())
    streaming.run(SimpleNamespace(fault='offline test'), None,
                  dict(seconds=seconds, observation_mode=mode), api)
    assert api.reply.call_args.args[1]['error'] == expected
    client = object.__new__(FusionStreamClient)
    client.closed = True  # No socket or TLS connection can be created.
    with pytest.raises(RuntimeError if expected == 'capture_paused' else ValueError):
        next(client.stream(seconds=seconds, mode=mode))


@pytest.mark.parametrize('isolate', [False, True])
@pytest.mark.parametrize('seconds', [0, 1])
def test_joint_sampler_virtual_time_beyond_ten_minutes_and_explicit_stop(isolate, seconds):
    clock = SimpleNamespace(now=0.)
    sampler = streaming.JointSampler(SimpleNamespace(monotonic=lambda: clock.now), None,
                                     seconds, hz=30, stationary_only=False, isolate_faults=isolate)
    class Stop:
        def wait(self, delay):
            clock.now += delay
            return clock.now >= 601
    class Reader:
        def sample(self):
            while sampler.pop() is not None:
                pass  # Emulate the normal stream consumer; queue remains bounded.
            return dict(frame_serial=clock.now, arm_state=[0]*6, velocity_deg_s=[0]*14)
    sampler.done = Stop()
    sampler.reader = Reader()
    sampler.run()
    assert sampler.error is None
    if seconds == 0:
        assert clock.now >= 601 and sampler.count >= 18000
    else:
        assert clock.now < 1.01 and 29 <= sampler.count <= 31


def test_unlimited_capability_is_in_actual_install_payload():
    server = base64.b64decode(installer.source_files()[
        'FusionDataCollector/controller_v22/server.py']).decode()
    assert "'stream_unlimited':True" in server
    from FusionDataCollector.collector import storage_reserve_bytes
    assert storage_reserve_bytes(0, False) == storage_reserve_bytes(60, False)
    assert storage_reserve_bytes(0, True) > storage_reserve_bytes(0, False)
