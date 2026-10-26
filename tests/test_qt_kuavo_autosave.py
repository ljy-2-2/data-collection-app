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
