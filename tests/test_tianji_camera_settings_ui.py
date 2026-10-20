"""The teleop controls share one guarded request and preserve recording locks."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import pytest

@pytest.mark.skipif(importlib.util.find_spec('PySide6') is None,reason='PySide6 unavailable')
def test_native_teleop_settings_controls_and_recording_lock():
    root=Path(__file__).resolve().parents[1]
    code='''
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from PySide6.QtCore import QObject,QUrl
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlComponent,QQmlEngine
        from qt_ui.bridge import Bridge
        app=QGuiApplication([])
        with TemporaryDirectory() as settings:
            bridge=Bridge({},settings_path=settings);bridge.poll_timer.stop()
            bridge._state.update(authenticated=True,page='teleop',robot='tianji',teleopStep=2,
                tianji=dict(state='idle',cameras=[dict(id='usb_2',connected=True)],collection={}))
            calls=[];bridge.request=lambda path,**kwargs:calls.append((path,kwargs))
            engine=QQmlEngine();engine.rootContext().setContextProperty('backend',bridge)
            engine.rootContext().setContextProperty('systemDark',False)
            components=[];items=[]
            for name in ('CameraResolution','CameraBrightness'):
                component=QQmlComponent(engine,QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/'+name+'.qml').resolve())))
                item=component.createWithInitialProperties(dict(scope='tianji',cameraKey='usb_2',available=True,width=400))
                assert item is not None,[e.toString() for e in component.errors()]
                components.append(component);items.append(item)
            assert len(calls)==1,calls
            assert calls[0][0]=='/tianji/camera-controls/usb_2/settings'
            mode=dict(id='mjpeg',format='mjpeg',width=1024,height=768,fps=30,label='1024 × 768 · 30 帧/秒')
            value=dict(brightness=dict(supported=True,min=-64,max=64,step=1,default=0,value=7),
                resolution=dict(options=[mode],selected=mode,editable=True))
            calls[0][1]['done'](value);app.processEvents()
            select=items[0].findChild(QObject,'cameraResolutionSelect')
            slider=items[1].findChild(QObject,'cameraBrightnessSlider')
            assert select.property('enabled') and slider.property('enabled'),(select.property('enabled'),slider.property('enabled'),bridge.teleopCameraSettings,len(calls),[(i.property('available'),i.property('editable'),i.property('details')) for i in items])
            assert slider.property('value')==7 and select.property('displayText')==mode['label'],(slider.property('value'),select.property('displayText'),bridge.teleopCameraSettings)
            # Busy/lock changes must not cause repeated hardware queries.
            for tick in range(20):
                for item in items:item.setProperty('editable',False)
                for item in items:item.setProperty('editable',True)
                app.processEvents()
            assert len(calls)==1,calls
            bridge.setTeleopCameraBrightness('usb_2',-8)
            assert calls[-1][1]['data']=={'brightness':-8}
            app.processEvents();assert not select.property('enabled') and not slider.property('enabled')
            calls[-1][1]['failed']('硬件读回失败');app.processEvents()
            assert bridge.teleopCameraSettings['usb_2']['brightness']['value']==7
            bridge.loadTeleopCameraSettings('usb_2');calls[-1][1]['done'](value);app.processEvents()
            before=len(calls)
            bridge._state['tianji']['collection']={'armed':True}
            bridge.setTeleopCameraResolution('usb_2','mjpeg');assert len(calls)==before
            for item in items:item.setProperty('editable',False)
            assert not select.property('enabled') and not slider.property('enabled')
            bridge._state['tianji'].update(state='running',collection={'armed':True})
            bridge.reconnectTeleopCameras()
            assert calls[-1][0]=='/tianji/camera-preview/reconnect'
            assert bridge.state['busy']==''
            before=len(calls)
            bridge._state['tianji']['collection']={'armed':True,'recording':True}
            bridge.reconnectTeleopCameras();assert len(calls)==before
            for item in items:item.setProperty('available',False);item.deleteLater()
            app.processEvents()
        print('PASS teleop settings and guarded requests, no duplicate refresh or recording changes')
    '''
    result=subprocess.run([sys.executable,'-c',textwrap.dedent(code)],cwd=root,
        env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QT_QUICK_BACKEND='software',PYTHONPATH=str(root/'scripts')),
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
