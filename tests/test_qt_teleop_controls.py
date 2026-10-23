"""Exercise teleop controls with synthetic state and no hardware requests."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 unavailable")
class TeleopControlsTest(unittest.TestCase):
    def test_manual_motion_lock_icons_and_compact_popup(self):
        root = Path(__file__).resolve().parents[1]
        source = r'''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PySide6.QtCore import QPointF, QUrl, Qt
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
            from PySide6.QtTest import QTest
            from shiboken6 import delete
            from qt_ui.bridge import Bridge
            from qt_ui.visuals import CameraItem, CurveItem, G2View, RobotScene

            app = QGuiApplication([])
            for name, cls in [('CameraItem', CameraItem), ('CurveItem', CurveItem), ('G2View', G2View)]:
                qmlRegisterType(cls, 'Collection.Native', 1, 0, name)
            with TemporaryDirectory() as config:
                bridge = Bridge({}, demo=True, settings_path=config)
                bridge.poll_timer.stop()
                bridge.request = lambda *a, **kw: None
                calls = []
                bridge.action = lambda *a, **kw: calls.append(a)
                devices = [dict(id=k, status='connected', label=k) for k in ('robot','left','right')]
                def state(status='idle', busy=''):
                    bridge.update(authenticated=True, robot='tianji', page='teleop', teleopStep=1,
                                  paramSaved=True, busy=busy,
                                  tianji=dict(state=status, devices=devices, cameras=[],
                                              joints=dict(left=[0]*7, right=[0]*7), collection={}))
                    QTest.qWait(60)
                state()
                scene = RobotScene(Path.cwd(), load_model=False)
                engine = QQmlApplicationEngine()
                warnings = []
                engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
                for key, value in [('backend', bridge), ('systemDark', False), ('robotScene', scene)]:
                    engine.rootContext().setContextProperty(key, value)
                engine.loadData(b"""
                    import QtQuick
                    import QtQuick.Controls
                    ApplicationWindow {
                        id: app; width: 1440; height: 1100; visible: true
                        Teleop {width: parent.width}
                        CameraResolution {
                            objectName: "resolution"; x: 20; y: 920; width: 300
                            scope: "tianji"; available: true; editable: true
                            details: ({loaded:true, editable:true, selected:{id:"a",label:"640 x 480"},
                                       options:[{id:"a",label:"640 x 480"},{id:"b",label:"1920 x 1080"}]})
                        }
                        CameraBrightness {
                            objectName: "brightness"; x: 340; y: 920; width: 300
                            scope: "tianji"; available: true; editable: true
                            details: ({loaded:true,supported:true,min:-64,max:64,step:1,value:0,default:0})
                        }
                    }
                """, QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/TeleopControlsTest.qml').resolve())))
                assert engine.rootObjects(), warnings
                window = engine.rootObjects()[0]
                def walk(obj):
                    yield obj
                    for child in obj.childItems():
                        yield from walk(child)
                def item(name, parent=None):
                    return next(o for o in walk(parent or window.contentItem()) if o.objectName()==name)
                def click(control):
                    point = control.mapToScene(QPointF(control.width()/2,control.height()/2)).toPoint()
                    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
                    QTest.qWait(60)
                start = item('teleopStartStop')
                check = item('teleopCheck')
                assert start.property('enabled')
                for status, busy in [('idle','move'), ('error','move'), ('moving',''), ('moving','stop')]:
                    state(status,busy)
                    assert start.property('text')=='开始遥操作'
                    assert start.property('glyph')=='Play'
                    assert not start.property('enabled') and not start.property('busy')
                    assert not start.property('danger')
                state('moving')
                assert item('restore-selected-pose').property('text')=='停止'
                assert item('restore-selected-pose').property('enabled')
                for status in ['running', 'unknown', 'error']:
                    state(status)
                    assert start.property('enabled') and start.property('glyph')=='Square'
                    click(start)
                    assert calls[-1][0]=='stop'
                state('running','stop')
                assert not start.property('enabled')
                # Repeated busy transitions must never rotate the regular icons.
                for control, busy in [(check,'check'), (start,'start'), (start,'stop')]*2:
                    state('running' if busy=='stop' else 'idle',busy)
                    QTest.qWait(170)
                    spinner = item('buttonSpinner',control)
                    glyph = item('buttonGlyph',control)
                    assert spinner.property('visible') and not glyph.property('visible')
                    before = glyph.mapToScene(QPointF(8,8))
                    state('running' if busy=='stop' else 'idle')
                    assert not spinner.property('visible') and glyph.property('visible')
                    assert glyph.property('rotation')==0
                    assert glyph.mapToScene(QPointF(8,8))==before
                    center = control.mapToScene(QPointF(control.width()/2,control.height()/2))
                    assert abs(before.y()-center.y())<=0.5
                state()
                resolution = item('resolution')
                brightness = item('brightness')
                for control in [resolution,brightness]:
                    control.setProperty('editable',False)
                    QTest.qWait(30)
                    assert control.property('implicitHeight')==28
                    assert not any('锁定' in str(o.property('text') or '') for o in walk(control))
                    control.setProperty('editable',True)
                select = item('cameraResolutionSelect',resolution)
                click(select)
                options = [o for o in walk(window.contentItem())
                           if o.property('text') in ('640 x 480','1920 x 1080')
                           and o.parentItem().height()==24]
                assert len(options)==2, [(o.property('text'),o.parentItem().height()) for o in walk(window.contentItem()) if o.property('text') in ('640 x 480','1920 x 1080')]
                assert all(o.property('font').pixelSize()==10 for o in options)
                assert not warnings, warnings
                window.close()
                delete(engine)
                bridge.shutdown()
        '''
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)], cwd=root,
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                     PYTHONPATH=str(root / "scripts")),
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
