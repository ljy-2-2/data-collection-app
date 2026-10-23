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
    controls.observe_stream(dict(cameras=[key.replace('_','-') for key in state],
                                 camera_controls={key.replace('_','-'):copy.deepcopy(value) for key,value in state.items()}))
    return SimpleNamespace(manager=manager,client=client,controls=controls,state=state)


def test_one_query_loads_both_cameras_and_second_control_uses_cache(rig):
    result = rig.controls.get('usb_2')
    assert result['brightness']['min'] == -64
    assert rig.controls.get('usb_4')['resolution']['selected']['id'] == 'mjpeg'
    assert rig.manager.pause_cameras.call_count == 0
    assert rig.client.camera_settings.call_count == 0


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
    monkeypatch.setattr(rig.controls, '_verify_preview', lambda generation, expected: (_ for _ in ()).throw(RuntimeError('wrong actual frame')))
    with pytest.raises(RuntimeError, match='wrong actual frame'):
        rig.controls.request('usb_2', mode_id='mjpeg-1920x1080', brightness=10)
    assert rig.state['usb_2']['brightness']['value'] == 0
    assert rig.state['usb_2']['resolution']['selected']['id'] == 'mjpeg'
    assert not rig.controls.busy


def test_replug_invalidates_only_the_device_that_changed(rig):
    inventory=[dict(id=k.replace('_','-'),available=True,bus=1,address=i+3,vid='32e4',pid='9230') for i,k in enumerate(rig.state)]
    rig.controls.inventory(inventory)
    rig.controls.observe_stream(dict(cameras=[key.replace('_','-') for key in rig.state],
        camera_controls={key.replace('_','-'):copy.deepcopy(value) for key,value in rig.state.items()}))
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
    monkeypatch.setattr(rig.controls,'_verify_preview',lambda generation, expected:(_ for _ in ()).throw(RuntimeError('camera capture failed')))
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


def test_repeated_settings_reads_never_pause_live_teleoperation_or_query_usb(rig):
    rig.manager.value['state']='running';rig.manager.collection.armed=True
    rig.controls.loaded_at={key:0 for key in rig.state}
    for _ in range(100):
        assert rig.controls.get('usb_2')['brightness']['value']==0
        assert rig.controls.get('usb_4')['resolution']['selected']['id']=='mjpeg'
    rig.manager.pause_cameras.assert_not_called()
    rig.manager.new_camera_client.assert_not_called()
    assert not rig.controls.busy


def test_missing_stream_settings_does_not_interrupt_preview_or_query_controller(rig):
    rig.controls.cache.clear()
    with pytest.raises(RuntimeError,match='设置快照'):rig.controls.get('usb_2')
    rig.manager.pause_cameras.assert_not_called()
    rig.manager.new_camera_client.assert_not_called()


def test_existing_preview_fault_rejects_settings_without_restarting_or_writing(rig):
    rig.manager.camera_phase = 'fault'
    rig.manager.camera_error = 'Audit changed: dmesg_sha256'
    with pytest.raises(RuntimeError, match='请先检查设备.*Audit changed'):
        rig.controls.request('usb_2', brightness=7)
    rig.manager.new_camera_client.assert_not_called()
    rig.manager.pause_cameras.assert_not_called()
    rig.manager.start_cameras.assert_not_called()
    assert not rig.controls.busy


def test_controller_fault_is_checked_after_stopping_stream_before_writing(rig):
    rig.client.status.return_value = dict(camera_controls=True, fault='Audit changed: dmesg_sha256')
    with pytest.raises(RuntimeError, match='控制器相机服务存在故障'):
        rig.controls.request('usb_2', brightness=7)
    rig.client.camera_settings.assert_not_called()
    rig.manager.pause_cameras.assert_called_once()
    rig.manager.start_cameras.assert_not_called()
    assert not rig.controls.busy and not rig.manager.camera_preview_restarting


