"""Input-only preflight: fake ADB, clock and XR source; no hardware."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control.kuavo_vr import check_input,headset_status,probe_subprocess,check_with_recovery
from teleop_control.kuavo import KuavoManager
from teleop_control.galaxis import GalaxisHeadsetSetup


def frame(stamp,stale=False,zero=False):
    pose=SimpleNamespace(orientation_xyzw=(0.,0.,0.,0. if zero else 1.))
    return SimpleNamespace(device_timestamp_ns=stamp,stale=stale,head_pose=pose,
        left_controller=SimpleNamespace(pose=pose),right_controller=SimpleNamespace(pose=pose))


@pytest.mark.parametrize('mode,ready',[('advancing',True),('frozen',False),('stale',False),('empty',False),('zero_pose',False)])
def test_only_fresh_valid_advancing_input_is_ready(mode,ready):
    clock=[0.];source=Mock()
    def read():
        if mode=='empty':return None
        return frame(1 if mode=='frozen' else int(clock[0]*1000)+1,mode=='stale',mode=='zero_pose')
    source.read_frame.side_effect=read
    result=check_input(source,clock=lambda:clock[0],sleep=lambda delay:clock.__setitem__(0,clock[0]+delay),duration=.5)
    assert result['vr_ready'] is ready
    source.start.assert_called_once();source.close.assert_called_once()


def test_cleanup_and_sdk_errors_are_not_success():
    source=Mock();source.start.side_effect=RuntimeError('SDK unavailable')
    assert 'SDK unavailable' in check_input(source)['vr_detail']
    source.close.assert_called_once()


@pytest.mark.parametrize('authorized,running,mapping,ready',[(True,True,True,True),(False,True,True,False),(True,False,True,False),(True,True,False,False)])
def test_headset_read_only_checks(authorized,running,mapping,ready):
    commands=[]
    def run(command,**kwargs):
        commands.append(command)
        if command[-1]=='devices':text='List of devices attached\nPICO123\t'+('device' if authorized else 'unauthorized')+'\n'
        elif 'pidof' in command:text='123' if running else ''
        elif command[-1]=='ro.product.manufacturer':text='PICO'
        elif command[-1]=='ro.product.model':text='A9210'
        elif command[-2:]==['reverse','--list']:text='usb tcp:63901 tcp:63901' if mapping else ''
        elif command[-2:]==['dumpsys','power']:text='mWakefulness=Asleep'
        else:raise AssertionError(command)
        return SimpleNamespace(stdout=text,stderr='',returncode=0)
    result=headset_status(GalaxisHeadsetSetup(run_command=run))
    assert result['pico_ready'] is ready
    if ready:assert result['pico_awake'] is False
    assert not any(word in command for command in commands for word in ('monkey','force-stop','install','kill-server','input'))


def test_native_logs_do_not_hide_probe_result(monkeypatch):
    value={'vr_checked':True,'vr_ready':True,'pico_ready':True}
    monkeypatch.setattr('subprocess.run',Mock(return_value=SimpleNamespace(returncode=0,
        stdout='native SDK diagnostics\nKUAVO_VR:'+json.dumps(value)+'\n')))
    assert probe_subprocess()==value


@pytest.mark.parametrize('initial,after',[(True,True),(False,True),(False,False)])
def test_recovery_only_after_missing_input_and_sdk_release(monkeypatch,initial,after):
    events=[]
    service=Mock()
    service.ensure_started.side_effect=lambda:events.append('ensure')
    service.restart.side_effect=lambda:events.append('restart')
    results=iter([initial,after])
    def probe(source):
        events.extend(['input','close'])
        return dict(vr_ready=next(results),vr_detail='no data')
    monkeypatch.setattr('teleop_control.kuavo_vr.check_input',probe)
    result=check_with_recovery(Mock(),service)
    assert result['vr_ready']==(initial or after)
    assert events==(['ensure','input','close'] if initial else ['ensure','input','close','restart','input','close'])


def test_recovery_failure_is_reported_without_success():
    service=Mock();service.ensure_started.side_effect=RuntimeError('receiver unavailable')
    result=check_with_recovery(Mock(),service)
    assert not result['vr_ready'] and 'receiver unavailable' in result['vr_detail']
    service.restart.assert_not_called()


def test_failed_sdk_release_does_not_restart_shared_bridge(monkeypatch):
    service=Mock()
    monkeypatch.setattr('teleop_control.kuavo_vr.check_input',lambda _:dict(vr_ready=False,vr_cleanup_failed=True))
    assert not check_with_recovery(Mock(),service)['vr_ready']
    service.restart.assert_not_called()


def test_wake_only_after_authorized_pico_verification():
    headset=Mock();headset._serial='PICO123';headset.probe_ready.return_value=False
    headset.last_error='unauthorized'
    headset_status(headset,wake=True)
    headset.wake.assert_not_called()
    headset.probe_ready.return_value=True
    headset._run.return_value=SimpleNamespace(returncode=0,stdout='not a headset')
    headset_status(headset,wake=True)
    headset.wake.assert_not_called()
    headset._run.side_effect=[SimpleNamespace(returncode=0,stdout='PICO'),SimpleNamespace(returncode=0,stdout='A9210'),
        SimpleNamespace(returncode=0,stdout='usb tcp:63901 tcp:63901'),SimpleNamespace(returncode=0,stdout='mWakefulness=Awake')]
    headset.wake.return_value=True
    assert headset_status(headset,wake=True)['pico_ready']
    headset.wake.assert_called_once()


def test_checked_input_survives_idle_but_does_not_claim_live_session(tmp_path):
    manager=KuavoManager(SimpleNamespace(hardware_enabled=False),root=tmp_path)
    manager._merge(dict(vr_checked=True,vr_ready=True,pico_ready=True,pico_sdk_available=True,pico_live=True))
    status=manager.snapshot()
    assert status['vr_ready'] and status['pico_ready']
    assert not status['active'] and not status['pico_live'] and status['state']=='idle'
