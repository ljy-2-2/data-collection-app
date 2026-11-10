"""Task drafts survive polling/restart and text does not resize setup controls."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_task_draft_and_fixed_setup_widths():
    root = Path(__file__).resolve().parents[1]
    source = r'''
        import os,sys,subprocess,json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from PySide6.QtCore import QUrl,Qt,QPointF
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQuick import QQuickWindow
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtTest import QTest
        from shiboken6 import delete
        from qt_ui.bridge import Bridge
        app=QGuiApplication([])
        with TemporaryDirectory() as config:
            bridge=Bridge({},demo=True,settings_path=config);bridge.poll_timer.stop()
            bridge.request=lambda *a,**kw:None
            engine=QQmlApplicationEngine();warnings=[]
            engine.warnings.connect(lambda rows:warnings.extend(x.toString() for x in rows))
            engine.rootContext().setContextProperty('backend',bridge)
            engine.rootContext().setContextProperty('systemDark',False)
            engine.loadData(b'import QtQuick;import QtQuick.Controls;ApplicationWindow {id:app;width:960;height:720;visible:true;Setup {width:parent.width;layer.enabled:false}}',QUrl.fromLocalFile(str(Path.cwd()/'scripts/qt_ui/qml/TaskFieldsTest.qml')))
            assert engine.rootObjects(),warnings
            window=engine.rootObjects()[0]
            def walk(item):
                yield item
                for c in item.childItems():yield from walk(c)
            def find(name):return next(i for i in walk(window.contentItem()) if i.objectName()==name)
            task=find('teleopTask');select=find('subtaskConfigSelect')
            def restart_expect(value):
                code='from PySide6.QtCore import QCoreApplication;from qt_ui.bridge import Bridge;import sys,json;a=QCoreApplication([]);b=Bridge({},demo=True,settings_path=sys.argv[1]);assert b.state["task"]==json.loads(sys.argv[2]),repr(b.state["task"])'
                subprocess.run([sys.executable,'-c',code,config,json.dumps(value)],check=True,capture_output=True,timeout=10)
            task.forceActiveFocus();QTest.keyClick(window,Qt.Key_A,Qt.ControlModifier);QTest.keyClick(window,Qt.Key_Backspace)
            for i in range(5):bridge.update(ready=bool(i%2));QTest.qWait(20)
            assert task.property('text')=='' and bridge.state['task']==''
            assert not find('teleopNext').isEnabled()
            restart_expect('')
            for char in 'pick_and_place_06':QTest.keyClick(window,char)
            QTest.qWait(30)
            assert task.property('text')==bridge.state['task']=='pick_and_place_06'
            restart_expect('pick_and_place_06')
            def geometry():
                return [(v.mapToScene(QPointF(0,0)).x(),v.width(),v.height()) for v in (task,select,find('robotType'),find('newSubtaskConfig'))]
            for width in (1280,960,740,480):
                window.setWidth(width)
                bridge.setValue('task','short');bridge.update(subtaskConfigs=[dict(id='one',name='short',labels=['one'])]);bridge.selectSubtaskConfig('one');QTest.qWait(50)
                before=geometry()
                bridge.setValue('task','长任务名称'*24);bridge.update(subtaskConfigs=[dict(id='one',name='长配置名称'*24,labels=['这是较长的子任务说明'*10])]);bridge.selectSubtaskConfig('one')
                for i in range(4):bridge.update(ready=bool(i%2));QTest.qWait(20)
                assert geometry()==before,(width,before,geometry())
                assert all(w>0 for x,w,h in before)
                assert all(x+w<=width for x,w,h in before)
            restart_expect('长任务名称'*24)
            assert not warnings,warnings
            window.close();delete(engine);bridge.shutdown()
    '''
    p = subprocess.run([sys.executable, '-c', textwrap.dedent(source)], cwd=root,
        env=dict(os.environ, PYTHONPATH=str(root/'scripts')+os.pathsep+str(root/'teleop_control/src'),
                 QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software'),
        capture_output=True, text=True, timeout=35)
    assert p.returncode == 0, p.stdout+p.stderr
