"""Exercise the actual Qt editor with an in-memory backend and no hardware."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 unavailable')
class SubtaskConfigUiTest(unittest.TestCase):
    def test_add_rename_reorder_save_cancel_and_delete(self):
        root=Path(__file__).resolve().parents[1]
        source='''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PySide6.QtCore import QUrl, QObject, QMetaObject, QPointF, Qt
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtQuick import QQuickWindow
            from PySide6.QtQml import QQmlApplicationEngine
            from PySide6.QtTest import QTest
            from shiboken6 import delete
            from qt_ui.bridge import Bridge
            app=QGuiApplication([])
            with TemporaryDirectory() as config:
                bridge=Bridge({},demo=True,settings_path=config);bridge.poll_timer.stop()
                bridge.update(authenticated=True)
                engine=QQmlApplicationEngine();warnings=[]
                engine.warnings.connect(lambda rows:warnings.extend(x.toString() for x in rows))
                engine.rootContext().setContextProperty('backend',bridge)
                engine.rootContext().setContextProperty('systemDark',False)
                engine.loadData(b"import QtQuick; import QtQuick.Controls; ApplicationWindow {id:app;width:960;height:720;visible:true;Setup {width:parent.width;layer.enabled:false}}",QUrl.fromLocalFile(str(Path.cwd()/'scripts/qt_ui/qml/SubtaskSmoke.qml')))
                assert engine.rootObjects(),warnings
                window=engine.rootObjects()[0]
                def find(name):
                    pending=[window];visited=set()
                    while pending:
                        item=pending.pop()
                        if item in visited:continue
                        visited.add(item)
                        if item.objectName()==name:return item
                        pending.extend(item.children())
                        if hasattr(item,'childItems'):pending.extend(item.childItems())
                    raise AssertionError(name)
                def invoke(name,method='clicked'):
                    assert QMetaObject.invokeMethod(find(name),method)
                    QTest.qWait(30)
                # Startup must not query the service before it is listening.
                requests=[]
                bridge.demo=False
                bridge.request=lambda path,*args,**kwargs:requests.append((path,kwargs))
                bridge.loadSubtaskConfigs()
                assert not requests
                bridge.serviceReady()
                assert [path for path,_ in requests].count('/subtask-configs')==1
                config_request=next(kwargs for path,kwargs in requests if path=='/subtask-configs')
                config_request['failed']('Connection refused')
                assert bridge.state['subtaskConfigError']
                config_request['done']([])
                assert bridge.state['subtaskConfigError']==''
                bridge.demo=True
                def check_row():
                    QTest.qWait(60)
                    controls=[find(name) for name in ['teleopTask','subtaskConfigSelect','robotType']]
                    points=[control.mapToScene(QPointF(0,0)) for control in controls]
                    assert max(p.y() for p in points)-min(p.y() for p in points)<1,points
                    assert points[0].x()<points[1].x()<points[2].x(),points
                def check_empty_dropdown():
                    for theme,normal,highlight in [('light','#ffffff','#fff7d1'),('dark','#1a1a1a','#362f13')]:
                        bridge.update(theme=theme);QTest.qWait(40)
                        control=find('subtaskConfigSelect')
                        point=control.mapToScene(QPointF(control.property('width')/2,control.property('height')/2)).toPoint()
                        QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(60)
                        option=find('subtaskConfigOption0')
                        label=find('subtaskConfigOptionLabel0')
                        background=find('subtaskConfigOptionBackground0')
                        assert option.property('height')>=34
                        assert label.property('text')=='不使用子任务标注'
                        assert label.property('width')>80 and label.property('height')>=18
                        assert background.property('color').name() in (normal,highlight)
                        center=option.mapToScene(QPointF(option.property('width')/2,18)).toPoint()
                        QTest.mouseMove(window,center);QTest.qWait(60)
                        assert background.property('color').name()==highlight
                        window.grabWindow().save('/tmp/subtask-empty-'+theme+'.png')
                        QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,center);QTest.qWait(40)
                        assert not bridge.state['subtaskConfig']
                    bridge.update(theme='light')
                check_row()
                check_empty_dropdown()
                window.grabWindow().save('/tmp/subtask-setup-row.png')
                invoke('subtaskConfigDialog','open')
                find('subtaskConfigName').setProperty('text','抓放')
                for index,name in enumerate(['pick','move','place']):
                    if index:invoke('addSubtaskCard')
                    field=find('subtaskLabel'+str(index))
                    field.setProperty('text',name);QMetaObject.invokeMethod(field,'textEdited')
                    assert field.property('width')>100
                invoke('subtaskUp2');invoke('subtaskDown1')
                invoke('saveSubtaskConfig')
                assert bridge.state['subtaskConfig']['labels']==['pick','move','place']
                assert not find('subtaskConfigDialog').property('visible')
                check_row()
                identifier=bridge.state['subtaskConfigId']
                invoke('subtaskConfigDialog','open');invoke('addSubtaskCard')
                invoke('cancelSubtaskConfig')
                assert len(bridge.state['subtaskConfigs'])==1
                assert bridge.state['subtaskConfigId']==identifier
                invoke('subtaskConfigDialog','open')
                bridge.saveSubtaskConfig(dict(name='bad',labels=['']))
                assert find('subtaskConfigDialog').property('visible') and bridge.state['subtaskConfigError']
                invoke('cancelSubtaskConfig')
                bridge.deleteSubtaskConfig(identifier)
                assert not bridge.state['subtaskConfig'] and not bridge.state['subtaskConfigs']
                check_empty_dropdown()
                assert not warnings,warnings
                window.close();delete(engine);bridge.shutdown()
        '''
        result=subprocess.run([sys.executable,'-c',textwrap.dedent(source)],cwd=root,
            env={**os.environ,'QT_QPA_PLATFORM':'offscreen','QT_QUICK_BACKEND':'software',
                 'PYTHONPATH':os.pathsep.join([str(root/'scripts'),str(root/'teleop_control/src'),os.environ.get('PYTHONPATH','')])},
            capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
