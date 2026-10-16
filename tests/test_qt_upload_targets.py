import os
import sys
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from PySide6.QtCore import QCoreApplication
from qt_ui.bridge import Bridge


def test_switch_keeps_preflight_and_requires_destination_login(tmp_path):
    app=QCoreApplication.instance() or QCoreApplication([])
    b=Bridge({'PLATFORM_WEB_URL':'https://cloud.test'},demo=True,settings_path=tmp_path)
    b.update(ready=True,authenticated=True,page='umi',umiStep=2,minima={'left':1.25})
    def action(name,path,payload,done,**kwargs):
        assert path=='/platform/destinations' and payload=={'kind':'local'}
        done(dict(selected='local',default_kind='cloud',kind='local',api_url='http://127.0.0.1:8000',web_url='http://127.0.0.1:5173',destinations=[],authenticated=False,preferences={}))
    b.action=action
    b.selectUploadTarget('local')
    assert b.state['umiStep']==2 and b.state['minima']=={'left':1.25}
    assert not b.state['authenticated']
    assert b.platformLocation=='本地' and b.platformWebUrl=='http://127.0.0.1:5173'
    b.poll_timer.stop()


def test_switch_never_runs_during_recording(tmp_path):
    app=QCoreApplication.instance() or QCoreApplication([])
    b=Bridge({},demo=True,settings_path=tmp_path)
    b.update(umi={'state':'recording'})
    b.action=lambda *args,**kw:(_ for _ in ()).throw(AssertionError('must not switch'))
    b.selectUploadTarget('local')
    assert '结束采集' in b.state['error']
    b.poll_timer.stop()


def test_navigation_preserves_local_login_and_destination_for_teleop(tmp_path):
    app=QCoreApplication.instance() or QCoreApplication([])
    b=Bridge({},demo=True,settings_path=tmp_path)
    target=dict(selected='local',default_kind='cloud',api_url='http://127.0.0.1:8000',web_url='http://127.0.0.1:5173')
    b.update(ready=True,authenticated=True,username='local-user',uploadTarget=target,page='umi',umiStep=2,teleopStep=2)
    b.selectUploadTarget=lambda *args:(_ for _ in ()).throw(AssertionError('navigation must not switch accounts'))
    for _ in range(3):
        b.navigate('teleop')
        assert b.state['page']=='teleop' and b.state['teleopStep']==2
        assert b.state['authenticated'] and b.state['username']=='local-user'
        assert b.state['uploadTarget']==target
        b.navigate('umi')
        assert b.state['umiStep']==2 and b.state['authenticated']
    b.poll_timer.stop()
