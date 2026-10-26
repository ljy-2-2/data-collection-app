"""Delayed callbacks and rapid navigation, using synthetic sessions only."""
import os
from pathlib import Path
import sys

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from PySide6.QtCore import QCoreApplication
from qt_ui.bridge import Bridge


@pytest.fixture
def bridge(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    b = Bridge({}, demo=True, settings_path=tmp_path)
    b.poll_timer.stop()
    b.update(ready=True)
    yield b
    b.shutdown()


def target(kind, authenticated=False, auto=False):
    return dict(selected=kind, kind=kind, default_kind='cloud', destinations=[],
                api_url='http://127.0.0.1:8000' if kind=='local' else 'https://cloud.test/api',
                web_url='http://127.0.0.1:5173' if kind=='local' else 'https://cloud.test',
                authenticated=authenticated, user={'username':kind},
                preferences=dict(username=kind, has_saved_password=True, auto_login=auto))


@pytest.mark.parametrize('first', ['cloud', 'local'])
def test_repeated_login_screen_switch_does_not_accept_old_session(bridge, first):
    bridge.action=lambda name,path,payload,done,**kw:done(target(payload['kind'], True))
    bridge.login=lambda *a:pytest.fail('Remember password is not auto-login')
    for kind in [first, 'local' if first=='cloud' else 'cloud'] * 10:
        bridge.selectUploadTarget(kind)
        assert bridge.state['uploadTarget']['selected']==kind
        assert not bridge.state['authenticated'] and not bridge.state['username']


def test_explicit_auto_login_still_reauthenticates_selected_platform(bridge):
    bridge.action=lambda name,path,payload,done,**kw:done(target('local',True,True))
    calls=[]
    bridge.login=lambda *args:calls.append(args)
    bridge.selectUploadTarget('local')
    assert calls==[('local','',True,True,True)]
    assert not bridge.state['authenticated']


@pytest.mark.parametrize('late', ['destinations', 'preferences'])
def test_late_startup_callbacks_cannot_restore_previous_platform(bridge, late):
    requests=[]
    bridge.demo=False
    bridge.request=lambda path,*args,**kw:requests.append((path,kw))
    bridge.serviceReady()
    old_destinations=requests.pop()[1]['done']
    if late=='preferences':
        old_destinations(target('cloud'))
        callback=requests.pop()[1]['done']
    else:callback=old_destinations
    bridge.action=lambda name,path,payload,done,**kw:done(target('local'))
    bridge.selectUploadTarget('local')
    bridge.login=lambda *a:pytest.fail('Stale startup must not authenticate')
    callback(target('cloud',True,True) if late=='destinations' else target('cloud',auto=True)['preferences'])
    assert bridge.state['uploadTarget']['selected']=='local'
    assert bridge.state['preferences']['username']=='local'
    assert not bridge.state['authenticated']


@pytest.mark.parametrize('busy', ['login','upload-target','check-umi','configure-umi','start-umi','stop-umi','logout','check','stop'])
def test_busy_actions_do_not_allow_navigation_or_mutate_preflight(bridge,busy):
    bridge.update(authenticated=True,busy=busy,page='umi',umiStep=2,
                  checkedProfile='dual',minima={'left':1.2},cameraLive={'left':True})
    before=dict(bridge.state)
    bridge.action=lambda *a,**kw:pytest.fail('No duplicate action allowed')
    for _ in range(4):
        bridge.navigate('teleop');bridge.navigate('ego');bridge.configureTeleop('changed','tianji')
        bridge.umiNext();bridge.umiBack();bridge.checkUmi();bridge.returnTeleop()
        bridge.selectUploadTarget('cloud');bridge.login('wrong','wrong',False,False,False)
        bridge.setValue('profile','single-right');bridge.setValue('umiTask','changed')
    assert bridge.state==before


def test_login_double_click_and_platform_switch_wait_for_inflight_request(bridge):
    bridge.demo=False;requests=[]
    bridge.request=lambda path,*args,**kw:requests.append((path,args,kw))
    bridge.login('local','synthetic',False,False,False)
    bridge.login('local','synthetic',False,False,False)
    bridge.selectUploadTarget('cloud');bridge.navigate('umi')
    assert len(requests)==1 and requests[0][0]=='/platform/auth/login'
    assert bridge.state['busy']=='login' and not bridge.state['authenticated']
    bridge.poll=lambda:None
    requests[0][1][1](dict(authenticated=True,user=dict(username='local')))
    assert bridge.state['authenticated'] and bridge.state['username']=='local'


def test_old_poll_failure_cannot_overwrite_state_after_stop(bridge):
    bridge.demo=False;requests=[]
    bridge.update(authenticated=True,teleopStep=2,robot='tianji')
    bridge.request=lambda path,*args,**kw:requests.append((path,kw))
    bridge.poll()
    callback=next(kw['failed'] for path,kw in requests if path=='/tianji/status')
    bridge.revision+=1
    bridge.update(tianji={'state':'idle','devices':[]})
    callback('late timeout')
    assert bridge.state['tianji']['state']=='idle'


@pytest.mark.parametrize('entry',['configure','navigate'])
@pytest.mark.parametrize('late',['done','failed'])
def test_tianji_reentry_clears_notices_and_rejects_old_status(bridge,entry,late):
    bridge.demo=False;requests=[]
    bridge.request=lambda path,*args,**kw:requests.append((path,kw))
    old_status=dict(state='idle',error='上次设备错误',camera_error='上次预览错误',
                    collection={'upload':{'error':'上传待重试'}})
    bridge.update(authenticated=True,teleopStep=2,robot='tianji',tianji=old_status,
                  error='上次操作错误',notice='上次操作提示',
                  cameraApplyError='上次相机参数失败',cameraApplyMessage='相机参数应用失败')
    bridge.poll()
    old=next(kw for path,kw in requests if path=='/tianji/status')
    if entry=='configure':
        bridge.update(teleopStep=1)
        bridge.configureTeleop('重新进入','tianji')
        bridge.save_timer.stop()
    else:
        bridge.navigate('umi');bridge.navigate('teleop')
    assert all(not bridge.state[key] for key in ('error','notice','cameraApplyError','cameraApplyMessage'))
    assert not bridge.state['tianjiStatusFresh']
    assert bridge.state['tianji']==old_status, 'Keep actual device/recording state intact'
    old[late](dict(state='error',error='旧请求返回') if late=='done' else '旧请求超时')
    assert not bridge.state['tianjiStatusFresh']
    assert bridge.state['tianji']==old_status
    bridge.last_poll.clear();bridge.poll()
    current=next(kw for path,kw in reversed(requests) if path=='/tianji/status')
    current['done'](dict(state='error',error='当前设备故障'))
    assert bridge.state['tianjiStatusFresh']
    assert bridge.state['tianji']['error']=='当前设备故障'


def test_reentry_does_not_erase_camera_apply_in_progress(bridge):
    bridge.update(authenticated=True,page='umi',cameraApplyBusy=True,
                  cameraApplyMessage='正在应用相机参数…')
    bridge.navigate('teleop')
    assert bridge.state['cameraApplyBusy']
    assert bridge.state['cameraApplyMessage']=='正在应用相机参数…'


@pytest.mark.parametrize('state',['idle','error'])
def test_zero_recovery_available_without_vr_or_normal_device_precheck(bridge,state):
    bridge.update(authenticated=True,robot='tianji',busy='',selectedPose='zero',
                  paramSaved=True,paramSaving=False,tianji=dict(state=state,joints={},devices=[]))
    calls=[]
    bridge.action=lambda *a,**kw:calls.append(a)
    bridge.restorePose()
    assert calls==[('move','/tianji/move',dict(kind='zero',mode='dual'))]


@pytest.mark.parametrize('state',['running','moving','unknown','checking'])
def test_zero_recovery_cannot_start_during_unconfirmed_or_active_control(bridge,state):
    bridge.update(authenticated=True,robot='tianji',busy='',selectedPose='zero',
                  paramSaved=True,paramSaving=False,tianji=dict(state=state,joints={},devices=[]))
    bridge.action=lambda *a,**kw:pytest.fail('Recovery must not interrupt active or unconfirmed motion')
    bridge.restorePose()
