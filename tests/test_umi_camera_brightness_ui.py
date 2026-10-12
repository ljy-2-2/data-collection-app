"""Native brightness UI and its service slots; no camera or service is contacted."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 unavailable")
class CameraBrightnessTests(unittest.TestCase):
    def run_qt(self, source):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-c", textwrap.dedent(source)], cwd=root,
                                env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(root / "scripts")),
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_slots_keep_camera_state_separate_and_use_actual_readback(self):
        self.run_qt('''
            from tempfile import TemporaryDirectory
            from PySide6.QtCore import QCoreApplication
            from qt_ui.bridge import Bridge
            app = QCoreApplication([])
            with TemporaryDirectory() as settings:
                bridge = Bridge({}, settings_path=settings)
                bridge.poll_timer.stop()
                bridge._state.update(authenticated=True, page='umi', umiStep=2,
                    cameraRoles=dict(left='/dev/video2', center='/dev/video0', right='/dev/video4'))
                calls = []; global_changes = []
                bridge.changed.connect(lambda: global_changes.append(True))
                def request(path, data=None, done=None, **kwargs):
                    calls.append(dict(path=path, data=data, done=done, **kwargs))
                bridge.request = request
                control = dict(supported=True, value=35, min=-30, max=200, step=5, default=20)
                bridge.loadCameraBrightness('video2')
                bridge.loadCameraBrightness('video2')
                assert len(calls) == 1
                assert calls[-1]['path'] == '/umi/camera-controls/video2/brightness'
                assert calls[-1]['method'] == 'GET'
                assert bridge.umiCameraBrightness['video2']['busy']
                calls[-1]['done'](control)
                assert bridge.umiCameraBrightness['video2']['value'] == 35
                bridge.setCameraBrightness('video2', 45)
                assert calls[-1]['method'] == 'POST' and calls[-1]['data'] == dict(value=45)
                calls[-1]['done'](dict(control, value=40))
                assert bridge.umiCameraBrightness['video2']['value'] == 40
                assert bridge.state['busy'] == '' and bridge.state['error'] == ''
                assert global_changes == []
                before = len(calls)
                bridge.setCameraBrightness('video2', 46)
                bridge.setCameraBrightness('video2', 300)
                assert len(calls) == before
                assert bridge.umiCameraBrightness['video2']['error']
                bridge.loadCameraBrightness('video2')
                calls[-1]['done'](dict(control, value=40))
                bridge.setCameraBrightness('video2', 50)
                calls[-1]['failed']('设备暂时不可用')
                assert bridge.umiCameraBrightness['video2']['error'] == '设备暂时不可用'
                assert not bridge.umiCameraBrightness['video2']['busy']
                assert bridge.umiCameraBrightness['video2']['value'] == 40
                bridge.loadCameraBrightness('video0')
                calls[-1]['done'](dict(supported=False, message='不支持亮度'))
                before = len(calls)
                bridge.setCameraBrightness('video0', 0)
                bridge.loadCameraBrightness('unselected-device')
                assert len(calls) == before
                assert bridge.umiCameraBrightness['video0']['supported'] is False
                assert bridge.umiCameraBrightness['video2']['value'] == 40
                bridge._state.update(umiStep=3, profile='single-left', umi=dict(state='starting', monitor_connected=True))
                before = len(calls)
                bridge.loadCameraBrightness('left')
                assert len(calls) == before
                bridge._state['umi']['state'] = 'ready'
                bridge.loadCameraBrightness('left')
                assert calls[-1]['path'] == '/umi/camera-controls/left/brightness'
                calls[-1]['done'](control)
                bridge.setCameraBrightness('left', 20)
                assert calls[-1]['data'] == dict(value=20)
                calls[-1]['done'](dict(control, value=20))
                before = len(calls)
                bridge.loadCameraBrightness('right')
                bridge.loadCameraBrightness('video2')
                assert len(calls) == before
                bridge.loadCameraBrightness('left')
                calls[-1]['done'](dict(control, min=201))
                assert bridge.umiCameraBrightness['left']['error']
                assert bridge.state['busy'] == '' and bridge.state['error'] == ''
                assert global_changes == []
        ''')

    def test_component_uses_hardware_range_and_disables_unavailable_controls(self):
        self.run_qt('''
            from pathlib import Path
            from PySide6.QtCore import QObject, Property, Signal, Slot, QUrl
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtQml import QQmlEngine, QQmlComponent
            from PySide6.QtTest import QTest
            app = QGuiApplication([])
            class Backend(QObject):
                changed = Signal()
                controls = {}
                gets = []
                writes = []
                @Property('QVariantMap', notify=changed)
                def state(self): return dict(theme='light')
                @Property('QVariantMap', notify=changed)
                def umiCameraBrightness(self): return self.controls
                @Slot(str)
                def loadCameraBrightness(self, key): self.gets.append(key)
                @Slot(str, int)
                def setCameraBrightness(self, key, value): self.writes.append((key, value))
            backend = Backend()
            engine = QQmlEngine()
            engine.rootContext().setContextProperty('backend', backend)
            engine.rootContext().setContextProperty('systemDark', False)
            component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/CameraBrightness.qml').resolve())))
            control = component.createWithInitialProperties(dict(cameraKey='video2', cameraName='左腕相机', width=235))
            assert control is not None, [e.toString() for e in component.errors()]
            slider = control.findChild(QObject, 'cameraBrightnessSlider')
            number = control.findChild(QObject, 'cameraBrightnessNumber')
            button = control.findChild(QObject, 'cameraBrightnessDefault')
            assert not slider.property('enabled') and not backend.gets
            control.setProperty('available', True)
            assert backend.gets == ['video2']
            backend.controls = dict(video2=dict(supported=True, loaded=True, value=35, min=-30, max=200, step=5, default=20))
            backend.changed.emit()
            assert slider.property('enabled')
            assert (slider.property('from'), slider.property('to'), slider.property('stepSize')) == (-30, 200, 5)
            assert number.property('text') == '35'
            button.clicked.emit()
            assert backend.writes == [('video2', 20)]
            # A hardware readback replaces a requested value; no global refresh is required.
            backend.controls['video2']['value'] = 25
            backend.changed.emit()
            assert number.property('text') == '25'
            backend.controls['video2']['busy'] = True
            backend.changed.emit()
            assert not slider.property('enabled') and not number.property('enabled')
            backend.controls['video2'].update(busy=False, error='读取失败')
            backend.changed.emit()
            assert not slider.property('enabled') and button.property('text') == '重试'
            backend.controls['video2'] = dict(supported=True, loaded=True, value=10, min=10, max=99, step=6, default=10)
            backend.changed.emit()
            slider.setProperty('value', 99)
            slider.moved.emit()
            QTest.qWait(250)
            assert backend.writes[-1] == ('video2', 94), backend.writes
            backend.controls['video2'] = dict(supported=False, loaded=True, message='此设备不支持亮度')
            backend.changed.emit()
            assert not slider.property('enabled') and button.property('enabled')
            control.setProperty('available', False)
            control.setProperty('cameraKey', 'left')
            control.setProperty('available', True)
            assert backend.gets[-1] == 'left'
            assert not slider.property('enabled')
            control.setParent(None)
        ''')


if __name__ == '__main__':
    unittest.main()
