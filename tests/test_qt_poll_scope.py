"""Do not redraw an unused robot panel while preserving active control polling."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_umi_polling_skips_unused_robot_but_keeps_active_feedback():
    script = textwrap.dedent('''
        import tempfile
        from PySide6.QtGui import QGuiApplication
        from qt_ui.bridge import Bridge
        app=QGuiApplication([])
        with tempfile.TemporaryDirectory() as config:
            bridge=Bridge({},settings_path=config)
            bridge.poll_timer.stop()
            requested=[]
            bridge.request=lambda path,**kwargs:requested.append(path)
            bridge.update(authenticated=True,page='umi',umiStep=1)
            def poll():
                requested.clear();bridge.pending.clear();bridge.last_poll.clear();bridge.poll()
                return set(requested)
            assert poll()=={'/umi/status'}
            bridge.update(robot='agibot-g2',teleopStep=2)
            assert {'/status','/preview'} <= poll()
            bridge.update(robot='tianji',teleopStep=1,g2={'session':{'state':'running'}})
            assert '/status' in poll(), 'Active G2 feedback must not disappear on another page'
            bridge.update(teleopStep=2,g2={'session':{'state':'idle'}})
            assert '/tianji/status' in poll(), 'Tianji watchdog access must remain active'
            bridge.shutdown()
    ''')
    root=Path(__file__).resolve().parents[1]
    env=dict(os.environ,QT_QPA_PLATFORM='offscreen',PYTHONPATH=str(root/'scripts'))
    result=subprocess.run([sys.executable,'-c',script],cwd=root,env=env,
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
