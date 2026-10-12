import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / 'umi_collection/vendor/galaxis'
sys.path[:0] = [str(ROOT/'teleop_control/src'), str(VENDOR/'galaxis-core/galaxis-core')]
from teleop_control import umi
from teleop_control.camera_resolution import parse_modes, choose_mode, ros_parameters, usb_connection_info
from galaxis_core.camera_quality import CameraCadence, timing_failure, recording_timing_result

LISTING = """
 [0]: 'MJPG' (Motion-JPEG, compressed)
    Size: Discrete 640x480
        Interval: Discrete 0.033s (30.000 fps)
    Size: Discrete 1920x1080
        Interval: Discrete 0.033s (30.000 fps)
    Size: Discrete 3840x2160
        Interval: Discrete 0.067s (15.000 fps)
 [1]: 'YUYV'
    Size: Discrete 640x480
        Interval: Discrete 0.033s (30.000 fps)
    Size: Discrete 1280x720
        Interval: Discrete 0.033s (29.970 fps)
    Size: Stepwise 1x1 - 9000x9000 with step 1/1
        Interval: Discrete 0.033s (30.000 fps)
"""


def test_usb_label_uses_actual_link_speed_and_handles_reconnection(tmp_path):
    usb = tmp_path/'devices/usb3/3-4'; interface = usb/'3-4:1.0'
    interface.mkdir(parents=True)
    (usb/'idVendor').write_text('1234')
    (usb/'speed').write_text('480')
    sys_class = tmp_path/'video4linux'; (sys_class/'video8').mkdir(parents=True)
    (sys_class/'video8/device').symlink_to(interface, target_is_directory=True)
    selected = tmp_path/'camera-by-id'; selected.symlink_to('/dev/video8')
    info = usb_connection_info(str(selected), sys_class=sys_class)
    assert info['speed_mbps'] == 480 and info['usb_path'] == '3-4'
    assert info['label'] == 'USB 当前连接 480 Mbps'
    (usb/'speed').write_text('5000')
    assert usb_connection_info(str(selected), sys_class=sys_class)['label'] == 'USB 当前连接 5 Gbps'
    (usb/'speed').unlink()
    assert usb_connection_info(str(selected), sys_class=sys_class) == {}
    assert usb_connection_info('/dev/video99', sys_class=sys_class) == {}


