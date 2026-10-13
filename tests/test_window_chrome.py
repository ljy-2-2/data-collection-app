"""Keep native window controls and the existing safe-close lifecycle."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 unavailable')
class WindowChromeTests(unittest.TestCase):
    def test_native_controls_have_no_duplicate_header_and_preserve_safe_close(self):
        root = Path(__file__).resolve().parents[1]
        source = textwrap.dedent('''
            import os
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PySide6.QtCore import QObject, QPointF, QUrl, Qt, qInstallMessageHandler
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtQml import QQmlApplicationEngine, QQmlProperty, qmlRegisterType
            from PySide6.QtQuick import QQuickWindow
            from PySide6.QtTest import QTest
            from shiboken6 import isValid
            from qt_ui.bridge import Bridge
            from qt_ui.visuals import CameraItem, CurveItem, Backdrop, G2View, RobotScene

            app = QGuiApplication([])
            warnings = []
            qInstallMessageHandler(lambda kind, context, message: warnings.append(message))
            for name, cls in [('CameraItem', CameraItem), ('CurveItem', CurveItem), ('Backdrop', Backdrop), ('G2View', G2View)]:
                qmlRegisterType(cls, 'Collection.Native', 1, 0, name)
            with TemporaryDirectory() as settings:
                backend = Bridge({}, demo=True, settings_path=settings)
                backend.poll_timer.stop()
                scene = RobotScene(Path.cwd())
                engine = QQmlApplicationEngine()
                engine.rootContext().setContextProperty('backend', backend)
                engine.rootContext().setContextProperty('robotScene', scene)
                engine.rootContext().setContextProperty('systemDark', False)
                engine.load(QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/Main.qml').resolve())))
                assert engine.rootObjects(), warnings
                window = engine.rootObjects()[0]
                window.resize(800, 600)
                QTest.qWait(80)
                def find_item(name):
                    pending = [window.contentItem()]
                    while pending:
                        item = pending.pop()
                        if item.objectName() == name: return item
                        pending.extend(item.childItems())
                assert all(find_item(name) is None for name in ('windowMinimize', 'windowMaximize', 'windowClose'))
                for flag in (Qt.WindowTitleHint, Qt.WindowSystemMenuHint, Qt.WindowMinimizeButtonHint, Qt.WindowMaximizeButtonHint, Qt.WindowCloseButtonHint):
                    assert window.flags() & flag
                assert not window.flags() & Qt.FramelessWindowHint
                for ready, authenticated in ((False, False), (True, False), (True, True)):
                    backend.update(ready=ready, authenticated=authenticated, page='ego')
                    for width, theme in ((360, 'light'), (1280, 'dark')):
                        backend.update(theme=theme)
                        window.resize(width, 600)
                        QTest.qWait(30)
                        assert window.contentItem().y() == 0, 'No duplicate title bar above content'
                window.showMinimized(); QTest.qWait(40)
                assert window.visibility() == QQuickWindow.Minimized
                window.showMaximized(); QTest.qWait(40)
                assert window.visibility() == QQuickWindow.Maximized
                window.showNormal(); QTest.qWait(40)
                assert window.visibility() == QQuickWindow.Windowed
                requests = []
                backend.closeRequested.connect(lambda: requests.append('safe-close'))
                backend.update(busy='record-stop')
                window.close(); QTest.qWait(40)
                assert requests == ['safe-close']
                assert window.isVisible(), 'Close must wait for existing shutdown handler'
                backend.update(busy='', page='umi', umiStep=1)
                screenshot = os.environ.get('WINDOW_CHROME_SCREENSHOT')
                if screenshot:
                    window.resize(1024, 768)
                    # Offscreen cannot draw the existing shadow effect. Disable
                    # only fixture shadows to make the UI screenshot readable.
                    def disable_shadow(item):
                        if not isValid(item): return
                        if item.metaObject().className().startswith(('Card_', 'Setup_')):
                            QQmlProperty.write(item, 'layer.enabled', False)
                        for child in item.childItems(): disable_shadow(child)
                    disable_shadow(window.contentItem())
                    QTest.qWait(100)
                    assert window.grabWindow().save(screenshot)
                bad = [message for message in warnings if any(term in message for term in ('ReferenceError', 'TypeError', 'Unable to assign', 'Binding loop', 'is not a type'))]
                assert not bad, bad
        ''')
        result = subprocess.run([sys.executable, '-c', source], cwd=root,
                                env=dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software',
                                         PYTHONPATH=str(root / 'scripts')),
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