def test_first_frame_failure_restores_preferences_without_another_usb_write_or_restart(rig):
    def fail_start():
        rig.manager.camera_phase = 'fault'
        rig.manager.camera_error = 'Camera did not produce first frame after bounded startup retry; stop'
        rig.client.status.return_value = dict(camera_controls=True, fault=rig.manager.camera_error)
    rig.manager.start_cameras.side_effect = fail_start
    rig.client.restore_camera_settings.return_value = dict(restored_configuration=True)
    with pytest.raises(RuntimeError, match='first frame.*已恢复原采集配置'):
        rig.controls.request('usb_2', brightness=7)
    # One read and the requested write. Rollback only restores saved preferences
    # while the controller's hardware fault is latched.
    assert rig.client.camera_settings.call_count == 2
    rig.client.restore_camera_settings.assert_called_once_with('usb-2')
    rig.manager.start_cameras.assert_called_once()
    assert rig.controls.cache['usb_2']['brightness']['value'] == 0
    assert rig.manager.camera_phase == 'fault'
    assert not rig.controls.busy and not rig.manager.camera_preview_restarting


def test_batch_stops_once_and_never_connects_to_controller_during_live_preview(rig):
    running = [True]
    pause, start = rig.manager.pause_cameras.side_effect, rig.manager.start_cameras.side_effect
    def stop_preview():
        running[0] = False
        pause()
    def start_preview():
        running[0] = True
        start()
    def connect():
        assert not running[0], 'Serial TLS service cannot accept settings/status during stream'
        return rig.client
    rig.manager.pause_cameras.side_effect = stop_preview
    rig.manager.start_cameras.side_effect = start_preview
    rig.manager.new_camera_client.side_effect = connect
    result = rig.controls.apply({'usb_4': {'brightness': 7}, 'usb_2': {'brightness': 10}})
    assert result['usb_4']['brightness']['value'] == 7
    assert result['usb_2']['brightness']['value'] == 10
    rig.manager.pause_cameras.assert_called_once()
    rig.manager.start_cameras.assert_called_once()
    assert [c.kwargs for c in rig.client.camera_settings.call_args_list if c.kwargs] == [
        {'brightness': 7}, {'brightness': 10}]


def test_later_camera_write_failure_rolls_back_entire_batch_before_preview_reopens(rig):
    settings = rig.client.camera_settings.side_effect
    def fail_second(key, **fields):
        result = settings(key, **fields)
        if key == 'usb-2' and fields.get('brightness') == 10:
            raise TimeoutError('reply lost after committed write')
        return result
    rig.client.camera_settings.side_effect = fail_second
    with pytest.raises(RuntimeError, match='右相机写入参数失败.*已回写'):
        rig.controls.apply({'usb_4': {'brightness': 7}, 'usb_2': {'brightness': 10}})
    assert all(v['brightness']['value'] == 0 for v in rig.state.values())
    assert all(v['brightness']['value'] == 0 for v in rig.controls.cache.values())
    rig.manager.pause_cameras.assert_called_once()
    rig.manager.start_cameras.assert_called_once()
    assert all('mode_id' not in c.kwargs for c in rig.client.camera_settings.call_args_list)


@pytest.mark.parametrize('fields', [{'brightness': 1.5}, {'brightness': True}, {'brightness': 999}, {'mode_id': 'unknown'}])
def test_invalid_later_draft_rejects_whole_batch_without_pausing(rig, fields):
    with pytest.raises(ValueError):
        rig.controls.apply({'usb_4': {'brightness': 7}, 'usb_2': fields})
    rig.manager.pause_cameras.assert_not_called()
    rig.manager.new_camera_client.assert_not_called()


def test_unchanged_confirm_does_not_pause_or_query_hardware(rig):
    rig.controls.apply({'usb_2': {'brightness': 0, 'mode_id': 'mjpeg'}})
    rig.manager.pause_cameras.assert_not_called()
    rig.manager.new_camera_client.assert_not_called()


def test_new_stream_cannot_overwrite_expected_brightness_and_falsely_pass_verification(rig):
    start = rig.manager.start_cameras.side_effect
    def wrong_readback():
        start()
        rig.controls.cache['usb_2']['brightness']['value'] = 0
    rig.manager.start_cameras.side_effect = wrong_readback
    with pytest.raises(RuntimeError, match='取流读回的参数与请求不一致'):
        rig.controls.request('usb_2', brightness=7)
    assert rig.state['usb_2']['brightness']['value'] == 0


def test_stopping_failure_never_opens_another_tls_connection(rig):
    rig.manager.pause_cameras.side_effect = RuntimeError('preview has not released')
    with pytest.raises(RuntimeError, match='停止相机预览失败'):
        rig.controls.request('usb_2', brightness=7)
    rig.manager.new_camera_client.assert_not_called()
    rig.manager.start_cameras.assert_not_called()
    assert not rig.controls.busy


