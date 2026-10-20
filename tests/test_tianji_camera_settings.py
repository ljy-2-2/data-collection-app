"""Camera controls with fake USB and preview ownership, never real hardware."""
import copy
import importlib.util
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from teleop_control.tianji_camera_settings import CameraSettings

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('camera_control_protocol', ROOT / 'fusion_data_collection/FusionDataCollector/controller_v22/camera_controls.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


def descriptor(width=1024, height=768, interval=333333):
    import struct
    interface = bytes([9, 4, 1, 0, 1, 14, 2, 0, 0])
    fmt = bytes([11, 0x24, 6, 1, 1, 0, 1, 0, 0, 0, 0])
    frame = bytearray(30)
    frame[:5] = bytes([30, 0x24, 7, 1, 0])
    struct.pack_into('<HH', frame, 5, width, height)
    frame[25] = 1
    struct.pack_into('<I', frame, 26, interval)
    return interface + fmt + frame


def test_only_advertised_30fps_modes_are_offered_and_brightness_unit_is_scoped():
    control_interface = bytes([9, 4, 0, 0, 0, 14, 1, 0, 0])
    processing = bytes([12, 0x24, 5, 2, 1, 0, 0, 2, 1, 0, 0, 0])
    parsed = protocol.parse_capabilities(control_interface + processing + descriptor() + descriptor(1920, 1080) + descriptor(640, 480, 83263))
    assert parsed['brightness'] == (0, 2)
    assert parsed['modes'] == ['mjpeg', 'mjpeg-1920x1080']
    with pytest.raises(ValueError):
        protocol.parse_capabilities(descriptor()[:-1])


@pytest.fixture
def rig():
    options = [dict(v, id=k, label=k) for k, v in protocol.PROFILES.items()]
    state = {key:dict(camera_id=key.replace('_', '-'), brightness=dict(supported=True,value=0,min=-64,max=64,step=1,default=0),
                     resolution=dict(options=options, selected=options[0], editable=True)) for key in ('usb_2', 'usb_4')}
    client = MagicMock()
    client.__enter__.return_value = client
    client.status.return_value = dict(camera_controls=True)
    def settings(key, mode_id=None, brightness=None):
        entry = state[key.replace('-', '_')]
        if mode_id is not None:
            entry['resolution']['selected'] = next(p for p in options if p['id'] == mode_id)
        if brightness is not None:
            if not -64 <= brightness <= 64:
                raise ValueError('Brightness outside hardware range')
            entry['brightness']['value'] = brightness
        return copy.deepcopy(entry)
    client.camera_settings.side_effect = settings
    manager = SimpleNamespace(command_lock=threading.RLock(), lock=threading.RLock(), value=dict(state='idle'),
        collection=SimpleNamespace(armed=False, active=lambda:False), require_other_idle=MagicMock(),
        camera_inventory={'usb_2':'usb-2','usb_4':'usb-4'}, frames={},camera_frame_metadata={},
        camera_error='',camera_phase='streaming',camera_preview_restarting=False,new_camera_client=MagicMock(return_value=client))
    def pause():
        manager.frames.clear();manager.camera_frame_metadata.clear()
    def start():
        for key, value in state.items():
            manager.frames[key] = (b'jpeg',time.monotonic())
            manager.camera_frame_metadata[key] = copy.deepcopy(value['resolution']['selected'])
    manager.pause_cameras=MagicMock(side_effect=pause)
    manager.start_cameras=MagicMock(side_effect=start)
    controls=CameraSettings(manager)
    return SimpleNamespace(manager=manager,client=client,controls=controls,state=state)


def test_one_query_loads_both_cameras_and_second_control_uses_cache(rig):
    result = rig.controls.get('usb_2')
    assert result['brightness']['min'] == -64
    assert rig.controls.get('usb_4')['resolution']['selected']['id'] == 'mjpeg'
    assert rig.manager.pause_cameras.call_count == 1
    assert rig.client.camera_settings.call_count == 2


@pytest.mark.parametrize('busy', ['running', 'moving', 'armed', 'recording'])
def test_settings_cannot_interrupt_control_or_recording(rig, busy):
    if busy in ('running', 'moving'):rig.manager.value['state']=busy
    if busy=='armed':rig.manager.collection.armed=True
    if busy=='recording':rig.manager.collection.active=lambda:True
    with pytest.raises(ValueError, match='停止遥操和采集'):
        rig.controls.request('usb_2', brightness=1)
    rig.manager.pause_cameras.assert_not_called()
    rig.manager.new_camera_client.assert_not_called()


def test_new_resolution_and_hardware_brightness_have_verified_preview(rig):
    rig.controls.get('usb_2')
    result = rig.controls.request('usb_2', mode_id='mjpeg-1920x1080', brightness=-8)
    assert result['resolution']['selected']['width'] == 1920
    assert result['brightness']['value'] == -8
    assert rig.manager.camera_frame_metadata['usb_2']['height'] == 1080
    assert rig.state['usb_4']['brightness']['value'] == 0


def test_failed_real_frame_verification_restores_previous_camera_settings(rig, monkeypatch):
    rig.controls.get('usb_2')
    monkeypatch.setattr(rig.controls, '_verify_preview', lambda generation: (_ for _ in ()).throw(RuntimeError('wrong actual frame')))
    with pytest.raises(RuntimeError, match='wrong actual frame'):
        rig.controls.request('usb_2', mode_id='mjpeg-1920x1080', brightness=10)
    assert rig.state['usb_2']['brightness']['value'] == 0
    assert rig.state['usb_2']['resolution']['selected']['id'] == 'mjpeg'
    assert not rig.controls.busy


def test_replug_invalidates_only_the_device_that_changed(rig):
    inventory=[dict(id=k.replace('_','-'),available=True,bus=1,address=i+3,vid='32e4',pid='9230') for i,k in enumerate(rig.state)]
    rig.controls.inventory(inventory)
    rig.controls.get('usb_2')
    inventory[0]['address'] += 2
    rig.controls.inventory(inventory)
    assert 'usb_2' not in rig.controls.cache and 'usb_4' in rig.controls.cache


def test_native_profiles_preserve_sizes_and_hardware_metadata_when_stored(tmp_path):
    import io
    from PIL import Image
    from fusion_camera.stream import StreamEvent
    from FusionDataCollector import collector
    profiles={'usb-2':dict(format='mjpeg',width=1280,height=1024,fps=30,brightness=dict(supported=True,value=7)),
              'usb-4':dict(format='yuyv',width=320,height=240,fps=30,brightness=dict(supported=True,value=-8))}
    preprocessing=collector.image_preprocessing(dict(format='mixed',native_size=None,cameras=list(profiles),camera_profiles=profiles),False)
    for key, profile in profiles.items():
        name=key.replace('-','_')
        (tmp_path/'camera'/name/'frames').mkdir(parents=True)
        buf=io.BytesIO()
        if profile['format']=='mjpeg':
            Image.new('RGB',(profile['width'],profile['height']),(50,80,100)).save(buf,format='JPEG')
            data=buf.getvalue()
        else:data=bytes([90,128,90,128])*(320*240//2)
        event=StreamEvent(dict(profile,type='image',camera_id=key),data)
        row=collector.encode(tmp_path,event,0,name,False,preprocessing['per_camera'][key]['stored_size'])
        output=tmp_path/row['image_path']
        with Image.open(output) as image:assert image.size==(profile['width'],profile['height'])
        if profile['format']=='mjpeg':assert output.read_bytes()==data
        assert [row['stored_width'],row['stored_height']]==preprocessing['per_camera'][key]['stored_size']


def test_controller_restore_keeps_fault_and_uses_only_its_verified_snapshot(tmp_path):
    service=SimpleNamespace(config={'helper':str(tmp_path/'capture')},fault='USB fault',
        camera_settings_rollback={'usb-2':dict(mode_id='mjpeg',brightness=0)})
    protocol.save_settings(service,{'usb-2':dict(mode_id='yuyv',brightness=8)})
    api=SimpleNamespace(ACTIVE=None)
    result=protocol.restore(service,{'camera_id':'usb-2','mode_id':'untrusted'},api)
    assert result['hardware_readback_deferred'] and result['restored_configuration']
    assert protocol.load_settings(service)=={'usb-2':dict(mode_id='mjpeg',brightness=0)}
    assert service.fault=='USB fault'
    with pytest.raises(ValueError):protocol.restore(service,{'camera_id':'usb-2'},api)


def test_manager_restore_after_usb_fault_preserves_failure_and_rolls_back_preferences(rig,monkeypatch):
    rig.controls.get('usb_2')
    actual=rig.client.camera_settings.side_effect
    def settings(key,**kwargs):
        if kwargs.get('mode_id')=='mjpeg':raise RuntimeError('USB fault still latched')
        return actual(key,**kwargs)
    rig.client.camera_settings.side_effect=settings
    rig.client.restore_camera_settings.return_value={'restored_configuration':True,'hardware_readback_deferred':True}
    monkeypatch.setattr(rig.controls,'_verify_preview',lambda generation:(_ for _ in ()).throw(RuntimeError('camera capture failed')))
    with pytest.raises(RuntimeError,match='已恢复原采集配置，硬件故障仍需排除后验证'):
        rig.controls.request('usb_2',mode_id='mjpeg-1920x1080')
    rig.client.restore_camera_settings.assert_called_once_with('usb-2')
    assert rig.controls.cache['usb_2']['resolution']['selected']['id']=='mjpeg'


def test_hardware_capabilities_exclude_raw_modes_that_overload_transport(monkeypatch):
    import builtins,io
    camera=dict(usb_path='1-2',vid='32e4',pid='9230')
    def fake_open(path,*args):
        if path.endswith('idVendor'):return io.StringIO('32e4')
        if path.endswith('idProduct'):return io.StringIO('9230')
        return io.BytesIO(b'descriptors')
    monkeypatch.setattr(builtins,'open',fake_open)
    monkeypatch.setattr(protocol,'parse_capabilities',lambda data:dict(modes=['yuyv','mjpeg','yuyv-320x240'],brightness=(0,2)))
    assert protocol.camera_capabilities(camera)['modes']==['mjpeg']