def load(name, path):
    spec=importlib.util.spec_from_file_location(name, path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_only_native_thirty_fps_modes_and_prefer_compressed_transport():
    modes=parse_modes(LISTING)
    assert [(m['width'], m['height'], m['fourcc']) for m in modes] == [
        (640,480,'MJPG'), (640,480,'YUYV'), (1280,720,'YUYV'), (1920,1080,'MJPG')]
    assert choose_mode(modes)['pixel_format']=='mjpeg2rgb'
    assert all(m['pixel_format']=='yuyv2rgb' for m in modes if m['fourcc']=='YUYV')
    assert choose_mode(modes)['width']==1920
    assert choose_mode(modes,modes[-1])['width']==1920
    assert choose_mode(modes,{'id':'unsupported'})['width']==1920
    assert choose_mode(modes,modes[0])['width']==640  # Explicit saved choice wins.
    assert choose_mode(modes[:-1])['width']==1280
    with pytest.raises(RuntimeError):choose_mode([])


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(umi,'read_modes',lambda _:parse_modes(LISTING))
    m=umi.UmiManager(tmp_path,tmp_path/'data',python=sys.executable,runtime_dir=tmp_path/'runtime')
    m._device_check={'camera_candidates':['/dev/video0','/dev/video2']}
    monkeypatch.setattr(m,'_udev_properties',lambda device:dict(ID_VENDOR_ID='1234',ID_MODEL_ID='9876',ID_PATH=device))
    return m


class Preview:
    error='相机实际输出 640×480，未能应用 1920×1080'
    def __init__(self,good=True):self.good=good;self.modes=[]
    def configure_mode(self,mode):self.modes.append(mode)
    def wait_for_frame(self,timeout):return self.good


def test_resolution_applies_to_preview_persists_by_hardware_and_rolls_back(manager):
    mode=manager.camera_resolution('video0')['options'][-1]
    worker=Preview();manager._camera_workers['/dev/video0']=worker
    result=manager.camera_resolution('video0',mode['id'])
    assert result['selected']==mode and worker.modes[-1]==mode
    assert manager.camera_resolution('video0')['selected']==mode
    assert manager.camera_resolution('video2')['selected']['width']==1920
    previous=manager._camera_modes_file.read_bytes()
    worker.good=False
    with pytest.raises(RuntimeError,match='实际输出'):
        manager.camera_resolution('video0',parse_modes(LISTING)[0]['id'])
    assert manager._camera_modes_file.read_bytes()==previous
    assert worker.modes[-1]==mode
    manager._saved_camera_modes=json.loads(previous)
    assert manager.camera_resolution('video0')['selected']==mode


def test_monitoring_rejects_resolution_writes_even_between_episodes(manager):
    manager._process=SimpleNamespace(poll=lambda:None)
    with pytest.raises(RuntimeError,match='结束采集'):
        manager.camera_resolution('left','1920x1080-MJPG-30')
    with pytest.raises(RuntimeError):manager.camera_resolution('video0','1920x1080-MJPG-30')


@pytest.mark.parametrize('key',['video1','../video0','video0;echo','/dev/video0'])
def test_invalid_device_rejected_before_query(manager,monkeypatch,key):
    monkeypatch.setattr(umi,'read_modes',lambda _:pytest.fail('unexpected hardware access'))
    with pytest.raises(ValueError):manager.camera_resolution(key)


def test_unknown_mode_does_not_reopen_camera(manager):
    worker=Preview();manager._camera_workers['/dev/video0']=worker
    with pytest.raises(ValueError):manager.camera_resolution('video0','3840x2160-MJPG-15')
    assert not worker.modes


def test_replug_reenumerates_capabilities_instead_of_reusing_video_number(manager,monkeypatch):
    modes=parse_modes(LISTING)
    worker=Preview();manager._camera_workers['/dev/video0']=worker
    manager.camera_resolution('video0',modes[0]['id'])
    # The same physical port/camera gets a different videoN after reconnect.
    monkeypatch.setattr(manager,'_udev_properties',lambda _:dict(ID_VENDOR_ID='1234',ID_MODEL_ID='9876',ID_PATH='/dev/video0'))
    assert manager.camera_resolution('video2')['selected']['width']==640
    # Replacement hardware at that port must enumerate its own capabilities.
    replacement=LISTING.replace('1920x1080','1280x1024').replace('640x480','800x600')
    monkeypatch.setattr(umi,'read_modes',lambda _:parse_modes(replacement))
    response=manager.camera_resolution('video2')
    assert response['selected']['width']==1280 and response['selected']['height']==1024
    assert all(m['width']!=1920 for m in response['options'])


@pytest.mark.parametrize('body,status',[
    ({'mode_id':'1920x1080-MJPG-30'},200), ({'mode_id':True},422),
    ({'mode_id':30},422), ({'mode_id':''},422), ({},422),
])
def test_resolution_http_body_validated_before_hardware(body,status,monkeypatch):
    import asyncio
    import httpx
    from teleop_control import app as api
    from teleop_control.main import MockController
    calls=[]
    def set_mode(*args):calls.append(args);return {'selected':body}
    async def inline(function,*args):return function(*args)
    monkeypatch.setattr(api.asyncio,'to_thread',inline)
    app=api.create_app(MockController(),cors_origins=[],umi=SimpleNamespace(camera_resolution=set_mode))
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.post('/umi/camera-controls/video2/resolution',json=body)
    response=asyncio.run(request())
    assert response.status_code==status
    assert bool(calls)==(status==200)


def test_profile_preserves_per_camera_sizes_and_generates_recording_guard(tmp_path):
    import yaml
    setup=load('_resolution_setup',VENDOR/'tools/web_device_config.py')
    build=load('_resolution_build',VENDOR/'galaxis_bringup/tools/build_runtime_configs.py')
    modes=parse_modes(LISTING)
    selected={'left':ros_parameters(modes[-1]),'right':ros_parameters(modes[0]),'center':ros_parameters(next(m for m in modes if m['width']==1280))}
    profile=setup.build_collection_profile('dual',('left','right','center'),selected)
    path=tmp_path/'profile.yaml';path.write_text(yaml.safe_dump(profile,default_flow_style=None))
    loaded=build.load_collection_profile(path)
    overrides=build.load_camera_overrides(path)
    nodes={f'{side}_camera/usb_cam':{'ros__parameters':{}} for side in selected}
    result=build.apply_camera_overrides(nodes,overrides)
    assert result['left_camera/usb_cam']['ros__parameters']['image_width']==1920
    assert result['center_camera/usb_cam']['ros__parameters']['image_width']==1280
    gate=build.apply_collection_profile('recording_gate.yaml',{'groups':{}},loaded)
    group=gate['groups']['camera_quality']
    assert group['default_type']=='topic_condition'
    assert group['conditions']['left_image_30fps']['topic']=='/left_camera/image_raw/compressed'
    assert group['conditions']['left_image_30fps']['rate_escalation']==0
    assert group['conditions']['left_image_30fps']['min_rate_hz']==27.0
    assert '1920' in group['conditions']['left_image_size']['condition']
    selected['left']['framerate']=15
    with pytest.raises(ValueError):setup.build_collection_profile('dual',('left','right','center'),selected)


@pytest.mark.parametrize('rate',[29.97,30.0,30.3])
def test_real_cadence_accepts_normal_camera_timing(rate):
    c=CameraCadence()
    for i in range(125):c.push(i/rate,round(i/rate*1e9))
    assert c.failure(124/rate)==''


def test_cadence_blocks_warmup_slow_stream_and_source_stall_hidden_by_arrival():
    c=CameraCadence();c.push(0,0)
    assert c.failure(0)
    for i in range(1,100):c.push(i/20,round(i/20*1e9))
    assert '帧率' in c.failure(99/20)
    c=CameraCadence()
    for i in range(125):c.push(i/30,round(i/30*1e9))
    c.push(125/30,round(125/30*1e9)+300_000_000)
    assert c.failure(125/30)


@pytest.mark.parametrize('timestamps',[[0,.033,.033,.066],[0,.033,.02],[0,.033,.2], [i/20 for i in range(100)]])
def test_final_validation_rejects_duplicates_reversal_gap_or_low_fps(timestamps):
    assert timing_failure(timestamps)


def test_local_slow_segment_is_not_hidden_by_a_good_overall_average():
    times=[i/40 for i in range(400)]
    times += [times[-1]+(i+1)/20 for i in range(80)]
    assert (len(times)-1)/(times[-1]-times[0])>30
    assert '帧率' in timing_failure(times)


def test_transient_jitter_warns_without_stopping_and_sustained_low_rate_stops():
    limits=dict(min_rate_hz=29.,stop_min_rate_hz=27.,stop_low_rate_sec=2.,
                rate_window_sec=3.,max_frame_gap_sec=1.,warn_frame_gap_sec=.2)
    times=[i/30 for i in range(120)]
    times += [times[-1]+.3+i/30 for i in range(120)]
    ns=[round(t*1e9) for t in times]
    problem,warnings=recording_timing_result(ns,ns,limits)
    assert not problem and warnings
    # A single 300 ms hiccup must also leave the live hard-stop guard open.
    guard=CameraCadence(27.,1.,3.,2.)
    for t,stamp in zip(times,ns):
        guard.push(t,stamp)
        if t>=3:assert not guard.failure(t)
    ns=[i*50_000_000 for i in range(160)]
    problem,_=recording_timing_result(ns,ns,limits)
    assert '帧率' in problem


def test_twenty_nine_point_ninety_seven_never_fails_count_window_rounding():
    guard=CameraCadence(27.,1.,3.,2.)
    for i in range(2000):
        t=i/29.97
        guard.push(t,round(t*1e9))
        if t>=3:assert not guard.failure(t)


def test_capture_checks_decoded_pixels_and_driver_fps_without_upscaling(monkeypatch):
    # A driver silently ignores 1080p. The preview must fail instead of resizing
    # a 640x480 frame and reporting a successful high-resolution selection.
    from threading import Event
    returned=Event()
    class Capture:
        def isOpened(self):return True
        def set(self,*_):return True
        def get(self,*_):return 30
        def read(self):returned.set();return True,SimpleNamespace(shape=(480,640,3))
        def release(self):pass
    cv=SimpleNamespace(VideoCapture=lambda *_:Capture(),VideoWriter_fourcc=lambda *_:1,
        CAP_V4L2=0,CAP_PROP_FOURCC=1,CAP_PROP_FRAME_WIDTH=2,CAP_PROP_FRAME_HEIGHT=3,
        CAP_PROP_FPS=4,CAP_PROP_BUFFERSIZE=5)
    monkeypatch.setitem(sys.modules,'cv2',cv)
    worker=umi._CameraPreviewWorker('/dev/video0',lambda _:None,parse_modes(LISTING)[-1])
    try:
        assert returned.wait(2)
        assert not worker.wait_for_frame(.2)
        assert '640×480' in worker.error and '1920×1080' in worker.error
    finally:worker.stop()
