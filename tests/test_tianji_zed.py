"""Real ZED descriptors, offline protocol and recording validation."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fusion_camera.profiles import valid_profile
from teleop_control.tianji_camera_settings import CameraSettings

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('zed_controls', ROOT/'fusion_data_collection/FusionDataCollector/controller_v22/camera_controls.py')
controls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controls)


def test_real_usb3_descriptor_has_exact_stereo_modes_and_brightness_unit():
    result = controls.parse_capabilities((ROOT/'tests/fixtures/zed_m_usb3_descriptors.bin').read_bytes())
    expected = {(2208,1242,15),(1920,1080,15),(1920,1080,30),
                (1280,720,15),(1280,720,30),(1280,720,60),
                (672,376,15),(672,376,30),(672,376,60),(672,376,100)}
    assert {tuple(controls.PROFILES[key][n] for n in ('width','height','fps')) for key in result['modes']} == expected
    assert result['brightness'] == (0, 3)
    assert all(valid_profile(controls.PROFILES[key]) for key in result['modes'])


@pytest.mark.parametrize('hid_pid,video_present,expected', [
    ('f681', False, True), ('f681', True, False), ('other', False, False), (None, False, False)])
def test_inventory_distinguishes_missing_zed_video_from_hid(monkeypatch, hid_pid, video_present, expected):
    spec = importlib.util.spec_from_file_location('zed_inventory_server',
        ROOT/'fusion_data_collection/FusionDataCollector/controller_v22/server.py')
    server = importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
    values = {}
    if hid_pid is not None:
        values.update({'1-1/idVendor':'2b03', '1-1/idProduct':hid_pid})
    if video_present:
        values.update({'2-1/idVendor':'2b03', '2-1/idProduct':'f682', '2-1/busnum':'2', '2-1/devnum':'3'})
    def read(path):
        key = path.removeprefix('/sys/bus/usb/devices/')
        if key not in values: raise IOError('missing USB device')
        return values[key]
    monkeypatch.setattr(server, 'read', read)
    result = server.device_info(dict(id='usb-1',vid='2b03',pid='f682',usb_path='2-1',
        format='mjpeg',width=1920,height=1080,fps=30))
    assert result['available'] is video_present
    assert (result.get('connection_issue') == 'zed_video_missing_hid_present') is expected


def test_snapshot_default_and_payload_fit_client_header_limit(tmp_path):
    camera = dict(id='usb-1',vid='2b03',pid='f682',usb_path='2-1')
    service = SimpleNamespace(config=dict(helper=str(tmp_path/'capture'),cameras=[camera]))
    mode, profile = controls.profile_for(service,'usb-1')
    assert mode == 'zed-left-1920x1080-30'
    caps = controls.parse_capabilities((ROOT/'tests/fixtures/zed_m_usb3_descriptors.bin').read_bytes())
    snapshot = controls.stream_snapshot(camera,dict(bus=2,address=3,vid='2b03',pid='f682'),caps,mode,
        dict(supported=True,min=0,max=8,step=1,default=4,value=4))
    checked = CameraSettings(SimpleNamespace())._checked('usb_1',snapshot)
    assert checked['resolution']['selected']['eye'] == 'left'
    assert checked['brightness']['max'] == 8
    # Leave room for two wrist snapshots, timing/stream data, and JSON escaping.
    assert len(json.dumps(snapshot)) < 6000
    assert all('ZED 左眼' in option['label'] for option in snapshot['resolution']['options'])


@pytest.mark.parametrize('change',[{'eye':'right'},{'source_width':1920},{'fps':60},{'format':'yuyv'}, {'encoding':'camera_native_jpeg'}])
def test_reject_mislabeled_stereo_or_unadvertised_1080_mode(change):
    profile = copy.deepcopy(controls.PROFILES['zed-left-1920x1080-30'])
    profile.update(change)
    assert not valid_profile(profile)
