"""Serialize local parameter writes using fake RPCs; no device access."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_coalesced_autosave_keeps_latest_edit_and_reports_failures(tmp_path):
    root=Path(__file__).resolve().parents[1]
    code=textwrap.dedent('''
        import tempfile
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtTest import QTest
        from qt_ui.bridge import Bridge
        app=QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            bridge=Bridge({},demo=True,settings_path=directory)
            bridge.poll_timer.stop();bridge.demo=False
            bridge.update(authenticated=True,robot='kuavo-5w')
            requests=[]
            bridge.request=lambda path,data,done,**kw:requests.append((path,data,done,kw['failed']))
            bridge.setValue('kuavoPositionScale',.6)
            bridge.setValue('kuavoPositionScale',.5)
            bridge.setValue('kuavoVelocity',.4)
            QTest.qWait(450)
            assert len(requests)==1 and requests[0][0]=='/kuavo/motion'
            assert requests[0][1]['position_scale']==.5
            assert bridge.state['kuavoMotionSaving']
            bridge.setValue('kuavoPositionScale',.3)
            QTest.qWait(450)
            assert len(requests)==1, 'never overlap writes'
            def succeed(request):
                request[2]({'motion_settings':{'values':request[1],'supported':False,'saved':True}})
            succeed(requests[0])
            assert bridge.state['kuavoPositionScale']==.3 and bridge.state['kuavoMotionPending']
            QTest.qWait(450)
            assert len(requests)==2 and requests[1][1]['position_scale']==.3
            requests[1][3]('disk full')
            assert bridge.state['kuavoMotionPending'] and 'disk full' in bridge.state['kuavoMotionError']
            QTest.qWait(450)
            assert len(requests)==2, 'do not repeatedly retry the same failed write'
            bridge.setValue('kuavoAcceleration',4.)
            QTest.qWait(450)
            assert len(requests)==3 and requests[2][1]['planning_acceleration_rad_s2']==4.
            succeed(requests[2])
            assert not bridge.state['kuavoMotionSaving'] and not bridge.state['kuavoMotionPending']
            assert not bridge.state['kuavoMotionError']
            bridge.update(kuavo={**bridge.state['kuavo'],'active':True,'state':'running'})
            bridge.setValue('kuavoPositionScale',.8)
            QTest.qWait(450)
            assert len(requests)==3 and bridge.state['kuavoPositionScale']==.3
            bridge.shutdown()
    ''')
    env={**os.environ,'PYTHONPATH':os.pathsep.join((str(root/'scripts'),str(root/'teleop_control/src'),os.environ.get('PYTHONPATH','')))}
    result=subprocess.run([sys.executable,'-c',code],cwd=root,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_robot_parameter_autosave_scope_ranges_revision_and_readback(tmp_path):
    root=Path(__file__).resolve().parents[1]
    code=textwrap.dedent('''
        import json,copy,tempfile
        from pathlib import Path
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtTest import QTest
        from qt_ui.bridge import Bridge
        from teleop_control.kuavo_motion_controls import motion_controls
        app=QCoreApplication([])
        folder=Path('tests/fixtures/kuavo_settings')
        form=dict(values=json.loads((folder/'snapshot.json').read_text())['values'],schema=json.loads((folder/'schema.json').read_text()),
                  revision=2,settings_sha256='c'*64,service_instance_id='a'*32,installation_sha256='b'*64,editable=True)
        form['values']['right_arm_scale']=.4
        def state():return dict(state='ready',active=True,prestart_editable=True,parameter_mode='service',session_settings=copy.deepcopy(form),motion_controls=motion_controls(form))
        with tempfile.TemporaryDirectory() as directory:
            bridge=Bridge({},demo=True,settings_path=directory);bridge.poll_timer.stop();bridge.demo=False
            bridge.update(authenticated=True,robot='kuavo-5w',kuavo=state(),busy='check')
            requests=[]
            bridge.request=lambda path,data,done,**kw:requests.append((path,data,done,kw['failed']))
            assert bridge.state['kuavoArmMode']=='dual'
            bridge.setValue('kuavoArmMode','right')
            assert bridge.state['kuavoPositionScale']==.4
            bridge.setValue('kuavoPositionScale',1.5)
            bridge.setValue('kuavoVelocity',0.)
            assert bridge.state['kuavoVelocity']==.7
            bridge.setValue('kuavoArmMode','left')
            assert bridge.state['kuavoArmMode']=='right', 'pending edit must not change scope'
            form['revision']=8;bridge.update(kuavo=state())
            QTest.qWait(450)
            assert requests[-1][0]=='/kuavo/session-settings'
            payload=requests[-1][1]
            assert payload['changes']=={'right_arm_scale':1.5}
            assert payload['expected_revision']==2, 'poll must not silently rebase edit'
            assert payload['action']=='apply' and payload['confirm']
            bridge.setValue('kuavoPositionScale',.2)
            assert bridge.state['kuavoPositionScale']==1.5, 'do not edit during remote write'
            requests[-1][3]('revision conflict')
            QTest.qWait(450)
            assert len(requests)==1 and bridge.state['kuavoMotionPending']
            assert bridge.state['kuavo']['session_settings'] is None
            bridge.kuavo_motion_base=None;bridge.kuavo_motion_fields.clear()
            bridge.update(kuavo=state(),kuavoMotionPending=False,kuavoMotionError='')
            bridge.setValue('kuavoArmMode','dual_base')
            bridge.setValue('kuavoVelocity',.9)
            QTest.qWait(450)
            payload=requests[-1][1]
            assert payload['changes']=={'left_arm_speed_rad_s':.9,'right_arm_speed_rad_s':.9}
            assert payload['expected_revision']==8
            form['values'].update(payload['changes']);form['revision']=9
            requests[-1][2](state())
            assert not bridge.state['kuavoMotionPending'] and not bridge.state['kuavoMotionSaving']
            assert bridge.state['kuavoVelocity']==.9
            assert bridge.state['kuavo']['session_settings']['revision']==9
            assert form['values']['right_arm_scale']==.4, 'only edited fields may be saved'
            bridge.shutdown()
    ''')
    env={**os.environ,"PYTHONPATH":os.pathsep.join((str(root/"scripts"),str(root/"teleop_control/src"),os.environ.get("PYTHONPATH","")))}
    result=subprocess.run([sys.executable,"-c",code],cwd=root,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