def test_serial_tls_stream_reproduces_old_timeout_and_batch_api_stops_it_first(rig, tmp_path):
    """Real loopback TLS and SDK, fake images/USB. No robot/controller access."""
    import json
    import socket
    import ssl
    import struct
    import subprocess
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fusion_camera.client import _exact
    from fusion_camera.stream import FusionStreamClient
    from teleop_control.tianji import tianji_router

    cert, key = tmp_path/'cert.pem', tmp_path/'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'],
                   check=True, capture_output=True, timeout=10)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0));listener.listen(5);listener.settimeout(.1)
    credentials = tmp_path/'client.json'
    credentials.write_text(json.dumps(dict(port=listener.getsockname()[1],
        token='offline-test-only', certificate=cert.name)))
    stopped, streaming = threading.Event(), threading.Event()
    calls, failures = [], []
    settings = rig.client.camera_settings.side_effect

    def reply(sock, metadata):
        data = json.dumps(dict(metadata, ok=True, bytes=0)).encode()
        sock.sendall(struct.pack('!I', len(data)) + data)

    def serve():
        while not stopped.is_set():
            try:
                raw, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                with raw:
                    raw.settimeout(3)
                    with context.wrap_socket(raw, server_side=True) as sock:
                        request = json.loads(_exact(sock, struct.unpack('!I', _exact(sock, 4))[0]))
                        assert request['token'] == 'offline-test-only'
                        method = request['method'];calls.append(method)
                        if method == 'stream':
                            reply(sock, dict(type='start'))
                            streaming.set()
                            assert sock.recv(1) == b'S'
                            calls.append('stream_stopped')
                            reply(sock, dict(type='end'))
                            streaming.clear()
                        elif method == 'status':
                            reply(sock, dict(camera_controls=True, idle=True, fault=None))
                        elif method == 'camera_settings':
                            reply(sock, settings(request['camera_id'], **{
                                k: request[k] for k in ('mode_id', 'brightness') if k in request}))
                        else:
                            raise AssertionError(method)
            except ssl.SSLError:
                pass  # The deliberately timed-out pre-stop handshake is abandoned.
            except Exception as error:
                failures.append(error)
    server = threading.Thread(target=serve, daemon=True);server.start()
    preview = FusionStreamClient('127.0.0.1', credentials=credentials, timeout=2)
    def read_preview():
        try:
            for _ in preview.stream(seconds=0, mode='teleop_observation'):
                pass
        except Exception as error:
            failures.append(error)
    receiver = threading.Thread(target=read_preview, daemon=True);receiver.start()
    try:
        assert streaming.wait(2)
        # Reproduce the former preflight ordering, with the production SDK.
        with pytest.raises(TimeoutError, match='handshake'):
            with FusionStreamClient('127.0.0.1', credentials=credentials, timeout=.2):
                pass
        assert streaming.is_set()
        pause = rig.manager.pause_cameras.side_effect
        def stop_preview():
            preview.request_stop();receiver.join(timeout=3)
            assert not receiver.is_alive()
            pause()
        rig.manager.pause_cameras.side_effect = stop_preview
        rig.manager.new_camera_client.side_effect = lambda: FusionStreamClient(
            '127.0.0.1', credentials=credentials, timeout=2)
        rig.manager.camera_settings = rig.controls
        app = FastAPI();app.include_router(tianji_router(rig.manager))
        with TestClient(app) as api:
            for invalid in ({}, {'usb_2': {'brightness': 10.0}}, {'unknown': {'brightness': 10}}):
                response = api.post('/tianji/camera-controls/settings', json={'changes': invalid})
                assert response.status_code == 422, response.text
            rig.manager.pause_cameras.assert_not_called()
            response = api.post('/tianji/camera-controls/settings', json={'changes': {
                'usb_4': {'brightness': 7}, 'usb_2': {'brightness': 10}}})
        assert response.status_code == 200, response.text
        assert response.json()['usb_2']['brightness']['value'] == 10
        assert response.json()['usb_4']['brightness']['value'] == 7
        assert calls.index('stream_stopped') < calls.index('status') < calls.index('camera_settings')
        rig.manager.pause_cameras.assert_called_once()
        rig.manager.start_cameras.assert_called_once()
        assert not failures
    finally:
        preview.close();stopped.set();listener.close()
        receiver.join(timeout=3);server.join(timeout=3)
