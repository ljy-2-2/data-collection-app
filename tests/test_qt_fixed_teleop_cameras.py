"""Fixed camera slots with actual QML/frame decoding and no network access."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 unavailable')
class FixedTeleopCamerasTest(unittest.TestCase):
    def test_inventory_disconnect_and_reconnect_keep_three_slots(self):
        root = Path(__file__).resolve().parents[1]
        source = r"""
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PySide6.QtCore import QPointF, QUrl
            from PySide6.QtGui import QGuiApplication, QColor, QImage
            from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
            from PySide6.QtTest import QTest
            from shiboken6 import delete
            from qt_ui.bridge import Bridge
            from qt_ui.visuals import CameraItem

            class OfflineCamera(CameraItem):
                def connect_stream(self):
                    pass  # Deliberately prohibit every HTTP/camera connection.

            app = QGuiApplication([])
            qmlRegisterType(OfflineCamera,'Collection.Native',1,0,'CameraItem')
            with TemporaryDirectory() as settings:
                backend = Bridge({},demo=True,settings_path=settings)
                backend.poll_timer.stop()
                backend.request = lambda *a, **kw: None
                backend._teleop_camera_settings = {key:dict(
                    resolution=dict(loaded=True,editable=False),
                    brightness=dict(loaded=True,supported=False))
                    for key in ('usb_1','usb_4','usb_2')}
                engine = QQmlApplicationEngine();warnings=[]
                engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
                engine.rootContext().setContextProperty('backend',backend)
                engine.rootContext().setContextProperty('systemDark',False)
                engine.loadData(b'''
                    import QtQuick
                    import QtQuick.Controls
                    ApplicationWindow {
                        width: 1050; height: 1100; visible: true
                        TeleopCameras {
                            width: parent.width; height: implicitHeight
                            active: true; layer.enabled: false
                        }
                    }
                ''',QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/FixedCameraTest.qml').resolve())))
                assert engine.rootObjects(),warnings
                window = engine.rootObjects()[0]
                def walk(item):
                    yield item
                    for child in item.childItems():yield from walk(child)
                def find(name):
                    return next(o for o in walk(window.contentItem()) if o.objectName()==name)
                card=find('teleopCameraCard')
                def inventory(entries):
                    card.setProperty('status',dict(cameras=entries))
                    QTest.qWait(35)
                roles=('center','left','right')
                ids=('usb_1','usb_4','usb_2')
                previews={r:find('teleopCamera-'+r) for r in roles}
                def geometry():
                    return [(find('teleopCameraTile-'+r).x(),find('teleopCameraTile-'+r).y(),
                             find('teleopCameraTile-'+r).width(),find('teleopCameraTile-'+r).height()) for r in roles]
                for width in (1050,720,480,360):
                    window.setWidth(width);inventory([])
                    baseline=geometry()
                    for count in (0,1,2,3,1,0):
                        entries=[dict(id=k,connected=True,available=False) for k in ids[:count]]
                        inventory(list(reversed(entries))+[dict(id='unknown',connected=True)])
                        assert geometry()==baseline
                        for i,(role,key) in enumerate(zip(roles,ids)):
                            camera=find('teleopCamera-'+role)
                            assert camera is previews[role]
                            assert bool(camera.source)==(i<count)
                            assert not camera.isVisible()
                            assert find('teleopCameraLabel-'+role).property('text')
                        assert len([o for o in walk(window.contentItem()) if o.objectName().startswith('teleopCameraTile-')])==3
                    main,left,right=baseline
                    if card.property('splitView'):
                        assert main[0]<left[0]==right[0] and main[1]==left[1]<right[1]
                    else:
                        assert main[1]<left[1]<right[1]
                    # Changing selected resolution and decoded frame size must not
                    # resize/reorder the preview slots, even across aspect ratios.
                    inventory([dict(id=k,connected=True) for k in ids])
                    baseline=geometry();card_height=card.height()
                    viewports=[(find('teleopCameraViewport-'+r).width(),find('teleopCameraViewport-'+r).height()) for r in roles]
                    for w,h in [(640,480),(1280,1024),(1920,1080)]:
                        backend._teleop_camera_settings={key:dict(resolution=dict(loaded=True,editable=False,selected=dict(width=w,height=h)),brightness=dict(loaded=True,supported=False)) for key in ids}
                        backend.teleopCameraSettingsChanged.emit()
                        frame=QImage(w,h,QImage.Format_RGB32);frame.fill(QColor('#00ff00'))
                        for camera in previews.values():camera._decoded(camera.sequence,frame)
                        QTest.qWait(35)
                        assert geometry()==baseline and card.height()==card_height
                        assert [(find('teleopCameraViewport-'+r).width(),find('teleopCameraViewport-'+r).height()) for r in roles]==viewports
                        assert all(camera.frame.width()==w and camera.frame.height()==h for camera in previews.values())
                    inventory([])
                window.setWidth(1050)
                inventory([dict(id=k,connected=True) for k in ids])
                camera=previews['left'];viewport=find('teleopCameraViewport-left')
                frame=QImage(640,480,QImage.Format_RGB32);frame.fill(QColor('#00ff00'))
                camera._decoded(camera.sequence,frame);QTest.qWait(60)
                assert camera.online and camera.isVisible()
                def pixel():
                    position=viewport.mapToScene(QPointF(viewport.width()/2,viewport.height()/2))
                    screenshot=window.grabWindow();dpr=screenshot.devicePixelRatio()
                    return screenshot.pixelColor(int(position.x()*dpr),int(position.y()*dpr)).name()
                assert pixel()=='#00ff00',pixel()
                # A wide frame is letterboxed, not stretched or cropped.
                wide=QImage(1920,1080,QImage.Format_RGB32);wide.fill(QColor('#00ff00'))
                camera._decoded(camera.sequence,wide);QTest.qWait(35)
                assert pixel()=='#00ff00'
                position=viewport.mapToScene(QPointF(viewport.width()/2,5))
                screenshot=window.grabWindow();dpr=screenshot.devicePixelRatio()
                assert screenshot.pixelColor(int(position.x()*dpr),int(position.y()*dpr)).name()=='#000000'
                camera.offline();QTest.qWait(60)
                assert camera.hasFrame and not camera.isVisible()
                assert pixel()=='#000000',pixel()
                camera._decoded(camera.sequence,frame);QTest.qWait(60)
                assert pixel()=='#00ff00'
                # A disconnected inventory entry wins even if available is stale.
                inventory([dict(id='usb_4',connected=False,available=True)])
                assert camera.source=='' and not camera.hasFrame
                assert pixel()=='#000000'
                # A late decode from the previous connection cannot resurrect it.
                camera._decoded(camera.sequence-1,frame);QTest.qWait(30)
                assert not camera.online and pixel()=='#000000'
                inventory([dict(id='usb_4',connected=True)])
                camera._decoded(camera.sequence,frame);QTest.qWait(60)
                assert camera is find('teleopCamera-left') and pixel()=='#00ff00'
                # G2 has the same fixed main/left/right roles.
                card.setProperty('tianji',False)
                card.setProperty('previewStatus',dict(cameras=[]));QTest.qWait(30)
                assert all(find('teleopCamera-'+r).source=='' for r in roles)
                assert [find('teleopCameraTile-'+r).property('modelData') for r in roles]==['head_color','hand_left_color','hand_right_color']
                assert not warnings,warnings
                window.close();delete(engine);backend.shutdown()
        """
        result = subprocess.run([sys.executable,'-c',textwrap.dedent(source)],cwd=root,
            env=dict(os.environ,PYTHONPATH=str(root/'scripts'),QT_SCALE_FACTOR='1',QT_QPA_PLATFORM='offscreen',QT_QUICK_BACKEND='software'),
            capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
