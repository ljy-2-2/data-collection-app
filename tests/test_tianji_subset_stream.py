"""Protocol selection and native slot identity, including absent head camera."""
import base64
import itertools
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
import update_tianji_camera_stream as installer
sys.path.insert(0, str(installer.ROOT/'fusion_camera_stream/controller'))

streaming = SimpleNamespace()
exec(compile(base64.b64decode(installer.source_files()[
    'FusionDataCollector/controller_v22/streaming.py']), 'streaming.py', 'exec'), streaming.__dict__)


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
