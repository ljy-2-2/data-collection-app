"""Exercise the collection monitor's real Qt layout at board/window sizes."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_monitor_fills_equal_squares_without_stretching_or_modifying_camera_jpegs():
    script = textwrap.dedent('''
        import tempfile
        from pathlib import Path
        from PySide6.QtCore import QUrl, QSize, Qt, QByteArray, QBuffer, QIODevice
        from PySide6.QtGui import QGuiApplication, QImage, QPainter
        from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
        from PySide6.QtTest import QTest
        from shiboken6 import delete
        from qt_ui.bridge import Bridge
        from qt_ui.visuals import CameraItem, CurveItem, decode_preview_image

        app = QGuiApplication([])
        class OfflineCamera(CameraItem):
            instances = []
            def __init__(self, parent=None):
                super().__init__(parent)
                self.instances.append(self)
            def connect_stream(self):
                pass
        qmlRegisterType(OfflineCamera, 'Collection.Native', 1, 0, 'CameraItem')
        qmlRegisterType(CurveItem, 'Collection.Native', 1, 0, 'CurveItem')
        with tempfile.TemporaryDirectory() as config:
            bridge = Bridge({}, demo=True, settings_path=config)
            bridge.poll_timer.stop()
            bridge.update(ready=True, authenticated=True, page='umi', umiStep=3,
                          umi=dict(state='ready', recording=False, monitor_connected=True, monitor={}))
            engine = QQmlApplicationEngine()
            engine.rootContext().setContextProperty('backend', bridge)
            engine.rootContext().setContextProperty('systemDark', False)
            qml = """
                import QtQuick
                import QtQuick.Controls
                import "."
                ApplicationWindow {
                    id: app; width: 1280; height: 900; visible: true
                    Umi { width: parent.width }
                }
            """
            engine.loadData(qml.encode(), QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/MonitorTest.qml').resolve())))
            assert engine.rootObjects()
            window = engine.rootObjects()[0]
            for width in (1280, 1920, 800, 600):
                window.setWidth(width)
                QTest.qWait(80)
                cameras = [c for c in OfflineCamera.instances if c.objectName().startswith('umiMonitorVideo-')]
                assert len(cameras) == 3
                widths = [c.width() for c in cameras]
                heights = [c.height() for c in cameras]
                assert min(widths) > 0 and max(widths) - min(widths) <= 1, widths
                assert max(heights) - min(heights) <= 1, heights
                for c in cameras:
                    assert abs(c.height() - c.width()) < 1
                    assert c.source.endswith('?native=true')
                    assert c.cropToFill
                    # Center 5:4 and wrist 16:9 must both fill the actual square,
                    # not merely have equal outer containers with unequal images.
                    target = QSize(round(c.width()), round(c.height()))
                    for size in (QSize(1920, 1080), QSize(1280, 1024)):
                        image = QImage(size, QImage.Format_RGB32); image.fill(Qt.green)
                        painter = QPainter(image)
                        margin = (size.width() - size.height()) // 2
                        painter.fillRect(0, 0, margin, size.height(), Qt.red)
                        painter.fillRect(size.width()-margin, 0, margin, size.height(), Qt.blue)
                        painter.end()
                        data = QByteArray(); buffer = QBuffer(data); buffer.open(QIODevice.WriteOnly)
                        assert image.save(buffer, 'JPEG')
                        original = bytes(data)
                        decoded = decode_preview_image(original, target, crop=True)
                        expected = size.scaled(target, Qt.KeepAspectRatioByExpanding)
                        assert decoded.size() == expected
                        assert decoded.width() >= target.width() and decoded.height() >= target.height()
                        c.frame = decoded
                        painted = QImage(target, QImage.Format_RGB32); painted.fill(Qt.magenta)
                        painter = QPainter(painted); c.paint(painter); painter.end()
                        # All corners are real green image pixels: no letterbox.
                        # The red/blue side bands are cropped, not squeezed in.
                        for x, y in [(4,4),(target.width()-5,4),
                                     (4,target.height()-5),(target.width()-5,target.height()-5)]:
                            color = painted.pixelColor(x,y)
                            assert color.green() > 230 and color.red() < 20 and color.blue() < 20, color.name()
                        assert bytes(data) == original
                        assert QImage.fromData(data, 'JPEG').size() == size
            # Leaving monitoring releases all camera subscriptions.
            bridge.update(umiStep=1)
            QTest.qWait(20)
            assert all(not c.source for c in cameras)
            window.setVisible(False)
            delete(engine)
    ''')
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
               PYTHONPATH=str(root / "scripts") + os.pathsep + str(root / "teleop_control/src"))
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_hardware_and_monitor_share_full_width_square_previews():
    script = textwrap.dedent('''
        import tempfile
        from pathlib import Path
        from PySide6.QtCore import QObject, QUrl, QPointF
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
        from PySide6.QtQuick import QQuickItem
        from PySide6.QtTest import QTest
        from shiboken6 import delete
        from qt_ui.bridge import Bridge
        from qt_ui.visuals import CameraItem, CurveItem, Backdrop, G2View, RobotScene

        app = QGuiApplication([])
        class OfflineCamera(CameraItem):
            instances = []
            def __init__(self, parent=None):
                super().__init__(parent); self.instances.append(self)
            def connect_stream(self):
                pass
        for name, cls in [('CameraItem', OfflineCamera), ('CurveItem', CurveItem),
                          ('Backdrop', Backdrop), ('G2View', G2View)]:
            qmlRegisterType(cls, 'Collection.Native', 1, 0, name)
        with tempfile.TemporaryDirectory() as config:
            bridge = Bridge({'APP_COLLECTION_MODE':'umi-tianji'}, demo=True, settings_path=config)
            bridge.poll_timer.stop()
            bridge.update(ready=True, authenticated=True, page='umi', umiStep=3)
            engine = QQmlApplicationEngine()
            scene = RobotScene(Path.cwd(), load_model=False)
            context = engine.rootContext()
            context.setContextProperty('backend', bridge)
            context.setContextProperty('systemDark', False)
            context.setContextProperty('robotScene', scene)
            engine.load(QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/Main.qml').resolve())))
            assert engine.rootObjects()
            window = engine.rootObjects()[0]
            bridge.request = lambda *args, **kwargs: None
            roles = dict(left='/dev/video0', center='/dev/video4', right='/dev/video2')
            bridge.update(cameraRoles=roles)
            for width, height in [(1920,1080),(1280,800),(1366,768),(1024,720),(600,800)]:
                window.setWidth(width); window.setHeight(height)
                layouts = {}
                for step, prefix in [(2,'umiPreflightVideo-'), (3,'umiMonitorVideo-')]:
                    bridge.update(umiStep=step, umi=dict(state='ready' if step==3 else 'idle',
                        recording=False, monitor_connected=step==3,
                        monitor=dict(device_check=dict(camera_candidates=list(roles.values())))))
                    QTest.qWait(100)
                    cameras = [c for c in OfflineCamera.instances if c.objectName().startswith(prefix)]
                    assert len(cameras)==3
                    sizes = [(c.width(),c.height()) for c in cameras]
                    assert max(w for w,h in sizes)-min(w for w,h in sizes)<=1, sizes
                    assert all(abs(w-h)<1 for w,h in sizes), sizes
                    assert all(c.cropToFill and c.isVisible() and c.source for c in cameras)
                    grid=window.findChild(QQuickItem, 'umiPreflightCameras' if step==2 else 'umiMonitorCameras')
                    columns=1 if width<780 else 3
                    expected=(grid.width()-(columns-1)*9)/columns-2
                    assert all(abs(w-expected)<=1 for w,h in sizes), (sizes,expected)
                    # Restored videos use the full row, independent of window height.
                    if width==1280:
                        assert all(345<=w<=350 for w,h in sizes), sizes
                    layouts[step]=sizes
                assert all(abs(a-b)<=1 for first,second in zip(layouts[2],layouts[3])
                           for a,b in zip(first,second)),layouts
                inactive = [c for c in OfflineCamera.instances if c.objectName().startswith('umiPreflightVideo-')]
                assert all(not c.source for c in inactive)
                record=window.findChild(QQuickItem,'umiRecord')
                assert record.isVisible() and record.isEnabled()
                scroll=window.findChild(QQuickItem,'pageScroll')
                # The enlarged layout can scroll; its actions remain reachable.
                scroll.setProperty('contentY', max(0,scroll.property('contentHeight')-scroll.height()))
                QTest.qWait(50)
                bottom=record.mapToScene(QPointF(record.width(),record.height()))
                assert 0<bottom.y()<=window.height()-5, (width,height,bottom.y())
            window.setVisible(False)
            delete(engine)
    ''')
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
               PYTHONPATH=str(root / "scripts") + os.pathsep + str(root / "teleop_control/src"))
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
