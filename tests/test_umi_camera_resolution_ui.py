"""Native resolution selector and its guarded control-service request."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.skipif(importlib.util.find_spec('PySide6') is None,reason='PySide6 unavailable')
def test_resolution_qml_and_bridge_render_options_and_lock_during_collection():
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
            bridge._state.update(authenticated=True,page='umi',umiStep=2,cameraRoles={'left':'/dev/video2'})
            calls=[]
            bridge.request=lambda path,**kwargs:calls.append((path,kwargs))
            engine=QQmlEngine();engine.rootContext().setContextProperty('backend',bridge)
            engine.rootContext().setContextProperty('systemDark',False)
            component=QQmlComponent(engine,QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/CameraResolution.qml').resolve())))
            item=component.createWithInitialProperties(dict(cameraKey='video2',cameraName='左腕相机',available=True,width=330))
            assert item is not None,[e.toString() for e in component.errors()]
            assert calls[-1][0]=='/umi/camera-controls/video2/resolution'
            mode=dict(id='1920x1080-MJPG-30',label='1920 × 1080 · 30 帧/秒',width=1920,height=1080,fps=30)
            calls[-1][1]['done'](dict(options=[mode],selected=mode,editable=True));app.processEvents()
            select=item.findChild(QObject,'cameraResolutionSelect')
            assert select.property('displayText')==mode['label']
            assert select.property('enabled')
            bridge.setCameraResolution('video2',mode['id'])
            assert calls[-1][1]['data']=={'mode_id':mode['id']}
            app.processEvents();assert not select.property('enabled')
            calls[-1][1]['failed']('设备未能输出所选分辨率');app.processEvents()
            assert bridge.umiCameraResolution['video2']['selected']==mode
            assert not bridge.umiCameraResolution['video2']['busy']
            item.setProperty('editable',False);assert not select.property('enabled')
            bridge._state['umiStep']=3
            before=len(calls);bridge.setCameraResolution('video2',mode['id']);assert len(calls)==before
            item.setProperty('available',False);item.deleteLater();app.processEvents()
        print('PASS native resolution selection, request, failed readback, recording lock')
    '''
    result=subprocess.run([sys.executable,'-c',textwrap.dedent(code)],cwd=root,
        env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QT_QUICK_BACKEND='software',PYTHONPATH=str(root/'scripts')),
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
