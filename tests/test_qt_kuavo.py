"""Render and exercise the new workspace without backend or hardware IO."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import pytest


@pytest.mark.parametrize('collection_mode', ['all', 'umi-tianji-kuavo'])
def test_kuavo_workspace_navigation_and_request_isolation(tmp_path, collection_mode):
    root=Path(__file__).resolve().parents[1]
    script=textwrap.dedent('''
        import tempfile
        from pathlib import Path
        from PySide6.QtCore import QUrl, QPointF, Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType, QQmlProperty
        from PySide6.QtTest import QTest
        from qt_ui.bridge import Bridge
        from qt_ui.visuals import CameraItem, CurveItem, G2View, RobotScene, Backdrop
        app=QGuiApplication([])
        for name,cls in [('CameraItem',CameraItem),('CurveItem',CurveItem),('G2View',G2View),('Backdrop',Backdrop)]:
            qmlRegisterType(cls,'Collection.Native',1,0,name)
        with tempfile.TemporaryDirectory() as config:
            bridge=Bridge({'APP_COLLECTION_MODE': __import__('os').environ['APP_COLLECTION_MODE']},demo=True,settings_path=config)
            bridge.poll_timer.stop()
            bridge.update(ready=True,authenticated=True)
            calls=[]
            bridge.action=lambda *args,**kw:calls.append((args,kw))
            bridge.configureTeleop('乐聚测试','kuavo-5w')
            assert bridge.state['robot']=='kuavo-5w' and bridge.state['teleopStep']==2
            assert not bridge.save_timer.isActive(), 'KUAVO must not save Tianji parameters'
            assert bridge.state['kuavoNamespace']=='current'
            bridge.teleop('check')
            assert calls[-1][0][1]=='/kuavo/check'
            bridge.setValue('kuavoBusAccepted',True)
            bridge.setValue('kuavoArmMode','left')
            bridge.setValue('kuavoPositionScale',.5)
            bridge.teleop('start')
            assert calls[-1][0][1]=='/kuavo/start'
            assert calls[-1][0][2]['confirm_motion'] is True
            assert calls[-1][0][2]['arm_mode']=='left' and calls[-1][0][2]['position_scale']==.5
            assert not bridge.can_move()
            status=dict(state='idle',active=False,service={'management_ready':True,'service':{'ready_to_start':True}},
                        pico_sdk_available=True,vr_ready=True,pico_ready=True,vr_checked=True,preparation={},logs=[],limitations=['相机预览和同步数据集暂未接入'],session={})
            from teleop_control.kuavo_preview import KuavoModel
            model=KuavoModel();status.update(reference=model.reference,segments=model.segments())
            bridge.update(kuavo=status)
            engine=QQmlApplicationEngine();warnings=[]
            engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
            scene=RobotScene(Path.cwd(),load_model=False)
            for key,value in [('backend',bridge),('systemDark',False),('robotScene',scene)]:
                engine.rootContext().setContextProperty(key,value)
            engine.load(QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/Main.qml').resolve())))
            assert engine.rootObjects(), warnings
            window=engine.rootObjects()[0]
            QTest.qWait(150)
            def walk(obj):
                yield obj
                for child in obj.childItems():yield from walk(child)
            def item(name):return next(o for o in walk(window.contentItem()) if o.objectName()==name)
            assert item('kuavoWorkspace').isVisible()
            assert not item('usageGuide').isVisible(), 'Do not show Tianji button instructions for KUAVO'
            assert not item('teleopWorkspace').isVisible()
            from shiboken6 import isValid
            layered=[child for child in walk(window.contentItem()) if QQmlProperty(child,'layer.enabled').read()]
            for child in layered:
                if isValid(child):QQmlProperty(child,'layer.enabled').write(False)
            QTest.qWait(100)
            screenshot=Path(__import__('os').environ['KUAVO_TEST_SCREENSHOT'])
            for width,columns in [(1600,2),(768,1),(360,1)]:
                window.setWidth(width);QTest.qWait(80)
                grid=item('kuavoVisualGrid')
                assert grid.property('columns')==columns
                robot=item('kuavoRobotCard');camera=item('kuavoCameraCard')
                assert robot.width()<=grid.width()+1 and camera.width()<=grid.width()+1
                if columns==2:assert abs(robot.y()-camera.y())<1
                else:assert camera.y()>=robot.y()+robot.height()
                window.grabWindow().save(str(screenshot.with_stem('kuavo-'+str(width))))
            window.setWidth(1280);QTest.qWait(80)
            window.grabWindow().save(str(screenshot))
            other=item('kuavoOtherParameters')
            point=other.mapToScene(QPointF(other.width()/2,other.height()/2)).toPoint()
            QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(50)
            dialog=window.findChild(__import__('PySide6.QtCore',fromlist=['QObject']).QObject,'kuavoGripperDialog')
            assert dialog.property('opened')
            window.grabWindow().save(str(screenshot.with_stem('kuavo-grippers')))
            # Cancel discards a draft; reopening starts from persisted values.
            count=len(calls)
            field=item('kuavo-left-open_rad')
            field.edited.emit(-2.5)
            assert dialog.property('draft').toVariant()['left']['open_rad']==-2.5
            dialog.close();QTest.qWait(30)
            assert len(calls)==count
            dialog.open();QTest.qWait(30)
            assert dialog.property('draft').toVariant()['left']['open_rad']==-2.67
            item('kuavo-left-open_rad').edited.emit(-2.5)
            save=item('kuavoGripperSave')
            point=save.mapToScene(QPointF(save.width()/2,save.height()/2)).toPoint()
            QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(30)
            assert calls[-1][0][1]=='/kuavo/grippers'
            assert calls[-1][0][2]['left']['open_rad']==-2.5
            assert set(calls[-1][0][2]['left'])=={'open_rad','closed_rad','speed_rad_s','accel_rad_s2'}
            # Failed saves keep the draft; success closes the dialog.
            calls[-1][1]['on_failed']('offline validation failed');QTest.qWait(30)
            assert dialog.property('opened') and dialog.property('error')=='offline validation failed'
            dialog.setProperty('saving',True)
            calls[-1][1]['done'](status);QTest.qWait(30)
            assert not dialog.property('opened')
            connect=item('kuavoConnection')
            point=connect.mapToScene(QPointF(connect.width()/2,connect.height()/2)).toPoint()
            QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(30)
            connection=window.findChild(__import__('PySide6.QtCore',fromlist=['QObject']).QObject,'kuavoConnectionDialog')
            assert connection.property('opened')
            token_field=item('kuavoConnectionToken')
            token_field.setProperty('text','b'*64)
            connection.close();QTest.qWait(30)
            assert token_field.property('text')==''
            start=item('kuavoStart');assert start.property('enabled')
            assert all(d['status']=='connected' for d in item('kuavoDevices').property('items').toVariant())
            bridge.update(kuavo={**status,'vr_ready':False})
            QTest.qWait(20)
            assert not start.property('enabled')
            checked=item('kuavoDevices').property('items').toVariant()
            assert checked[1]['status']=='offline' and checked[2]['status']=='connected'
            bridge.update(kuavo=status);QTest.qWait(20)
            assert start.property('enabled')
            point=start.mapToScene(QPointF(start.width()/2,start.height()/2)).toPoint()
            QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(50)
            assert engine.rootObjects()[0].findChild(__import__('PySide6.QtCore',fromlist=['QObject']).QObject,'kuavoConfirm').property('opened')
            bridge.update(kuavo={**status,'state':'running','active':True,'session_id':'test-session'})
            assert bridge.teleop_active()
            bridge.setValue('kuavoArmMode','right');bridge.setValue('kuavoPositionScale',.2)
            assert bridge.state['kuavoArmMode']=='left' and bridge.state['kuavoPositionScale']==.5
            bridge.returnTeleop();assert bridge.state['teleopStep']==2
            bridge.teleop('stop')
            assert calls[-1][0][1]=='/kuavo/stop'
            assert calls[-1][0][2]=={'session_id':'test-session'}
            bridge.update(kuavo=status)
            bridge.returnTeleop();assert bridge.state['teleopStep']==1
            bridge.configureTeleop('天机','tianji')
            assert bridge.state['robot']=='tianji' and bridge.save_timer.isActive()
            bridge.save_timer.stop()
            original=dict(bridge.state['tianji'])
            for state,active,label,can_check in [
                ('checking',False,'开始遥操作',False),
                ('unknown',False,'开始遥操作',True),
                ('error',False,'开始遥操作',True),
                ('unknown',True,'停止控制',False),
                ('running',True,'停止控制',False),
            ]:
                bridge.update(busy='',paramSaving=False,paramSaved=True,tianji={**original,'state':state,'control_active':active})
                QTest.qWait(20)
                assert item('teleopStartStop').property('text')==label,(state,active)
                assert item('teleopCheck').property('enabled')==can_check,(state,active)
                if not active:assert not item('teleopStartStop').property('enabled')
            bridge.shutdown()
            assert not warnings, warnings
    ''')
    env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QT_QUICK_BACKEND='software',QSG_RHI_BACKEND='software',
             APP_COLLECTION_MODE=collection_mode,
             PYTHONPATH=os.pathsep.join((str(root/'scripts'),str(root/'teleop_control/src'),os.environ.get('PYTHONPATH',''))),KUAVO_TEST_SCREENSHOT=str(tmp_path/'kuavo.png'))
    result=subprocess.run([sys.executable,'-c',script],cwd=root,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
