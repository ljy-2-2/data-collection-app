"""Verify real QML camera presentation using synthetic frames and no device IO."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_three_slots_refresh_stream_identity_and_hide_disconnected_frames(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=r'''
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from PySide6.QtCore import QUrl,QPointF
        from PySide6.QtGui import QGuiApplication,QImage,QColor
        from PySide6.QtQml import QQmlApplicationEngine,qmlRegisterType
        from PySide6.QtTest import QTest
        from shiboken6 import delete
        from qt_ui.bridge import Bridge
        from qt_ui.visuals import CameraItem
        class OfflineCamera(CameraItem):
            def connect_stream(self):pass
        app=QGuiApplication([])
        qmlRegisterType(OfflineCamera,'Collection.Native',1,0,'CameraItem')
        with TemporaryDirectory() as directory:
            backend=Bridge({},demo=True,settings_path=directory);backend.poll_timer.stop()
            backend.request=lambda *a,**kw:None
            engine=QQmlApplicationEngine();warnings=[]
            engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
            engine.rootContext().setContextProperty('backend',backend)
            engine.rootContext().setContextProperty('systemDark',False)
            engine.loadData(b''' + '"""' + '''
                import QtQuick
                import QtQuick.Controls
                ApplicationWindow {
                    width:900;height:400;visible:true
                    KuavoCameras {objectName:"cameraCard";anchors.fill:parent;layer.enabled:false}
                }
            ''' + '"""' + ''',QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/KuavoCameraTest.qml').resolve())))
            assert engine.rootObjects(),warnings
            window=engine.rootObjects()[0]
            def walk(item):
                yield item
                for child in item.childItems():yield from walk(child)
            def find(name):return next(o for o in walk(window.contentItem()) if o.objectName()==name)
            card=find('cameraCard');roles=('head','left','right')
            cameras={r:find('kuavoCameraVideo-'+r) for r in roles}
            tiles={r:find('kuavoCameraTile-'+r) for r in roles}
            def update(identity,missing=None):
                card.setProperty('status',dict(stream_id=identity,cameras=[dict(id=r,connected=r!=missing,state='error' if r==missing else 'streaming',error='') for r in roles]))
                QTest.qWait(40)
            update('first')
            colors={'head':'#d02030','left':'#20c060','right':'#3070d0'}
            for r,camera in cameras.items():
                assert camera.source.endswith('/'+r+'/stream?v=first')
                frame=QImage(1280 if r=='head' else 1920,800 if r=='head' else 1080,QImage.Format_RGB32);frame.fill(QColor(colors[r]))
                camera._decoded(camera.sequence,frame)
            QTest.qWait(50)
            assert all(c.isVisible() and c.online for c in cameras.values())
            for r,camera in cameras.items():
                assert camera.width()<=tiles[r].width() and camera.height()<=tiles[r].height()
                assert tiles[r].x()+tiles[r].width()<=card.width()
                assert tiles[r].y()+tiles[r].height()<=card.height()
            assert tiles['head'].x()<tiles['left'].x()==tiles['right'].x()
            assert tiles['head'].height()>tiles['left'].height()
            shot=window.grabWindow()
            shot.save(__import__('os').environ['CAMERA_SCREENSHOT'])
            for r,camera in cameras.items():
                center=camera.mapToScene(QPointF(camera.width()/2,camera.height()/2))
                assert shot.pixelColor(round(center.x()*shot.devicePixelRatio()),round(center.y()*shot.devicePixelRatio())).name()==colors[r],(r,shot.pixelColor(round(center.x()),round(center.y())).name(),colors[r],shot.size(),center)
            shot.save(__import__('os').environ['CAMERA_SCREENSHOT'])
            update('second')
            assert all(c.source.endswith('v=second') and not c.online for c in cameras.values())
            update('second',missing='left')
            assert cameras['left'].source==''
            assert cameras['head'].source and cameras['right'].source
            assert all(t.isVisible() for t in tiles.values())
            card.setProperty('active',False);QTest.qWait(30)
            assert all(not c.source for c in cameras.values())
            assert not warnings,warnings
            window.close();delete(engine);backend.shutdown()
    '''
    result=subprocess.run([sys.executable,'-c',textwrap.dedent(script)],cwd=root,
        env={**os.environ,'PYTHONPATH':str(root/'scripts'),'QT_QPA_PLATFORM':'offscreen','QT_QUICK_BACKEND':'software',
             'CAMERA_SCREENSHOT':str(tmp_path/'kuavo-cameras.png')},text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr


def test_shared_tianji_camera_drafts_cancel_confirm_and_readback(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=r'''
        import copy,json,os
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from PySide6.QtCore import QObject,QUrl,QMetaObject
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtQuick import QQuickWindow
        from PySide6.QtTest import QTest
        from shiboken6 import delete
        from qt_ui.bridge import Bridge
        from teleop_control.kuavo_cameras import CaptureProfile
        app=QGuiApplication([])
        with TemporaryDirectory() as directory:
            backend=Bridge({},demo=True,settings_path=directory);backend.poll_timer.stop();backend.demo=False
            roles=['head','left','right']
            config=dict(transport='auto',host='fake',user='fake',ros_master='http://fake:11311',ros_setup='/fake',roles={r:r for r in roles})
            status=dict(config=config,sources=[dict(id=r,kind='usb',capabilities=dict(
                resolutions=[dict(width=640,height=480),dict(width=1280,height=720)],brightness=dict(value=0,min=-64,max=64,step=1,default=0))) for r in roles],
                cameras=[dict(id=r,connected=True,width=640,height=480,error='') for r in roles])
            backend.update(kuavo={'camera_preview':status})
            requests=[];pending=[]
            def request(path,data,done,**kwargs):
                requests.append((path,copy.deepcopy(data)))
                if path.endswith('/discover'):done(copy.deepcopy(status))
                elif path.endswith('/settings'):
                    for profile in data.values():CaptureProfile.model_validate_json(json.dumps(profile))
                    pending.append((done,kwargs['failed']))
                else:raise AssertionError(path)
            backend.request=request
            engine=QQmlApplicationEngine();warnings=[]
            engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
            engine.rootContext().setContextProperty('backend',backend)
            engine.rootContext().setContextProperty('systemDark',False)
            engine.loadData(b"""
                import QtQuick
                import QtQuick.Controls
                ApplicationWindow {
                    width:900;height:800;visible:true
                    KuavoCameraDialog {cameraStatus:backend.state.kuavo.camera_preview;busy:!!backend.state.kuavoCameraBusy}
                }
            """,QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/KuavoSettingsTest.qml').resolve())))
            assert engine.rootObjects(),warnings
            window=engine.rootObjects()[0];dialog=window.findChild(QObject,'kuavoCameraDialog')
            def walk(obj):
                yield obj
                for child in obj.childItems():yield from walk(child)
            def item(name):return next(o for o in walk(window.contentItem()) if o.objectName()==name)
            def opened():QMetaObject.invokeMethod(dialog,'open');QTest.qWait(40)
            assert not requests
            opened()
            assert dialog.property('width')==560
            assert len(requests)==1 and requests[0][0]=='/kuavo/cameras/discover'
            for role in roles:
                assert item('cameraDraftResolution-'+role).property('count')==2
                assert item('cameraDraftBrightnessSlider-'+role).property('enabled')
            slider=item('cameraDraftBrightnessSlider-left');slider.setProperty('value',12);slider.moved.emit();QTest.qWait(20)
            assert item('cameraDraftBrightness-left').property('text')=='12'
            assert len(requests)==1
            item('kuavoCameraCancel').clicked.emit();QTest.qWait(20)
            opened();assert item('cameraDraftBrightness-left').property('text')=='0'
            draft=item('kuavoCameraDraft-left')
            draft.setProperty('dirty',True);draft.setProperty('modeId','1280x720');draft.setProperty('brightnessText','100')
            QTest.qWait(20);assert not item('kuavoCameraConfirm').property('enabled')
            draft.setProperty('brightnessText','16');QTest.qWait(20)
            assert item('cameraDraftBrightnessSlider-left').property('value')==16
            window.grabWindow().save(os.environ['CAMERA_SCREENSHOT'])
            item('kuavoCameraConfirm').clicked.emit();QTest.qWait(20)
            assert not dialog.property('opened')
            assert requests[-1]==('/kuavo/cameras/settings',{'left':{'width':1280,'height':720,'brightness':16}})
            pending[-1][1]('fake readback mismatch');QTest.qWait(20)
            opened();assert draft.property('modeId')=='1280x720' and draft.property('brightnessText')=='16'
            item('kuavoCameraConfirm').clicked.emit();QTest.qWait(20)
            updated=copy.deepcopy(status);updated['config']['profiles']={'left':dict(width=1280,height=720,brightness=16)}
            updated['cameras'][1].update(width=1280,height=720)
            updated['sources'][1]['capabilities']['brightness']['value']=16
            status=updated;pending[-1][0](updated);QTest.qWait(20)
            opened();count=len(requests);item('kuavoCameraConfirm').clicked.emit();QTest.qWait(20)
            assert len(requests)==count, 'unchanged confirm must not re-open cameras'
            assert not warnings,warnings
            window.close();delete(engine);backend.shutdown()
    '''
    result=subprocess.run([sys.executable,'-c',textwrap.dedent(script)],cwd=root,
        env={**os.environ,'PYTHONPATH':str(root/'scripts')+':'+str(root/'teleop_control/src'),
             'QT_QPA_PLATFORM':'offscreen','QT_QUICK_BACKEND':'software','CAMERA_SCREENSHOT':str(tmp_path/'kuavo-settings.png')},
        text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
