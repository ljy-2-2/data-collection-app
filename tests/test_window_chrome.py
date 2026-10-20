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
            from PySide6.QtGui import QGuiApplication, QImage, QColor
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
                def find_item(name, visible=False):
                    pending = [window.contentItem()]
                    while pending:
                        item = pending.pop()
                        if item.objectName() == name and (not visible or item.isVisible()): return item
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
                backend.update(page='teleop', teleopStep=2, robot='tianji', error='', notice='')
                original_status = dict(backend.state['tianji'])
                for width in (360, 1280):
                    window.resize(width, 800)
                    QTest.qWait(40)
                    tools = find_item('pageTools')
                    guide = find_item('usageGuide')
                    notice = find_item('pageNotice')
                    stepper = find_item('pageStepper')
                    workspace = find_item('teleopWorkspace')
                    scroll = find_item('pageScroll')
                    baseline = (stepper.y(), workspace.y(), workspace.height(), scroll.property('contentHeight'))
                    assert tools.height() == 30
                    for message in ('等待采集账号登录后上传', '较长错误：' + '连接失败，请检查账号。' * 30, ''):
                        backend.update(error=message)
                        QTest.qWait(30)
                        assert (stepper.y(), workspace.y(), workspace.height(), scroll.property('contentHeight')) == baseline
                        assert notice.isVisible() == bool(message)
                        assert abs(notice.x() + notice.width() + 12 - guide.x()) < .1
                        assert notice.height() == guide.height() == 30
                        assert find_item('pageNoticeText').property('text') == message
                    backend.update(tianji=dict(original_status, collection=dict(error='采集失败', upload=dict(error='等待采集账号登录后上传'))))
                    QTest.qWait(30)
                    assert '采集失败' in find_item('pageNoticeText').property('text')
                    assert '等待采集账号登录后上传' in find_item('pageNoticeText').property('text')
                    assert (stepper.y(), workspace.y(), workspace.height(), scroll.property('contentHeight')) == baseline
                    screenshot_dir = os.environ.get('NOTICE_LAYOUT_SCREENSHOT_DIR')
                    if screenshot_dir:
                        def disable_notice_shadow(item):
                            if not isValid(item): return
                            if item.metaObject().className().startswith(('Card_', 'Setup_')):
                                QQmlProperty.write(item, 'layer.enabled', False)
                            for child in item.childItems(): disable_notice_shadow(child)
                        disable_notice_shadow(window.contentItem())
                        QTest.qWait(100)
                        assert window.grabWindow().save(str(Path(screenshot_dir) / f'notice-{width}.png'))
                    backend.update(tianji=original_status, notice='上传已恢复')
                    QTest.qWait(30)
                    assert (stepper.y(), workspace.y(), workspace.height(), scroll.property('contentHeight')) == baseline
                    backend.update(notice='')
                camera_ids = ['usb_2', 'usb_4', 'usb_1']
                camera_roles = ['right', 'left', 'center']
                for count in (0, 1, 2, 3, 2, 0, 3):
                    inventory = [dict(id=key, connected=True, available=False) for key in camera_ids[:count]]
                    backend.update(page='teleop', teleopStep=2, tianji=dict(original_status, cameras=inventory, camera_preview_mode='snapshot'))
                    QTest.qWait(30)
                    for idx, role in enumerate(camera_roles):
                        assert (find_item('teleopCamera-' + role) is not None) == (idx < count)
                    if count:
                        ordered_roles = [r for r in ('left', 'center', 'right') if r in camera_roles[:count]]
                        tiles = [find_item('teleopCameraTile-' + r) for r in ordered_roles]
                        assert all(a.x() < b.x() and a.y() == b.y() for a, b in zip(tiles, tiles[1:])), 'Wrist previews must stay left/right regardless of discovery order'
                        for key, role in zip(camera_ids[:count], camera_roles[:count]):
                            assert find_item('teleopCameraTile-' + role).property('modelData') == key
                        tile = find_item('teleopCamera-' + camera_roles[0])
                        assert tile.staleInterval >= 10000
                        backend.update(tianji=dict(backend.state['tianji'], cameras=[dict(c, updated_at=2) for c in reversed(inventory)]))
                        QTest.qWait(10)
                        assert tile is find_item('teleopCamera-' + camera_roles[0]), 'Polling must not recreate camera readers'
                for robot in ('agibot-g2', 'tianji'):
                    backend.update(robot=robot)
                    for width in (360, 780, 1024, 1180, 1280, 1440, 1920):
                        window.resize(width, 1000)
                        QTest.qWait(30)
                        model = find_item('teleopRobotCard')
                        cameras = find_item('teleopCameraCard')
                        stage = find_item('teleopRobotStage')
                        images = find_item('teleopCameraImages')
                        assert abs(model.width()-cameras.width()) < 1, (robot, width, model.width(), cameras.width())
                        assert abs(model.height()-cameras.height()) < 1
                        assert abs(stage.width()-images.width()) < 1
                        assert abs(stage.height()-images.height()) < 1
                        assert stage.y() == images.y()
                        if width > 1180:
                            assert abs(model.y()-cameras.y()) < 1
                        else:
                            assert abs(model.x()-cameras.x()) < 1
                            assert cameras.y() >= model.y()+model.height()
                        screenshot_dir = os.environ.get('TELEOP_LAYOUT_SCREENSHOT_DIR')
                        if screenshot_dir and robot == 'tianji' and width in (780, 1440):
                            def disable_panel_shadow(item):
                                if not isValid(item): return
                                QQmlProperty.write(item, 'layer.enabled', False)
                                for child in item.childItems(): disable_panel_shadow(child)
                            disable_panel_shadow(window.contentItem())
                            QTest.qWait(80)
                            assert window.grabWindow().save(str(Path(screenshot_dir) / f'teleop-panels-{width}.png'))
                for role in ('left', 'center', 'right'):
                    camera = find_item('teleopCamera-' + role)
                    label = find_item('teleopCameraLabel-' + role)
                    stale = find_item('teleopCameraStale-' + role)
                    assert label.isVisible()
                    frame = QImage(32, 24, QImage.Format_RGB32)
                    frame.fill(QColor('blue'))
                    camera._source = 'offline-fixture'
                    camera._decoded(camera.sequence, frame)
                    QTest.qWait(10)
                    assert label.isVisible() and not stale.isVisible()
                    camera.offline()
                    QTest.qWait(10)
                    assert label.isVisible() and stale.isVisible()
                    camera.source = ''
                target = dict(selected='local', web_url='http://127.0.0.1:5173', destinations=[dict(kind='local'), dict(kind='cloud')])
                backend.update(uploadTarget=target, tianji=original_status)
                for width in (360, 1280):
                    window.resize(width, 800)
                    for authenticated, page, step, name in ((False, 'teleop', 1, 'loginUploadTarget'), (True, 'teleop', 1, 'teleopUploadTarget'), (True, 'umi', 2, 'umiUploadTarget')):
                        backend.update(authenticated=authenticated, page=page, teleopStep=step, umiStep=step)
                        QTest.qWait(30)
                        selector = find_item(name, visible=True)
                        assert selector is not None, (name, width)
                        assert selector.isVisible(), (name, width, backend.state['page'], backend.state['teleopStep'], backend.state['umiStep'])
                        content = selector.parentItem()
                        assert abs(selector.y() + selector.height()/2 - content.height()/2) < 1
                        assert selector.x() + selector.width() <= content.width() + 1
                        assert selector.height() == 32
                        screenshot_dir = os.environ.get('NOTICE_LAYOUT_SCREENSHOT_DIR')
                        if screenshot_dir:
                            def disable_target_shadow(item):
                                if not isValid(item): return
                                QQmlProperty.write(item, 'layer.enabled', False)
                                for child in item.childItems(): disable_target_shadow(child)
                            disable_target_shadow(window.contentItem())
                            QTest.qWait(100)
                            assert window.grabWindow().save(str(Path(screenshot_dir) / f'{name}-{width}.png'))
                assert 'ToolTip' not in Path('scripts/qt_ui/qml/Main.qml').read_text()
                camera_qml = Path('scripts/qt_ui/qml/TeleopCameras.qml').read_text()
                assert 'cameraIds' in camera_qml
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
