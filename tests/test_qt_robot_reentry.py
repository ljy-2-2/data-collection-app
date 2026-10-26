"""Real GPU-resource lifecycle regression, entirely offline with local meshes.

Opt in with RUN_QT_OPENGL_TESTS=1 on a host with working OpenGL 3.3.
Mesa software OpenGL is sufficient; Qt's 2D software renderer is not.
Set QT_OPENGL_TEST_PLATFORM=xcb to test through the desktop GL surface when
the platform's offscreen GLX framebuffer cannot be read back correctly.
"""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import pytest


@pytest.mark.skipif(os.environ.get('RUN_QT_OPENGL_TESTS')!='1',reason='Requires an OpenGL 3.3 rendering context')
def test_robot_models_render_after_reentry_and_switching(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=textwrap.dedent('''
        import tempfile
        import numpy as np
        from pathlib import Path
        from PySide6.QtCore import QUrl,QCoreApplication,QEvent,qInstallMessageHandler,QPoint,Qt
        from PySide6.QtGui import QGuiApplication,QSurfaceFormat
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtTest import QTest
        from shiboken6 import isValid,delete
        from qt_ui.bridge import Bridge
        from qt_ui.visuals import RobotScene,KuavoScene
        fmt=QSurfaceFormat();fmt.setVersion(3,3);fmt.setProfile(QSurfaceFormat.CoreProfile)
        QSurfaceFormat.setDefaultFormat(fmt)
        app=QGuiApplication([]);warnings=[]
        qInstallMessageHandler(lambda mode,ctx,msg:warnings.append(msg))
        config=tempfile.TemporaryDirectory()
        bridge=Bridge({},demo=True,settings_path=config.name);bridge.poll_timer.stop()
        scene_a=RobotScene(Path.cwd());scene_b=KuavoScene(Path.cwd())
        engine=QQmlApplicationEngine()
        for name,value in [('backend',bridge),('systemDark',False),('sceneA',scene_a),('sceneB',scene_b)]:
            engine.rootContext().setContextProperty(name,value)
        engine.loadData(b'import QtQuick\\nWindow {id:window;width:480;height:420;visible:true;property bool loaded:false;property bool useB:false;Loader {anchors.fill:parent;active:window.loaded;sourceComponent:Component {RobotView {scene:window.useB?sceneB:sceneA}}}}',
            QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/reentry-test.qml').resolve())))
        assert engine.rootObjects(),warnings
        window=engine.rootObjects()[0];baseline={};previous={}
        for turn in range(4):
            for name,scene in [('tianji',scene_a),('kuavo',scene_b)]:
                window.setProperty('useB',name=='kuavo')
                if turn==2:
                    if name=='tianji':scene.update_pose({'joints':{'left':[30,-25,0,0,0,0,0]}})
                    else:scene.update_pose({'telemetry':{'joints':{'zarm_l1_joint':-.6,'zarm_l4_joint':-.8}}})
                    scene.drag(8,3);scene.changeZoom(1.1)
                matrices=[p.matrix.copy() for p in scene.parts_value]
                view=(scene.yaw,scene.pitch,scene.zoom)
                window.setProperty('loaded',True);QTest.qWait(100)
                assert all(np.array_equal(p.matrix,m) for p,m in zip(scene.parts_value,matrices))
                assert view==(scene.yaw,scene.pitch,scene.zoom)
                assert len(scene.findChildren(type(next(iter(scene.geometries.values())))))==len(scene.geometries)
                if name in previous:assert all(not isValid(g) for g in previous[name])
                previous[name]=list(scene.geometries.values())
                shot=window.grabWindow();assert not shot.isNull()
                # Crop excludes buttons. A blank or stale view cannot pass.
                crop=shot.copy(0,60,480,350)
                pixels=bytes(crop.constBits())
                assert len(set(pixels))>30,'model was not rendered'
                epoch=turn//2
                key=(name,epoch)
                if key in baseline:assert pixels==baseline[key],(name,turn,'reentry changed the rendered model')
                else:baseline[key]=pixels
                if turn==2:assert pixels!=baseline[(name,0)],'pose/view updates were lost'
                shot.save(str(Path(__import__('sys').argv[1])/f'{name}-{turn}.png'))
                if turn==0:
                    before_view=(scene.yaw,scene.pitch,scene.zoom)
                    before_camera=scene.camera_matrix.copy()
                    before_parts=[p.matrix.copy() for p in scene.parts_value]
                    QTest.mousePress(window,Qt.RightButton,Qt.NoModifier,QPoint(200,200))
                    QTest.mouseMove(window,QPoint(230,220),20)
                    QTest.mouseRelease(window,Qt.RightButton,Qt.NoModifier,QPoint(230,220))
                    QTest.qWait(100)
                    assert (scene.yaw,scene.pitch,scene.zoom)==before_view
                    assert scene.pan_x>0 and scene.pan_y>0
                    assert not np.array_equal(scene.camera_matrix,before_camera)
                    assert all(np.array_equal(p.matrix,m) for p,m in zip(scene.parts_value,before_parts))
                    panned=window.grabWindow().copy(0,60,480,350)
                    assert bytes(panned.constBits())!=pixels
                    scene.resetView();QTest.qWait(100)
                    assert scene.pan_x==scene.pan_y==0
                    restored=window.grabWindow().copy(0,60,480,350)
                    assert bytes(restored.constBits())==pixels
                window.setProperty('loaded',False);QTest.qWait(50)
                engine.collectGarbage();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        window.close();delete(engine);bridge.shutdown()
        assert not warnings,warnings[:10]
    ''')
    env={**os.environ,'QT_QPA_PLATFORM':os.environ.get('QT_OPENGL_TEST_PLATFORM','offscreen'),'QT_QUICK_BACKEND':'rhi',
         'QSG_RHI_BACKEND':'opengl','QSG_RENDER_LOOP':'basic','LIBGL_ALWAYS_SOFTWARE':'1',
         'PYTHONPATH':os.pathsep.join((str(root/'scripts'),str(root/'teleop_control/src'),os.environ.get('PYTHONPATH','')))}
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],cwd=root,env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
