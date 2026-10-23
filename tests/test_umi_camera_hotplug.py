import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT/'umi_collection/vendor/galaxis'
sys.path[:0] = [str(ROOT/'teleop_control/src'), str(VENDOR/'galaxis-core/galaxis-core')]
from teleop_control import camera_devices, umi
from teleop_control.models import UmiStatus
from galaxis_core.camera_quality import CameraCadence, load_contract, recording_timing_result


def camera(device='/dev/video8', serial='center', port='1-1', generation='first'):
    return dict(device=device, vendor='1234', product='5678', serial=serial,
                usb_path=port, generation=generation, name='Test Camera', hub='1-1')


@pytest.mark.parametrize('profile,expected', [('dual', 3), ('single-left', 2), ('single-right', 2)])
@pytest.mark.parametrize('connected', [False, True])
def test_check_http_executes_real_manager_and_returns_camera_candidates(tmp_path, monkeypatch, profile, expected, connected):
    import asyncio
    import httpx
    from teleop_control.app import create_app
    from teleop_control.main import MockController

    manager = umi.UmiManager(tmp_path, tmp_path/'data', python=sys.executable,
                             runtime_dir=tmp_path/'runtime')
    devices = [camera('/dev/video2'), camera('/dev/video4', serial='left'),
               camera('/dev/video6', serial='right')] if connected else []
    # Replace hardware I/O, but exercise the actual check method and HTTP serialization.
    monkeypatch.setattr(camera_devices, 'discover', lambda: devices)
    monkeypatch.setattr(manager, '_sync_external_stack', lambda: False)
    monkeypatch.setattr(manager, '_monitor_state', lambda: (False, {}))
    app = create_app(MockController(), cors_origins=[], umi=manager)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.post('/umi/check', params={'collection_profile': profile})

    response = asyncio.run(request())
    assert response.status_code == 200, response.text
    check = response.json()['monitor']['device_check']
    assert check['camera_candidates'] == [item['device'] for item in devices]
    assert check['camera_candidate_details'] == devices
    assert len(check['camera_devices']) == check['expected_cameras'] == expected
    assert manager._device_check == check


def test_replug_tracks_identity_across_video_number_and_port_changes():
    old = camera()
    new = camera('/dev/video2', port='1-3.1', generation='second')
    recycled = camera('/dev/video8', serial='different')
    assert camera_devices.match(old, [new, recycled]) == new
    assert camera_devices.match(old, [recycled]) is None
    assert camera_devices.match(old, [new, camera('/dev/video4', port='1-3.2')]) is None
    wrist = camera(serial='', port='1-3.3.1')
    assert camera_devices.match(wrist, [camera('/dev/video4', serial='', port='1-3.3.1')])['device']=='/dev/video4'
    assert camera_devices.match(wrist, [camera('/dev/video6', serial='', port='1-3.3.2')]) is None
    assert camera_devices.resolve_roles({'left':old,'right':old},[new]) == {}


def test_discovery_filters_metadata_even_when_capture_node_is_odd_number(tmp_path):
    sys_class=tmp_path/'class';sys_class.mkdir();dev=tmp_path/'dev';dev.mkdir()
    usb=tmp_path/'usb/1-3.1';usb.mkdir(parents=True)
    for key,value in dict(idVendor='1234', idProduct='5678',serial='unique').items():
        (usb/key).write_text(value)
    for name in ('video9','video10'):
        entry=usb/name;entry.mkdir();(entry/'name').write_text('USB Camera')
        (sys_class/name).symlink_to(entry);(dev/name).touch()
    result=camera_devices.discover(sys_class,dev,lambda path:path.endswith('video9'))
    assert len(result)==1 and result[0]['device'].endswith('video9')
    assert result[0]['stable_id']=='serial:1234:5678:unique'


def test_refresh_removes_stale_worker_without_resetting_encoders_or_other_cameras(tmp_path,monkeypatch):
    manager=umi.UmiManager(tmp_path,tmp_path/'data',python=sys.executable,runtime_dir=tmp_path/'runtime')
    old=camera();new=camera('/dev/video2',port='1-3.1',generation='second')
    wrist=camera('/dev/video4',serial='wrist')
    manager._device_check=dict(camera_candidates=[old['device'],wrist['device']],camera_candidate_details=[old,wrist],encoder_candidates=['a','b'])
    manager._configured_encoder_minima=('dual',dict(left=4.2,right=5.1))
    manager._write_camera_selection(dict(center=camera_devices.identity(old),left=camera_devices.identity(wrist)))
    stopped=[]
    manager._camera_workers={old['device']:SimpleNamespace(stop=lambda:stopped.append('center')),
                             wrist['device']:SimpleNamespace(stop=lambda:stopped.append('left'))}
    monkeypatch.setattr(camera_devices,'discover',lambda:[new,wrist])
    check=manager.refresh_camera_devices(force=True)
    assert check['camera_candidates']==['/dev/video2','/dev/video4']
    assert check['camera_roles']==dict(center='/dev/video2',left='/dev/video4')
    assert stopped==['center'] and '/dev/video4' in manager._camera_workers
    assert manager._configured_encoder_minima==('dual',dict(left=4.2,right=5.1))


