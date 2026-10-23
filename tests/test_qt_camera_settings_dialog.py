"""Exercise staged camera settings with fake replies; never connect to hardware."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_confirm_cancel_and_batch_failure(tmp_path):
    root=Path(__file__).resolve().parents[1]
    code=r'''
        import copy,json,os
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from PySide6.QtCore import QObject,QUrl,QMetaObject,QPointF,Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine,qmlRegisterType,QQmlProperty
        from PySide6.QtTest import QTest
        from qt_ui.bridge import Bridge
        from teleop_control.tianji_models import TianjiCameraSettingsBatch
        from qt_ui.visuals import CameraItem
        from shiboken6 import isValid,delete
        class OfflineCamera(CameraItem):
            def connect_stream(self):pass
        app=QGuiApplication([])
        qmlRegisterType(OfflineCamera,'Collection.Native',1,0,'CameraItem')
        with TemporaryDirectory() as config:
            bridge=Bridge({},settings_path=config);bridge.poll_timer.stop()
            ids=['usb_1','usb_4','usb_2']
            modes=[dict(id='small',label='640 × 480',width=640,height=480),dict(id='large',label='1920 × 1080',width=1920,height=1080)]
            values={key:dict(resolution=dict(options=modes,selected=modes[0],editable=True),
                brightness=dict(supported=True,value=7,min=-64,max=64,step=1,default=0)) for key in ids}
            cameras=[dict(id=key,connected=True) for key in ids]
            bridge.update(authenticated=True,robot='tianji',teleopStep=2,
                tianji=dict(state='idle',cameras=cameras,collection={}),
                uploadTarget=dict(selected='local',web_url='http://localhost:5173',destinations=[dict(kind='local'),dict(kind='cloud')]))
            writes=[];reads=[]
            def request(path,**kwargs):
                assert path.startswith('/tianji/camera-controls/') and path.endswith('/settings'),path
                if kwargs.get('method')=='POST':
                    # Exercise the actual strict API schema after JSON serialization.
                    # Python equality alone misses this: 12.0 == 12, but JSON differs.
                    assert path=='/tianji/camera-controls/settings'
                    TianjiCameraSettingsBatch.model_validate_json(json.dumps(kwargs['data']))
                    writes.append(kwargs)
                else:
                    key=path.split('/')[-2]
                    reads.append(key);kwargs['done'](copy.deepcopy(values[key]))
            bridge.request=request
            engine=QQmlApplicationEngine();warnings=[]
            engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
            engine.rootContext().setContextProperty('backend',bridge)
            engine.rootContext().setContextProperty('systemDark',False)
            engine.loadData(b"""
                import QtQuick
                import QtQuick.Controls
                ApplicationWindow {
                    width: 1050;height: 850;visible:true
                    Setup {id:setup;width:parent.width;layer.enabled:false}
                    TeleopCameras {y:setup.height+16;width:parent.width;height:implicitHeight;active:true;status:backend.state.tianji;layer.enabled:false}
                }
            """,QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/CameraDialogTest.qml').resolve())))
            assert engine.rootObjects(),warnings
            window=engine.rootObjects()[0]
            def walk(obj):
                yield obj
                for child in obj.childItems():yield from walk(child)
            def item(name):return next(o for o in walk(window.contentItem()) if o.objectName()==name)
            def click(control):
                point=control.mapToScene(QPointF(control.width()/2,control.height()/2)).toPoint()
                QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(50)
            def open_dialog():
                QMetaObject.invokeMethod(dialog,'open');QTest.qWait(60)
            def change(key):
                section=item('cameraDraft-'+key)
                section.setProperty('dirty',True);section.setProperty('modeId','large');section.setProperty('brightnessText','12')
                return section
            for robot in ['tianji','kuavo-5w']:
                bridge.update(robot=robot);QTest.qWait(20)
                assert item('teleopUploadTargetLabel').isVisible()
                assert item('teleopUploadTargetLabel').property('text')=='本机平台'
                assert not item('teleopUploadTarget').isVisible()
            bridge.update(uploadTarget={**bridge.state['uploadTarget'],'selected':'cloud'})
            assert item('teleopUploadTargetLabel').property('text')=='云端平台'
            bridge.update(robot='tianji');QTest.qWait(20)
            assert item('robotType').property('currentText')=='天机 Marvin M6'
            window.grabWindow().save(os.environ['CAMERA_SCREENSHOT']+'-layout.png')
            dialog=window.findChild(QObject,'tianjiCameraDialog')
            assert not reads and not writes, 'Loading the page must not read or apply settings'
            click(item('teleopCameraSettings'))
            assert dialog.property('opened') and reads==ids
            section=change('usb_1');assert not writes
            click(item('cameraSettingsCancel'));assert not dialog.property('opened') and not writes
            open_dialog();section=item('cameraDraft-usb_1')
            assert section.property('modeId')=='small' and section.property('brightnessText')=='7'
            section=change('usb_1');change('usb_4')
            section.setProperty('brightnessText','999');QTest.qWait(20)
            assert not item('cameraSettingsConfirm').property('enabled')
            section.setProperty('brightnessText','12');QTest.qWait(20)
            window.grabWindow().save(os.environ['CAMERA_SCREENSHOT']+'-dialog.png')
            click(item('cameraSettingsConfirm'))
            assert len(writes)==1 and writes[0]['data']==dict(changes={key:dict(mode_id='large',brightness=12) for key in ('usb_1','usb_4')})
            assert bridge.state['cameraApplyBusy'] and not dialog.property('opened')
            assert not item('teleopCameraSettings').property('enabled')
            assert item('cameraApplyStatus').property('text')=='正在应用相机参数…'
            assert bridge.teleop_active(), 'Closing the popup must not release the operation lock'
            bridge.applyTeleopCameraSettings({'usb_2':{'brightness':10.0}})
            assert len(writes)==1, 'Do not submit another batch while the closed dialog is applying'
            # The backend rolls back the batch on failure. Retain every draft.
            writes[0]['failed']('左相机写入失败；已回写本次修改前的参数');QTest.qWait(20)
            assert not bridge.state['cameraApplyBusy'] and not dialog.property('opened')
            assert '左相机写入失败' in bridge.state['cameraApplyError']
            assert item('cameraApplyStatus').property('text')=='相机参数应用失败'
            assert item('teleopCameraSettings').property('enabled')
            open_dialog()
            for key in ('usb_1','usb_4'):
                assert item('cameraDraft-'+key).property('brightnessText')=='12'
            click(item('cameraSettingsConfirm'))
            assert not dialog.property('opened') and bridge.state['cameraApplyBusy']
            assert len(writes)==2 and writes[1]['data']==writes[0]['data']
            results={}
            for key in ('usb_1','usb_4'):
                result=copy.deepcopy(values[key]);result['resolution']['selected']=modes[1];result['brightness']['value']=12
                values[key]=result;results[key]=result
            writes[1]['done'](results);QTest.qWait(20)
            assert not dialog.property('opened') and not bridge.state['cameraApplyBusy']
            assert not bridge.state['cameraApplyError']
            assert item('cameraApplyStatus').property('text')=='相机参数已应用'
            # The reported right-camera value must serialize as 10, not 10.0,
            # and brightness-only edits must not change the recording mode.
            open_dialog();section=item('cameraDraft-usb_2')
            section.setProperty('dirty',True);section.setProperty('brightnessText','10')
            click(item('cameraSettingsConfirm'))
            assert len(writes)==3 and writes[2]['data']=={'changes':{'usb_2':{'brightness':10}}}
            result=copy.deepcopy(values['usb_2']);result['brightness']['value']=10
            values['usb_2']=result;writes[2]['done']({'usb_2':result});QTest.qWait(20)
            assert not dialog.property('opened')
            # Do not silently truncate, accept bools, or partly submit invalid batches.
            for invalid in (10.5,True,float('nan'),float('inf')):
                bridge.applyTeleopCameraSettings({'usb_1':{'brightness':3.0},'usb_2':{'brightness':invalid}})
                assert len(writes)==3 and not bridge.state['cameraApplyBusy']
            # Changing to collection mode after opening still prevents writes.
            open_dialog();change('usb_2')
            bridge.update(tianji=dict(state='running',cameras=cameras,collection=dict(armed=True)))
            QTest.qWait(20);assert not item('cameraSettingsConfirm').property('enabled')
            assert len(writes)==3
            assert not warnings,warnings
            window.close();delete(engine);bridge.shutdown()
    '''
    result=subprocess.run([sys.executable,'-c',textwrap.dedent(code)],cwd=root,
        env=dict(os.environ,PYTHONPATH=os.pathsep.join([str(root/'scripts'),str(root/'teleop_control/src'),os.environ.get('PYTHONPATH','')]),QT_QPA_PLATFORM='offscreen',QT_QUICK_BACKEND='software',
                 CAMERA_SCREENSHOT=str(tmp_path/'camera')),capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
