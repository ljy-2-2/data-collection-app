"""Render native UMI sensor components offline, isolated from other Qt tests."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class UmiDisplayTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 unavailable")
    def test_sensor_and_curve_render_degrees_while_calibration_stays_radians(self):
        source = textwrap.dedent('''
            import math
            from pathlib import Path
            from PySide6.QtCore import QObject, Property, QUrl, Signal
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtQml import QQmlComponent, QQmlEngine, qmlRegisterType
            from qt_ui.visuals import CurveItem

            app = QGuiApplication([])
            class Backend(QObject):
                changed = Signal()
                raw = math.pi
                zero = math.pi / 2
                position = math.pi / 6
                @Property('QVariantMap', notify=changed)
                def state(self):
                    return dict(theme='light', busy='',
                        encoders=dict(left=dict(online=True, value=self.raw)),
                        encoderHistory=dict(left=[0, self.raw]),
                        minima=dict(left=self.zero))
                @Property('QVariantMap', notify=changed)
                def umiTelemetry(self):
                    return dict(monitor_connected=True,
                        encoders=dict(left=dict(online=True, rate_hz=100, position=self.position)))
                @Property('QVariantMap', notify=changed)
                def umiHistory(self):
                    return dict(left=[0, 2 * self.position])
            backend = Backend()
            qmlRegisterType(CurveItem, 'Collection.Native', 1, 0, 'CurveItem')
            engine = QQmlEngine()
            engine.rootContext().setContextProperty('backend', backend)
            engine.rootContext().setContextProperty('systemDark', False)
            component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/Sensor.qml').resolve())))
            sensor = component.create()
            assert sensor is not None, [e.toString() for e in component.errors()]
            def texts():
                return [item.property('text') for item in sensor.findChildren(QObject) if item.property('text') is not None]
            assert '180.00°' in texts(), texts()
            assert '原始角度' in texts(), texts()
            assert '已闭合到最小值（原始零点 90.00°）' in texts(), texts()
            assert backend.state['minima']['left'] == math.pi / 2
            sensor.setProperty('monitoring', True)
            assert sensor.findChild(CurveItem).zeroBased
            assert '总开合角' in texts(), texts()
            for value, expected in [(math.pi / 6, '60.00°'), (0, '0.00°'), (-0.0, '0.00°'), (-1e-8, '0.00°')]:
                backend.position = value
                backend.changed.emit()
                assert expected in texts(), texts()
                assert '-0.00°' not in texts(), texts()
                assert sensor.property('number') == 2 * value
                assert sensor.findChild(CurveItem).values[-1] == 2 * value
            sensor.setProperty('monitoring', False)
            assert not sensor.findChild(CurveItem).zeroBased
            backend.raw = -math.pi / 3
            backend.zero = -math.pi / 2
            backend.changed.emit()
            assert '-60.00°' in texts(), texts()
            assert '已闭合到最小值（原始零点 -90.00°）' in texts(), texts()
            assert sensor.findChild(CurveItem).values[-1] == backend.raw

            class Painter:
                def __init__(self): self.labels = []; self.path = None
                def setPen(self, *args): pass
                def setFont(self, *args): pass
                def drawLine(self, *args): pass
                def drawText(self, *args): self.labels.append(args[-1])
                def drawPath(self, path): self.path = path
            curve = CurveItem()
            curve.setWidth(300); curve.setHeight(136)
            raw = [0, math.pi / 2, None, math.pi]
            curve.values = raw
            painter = Painter(); curve.paint(painter)
            assert painter.labels == ['201.60°', '90.00°', '-21.60°'], painter.labels
            assert curve.values == raw
            assert painter.path.elementCount() == 3
            # Small physical motion must remain visible on the degree axis.
            curve.values = [0, .001]
            painter = Painter(); curve.paint(painter)
            assert abs(painter.path.elementAt(0).y - painter.path.elementAt(1).y) > 40
            # Padding must not imply negative recorded angles on the monitor.
            curve.zeroBased = True
            for samples in ([0, .1, .25], [0, 0]):
                curve.values = samples
                painter = Painter(); curve.paint(painter)
                assert not any(label.startswith('-') for label in painter.labels), painter.labels
                assert painter.labels[-1] == '0.00°'
                assert curve.values == samples
            # Raw preflight readings and their negative axis remain intact.
            curve.zeroBased = False
            curve.values = [-.2, -.1]
            painter = Painter(); curve.paint(painter)
            assert all(label.startswith('-') for label in painter.labels), painter.labels
            sensor.setParent(None)
        ''')
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(root / "scripts"))
        result = subprocess.run([sys.executable, "-c", source], cwd=root, env=env,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