def test_live_selection_rejects_recording_and_keeps_contract(tmp_path,monkeypatch):
    manager=umi.UmiManager(tmp_path,tmp_path/'data',python=sys.executable,runtime_dir=tmp_path/'runtime')
    old=camera();new=camera('/dev/video2',serial='replacement')
    mode=dict(width=1280,height=1024,fourcc='MJPG',fps=30)
    manager._expected_camera_modes={'center':mode}
    manager._write_camera_selection({'center':{**camera_devices.identity(old),'brightness':10}})
    monkeypatch.setattr(manager,'status',lambda:UmiStatus(state='recording',recording=True,monitor_connected=True))
    with pytest.raises(RuntimeError):manager.select_live_camera('center','/dev/video2')
    assert camera_devices.read_selection(manager._camera_selection_file)['center']['serial']=='center'
    monkeypatch.setattr(manager,'status',lambda:UmiStatus(state='starting',monitor_connected=True))
    monkeypatch.setattr(manager,'refresh_camera_devices',lambda **kw:dict(camera_candidate_details=[new]))
    monkeypatch.setattr(manager,'_monitor_state',lambda:(True,dict(recording=False)))
    monkeypatch.setattr(umi,'read_modes',lambda _: [mode])
    monkeypatch.setattr(camera_devices,'discover',lambda:[new])
    manager.select_live_camera('center','/dev/video2')
    assert camera_devices.read_selection(manager._camera_selection_file)['center']['serial']=='replacement'
    assert camera_devices.read_selection(manager._camera_selection_file)['center']['brightness']==10
    assert manager._expected_camera_modes=={'center':mode}


def test_legacy_quality_contract_migrates_and_real_gaps_remain_invalid(tmp_path):
    import yaml
    profile=dict(active_cameras=['center'],cameras={'center':{}},camera_quality={'min_rate_hz':29})
    path=tmp_path/'profile.yaml';path.write_text(yaml.safe_dump(profile))
    limits=load_contract(path)['limits']
    assert limits['version']==2
    # 28 fps and a brief 300 ms delay stay usable, with real timestamps unchanged.
    times=[i/28 for i in range(280)]+[10.3+i/30 for i in range(300)]
    stamps=[round(t*1e9) for t in times]
    assert recording_timing_result(stamps,stamps,limits)[0]==''
    times=[i/30 for i in range(200)]+[10+i/30 for i in range(200)]
    stamps=[round(t*1e9) for t in times]
    problem, warnings = recording_timing_result(stamps,stamps,limits)
    assert not problem and warnings
    c=CameraCadence(limits['stop_min_rate_hz'],limits['max_frame_gap_sec'],limits['rate_window_sec'],limits['stop_low_rate_sec'])
    for i in range(360):
        c.push(i/15,round(i/15*1e9));issue=c.failure(i/15)
    assert '帧率' in issue


def test_qt_hotplug_preserves_calibration_and_manual_role_choices():
    source='''
from PySide6.QtGui import QGuiApplication
from qt_ui.bridge import Bridge
import tempfile
app=QGuiApplication([])
with tempfile.TemporaryDirectory() as path:
    b=Bridge({},demo=True,settings_path=path);b.poll_timer.stop()
    old=dict(device='/dev/video8',vendor='v',product='p',serial='center',usb_path='1-1',generation='old')
    new={**old,'device':'/dev/video2','usb_path':'1-3.1','generation':'new'}
    b._state.update(umiStep=2,minima=dict(left=4.2,right=5.1),cameraRoles={'center':'/dev/video8'})
    b._camera_role_identities={'center':old}
    b.reconcile_camera_devices({'monitor':{'device_check':{'camera_candidate_details':[]}}})
    assert b.state['cameraRoles']['center']==''
    b.reconcile_camera_devices({'monitor':{'device_check':{'camera_candidate_details':[new]}}})
    assert b.state['cameraRoles']['center']=='/dev/video2'
    assert b.state['minima']==dict(left=4.2,right=5.1)
    recycled={**old,'serial':'different'}
    b.reconcile_camera_devices({'monitor':{'device_check':{'camera_candidate_details':[recycled]}}})
    assert b.state['cameraRoles']['center']==''
'''
    env={**os.environ,'QT_QPA_PLATFORM':'offscreen','PYTHONPATH':str(ROOT/'scripts')}
    result=subprocess.run([sys.executable,'-c',source],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
