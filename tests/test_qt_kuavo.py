"""Render and exercise the new workspace without backend or hardware IO."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_kuavo_workspace_navigation_and_request_isolation(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=textwrap.dedent('''
        import tempfile
        from pathlib import Path
        from PySide6.QtCore import QUrl, QPointF, Qt, QObject
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType, QQmlProperty
        from PySide6.QtTest import QTest
        from qt_ui.bridge import Bridge
        from qt_ui.visuals import CameraItem, CurveItem, G2View, RobotScene, Backdrop
        app=QGuiApplication([])
        for name,cls in [('CameraItem',CameraItem),('CurveItem',CurveItem),('G2View',G2View),('Backdrop',Backdrop)]:
            qmlRegisterType(cls,'Collection.Native',1,0,name)
        with tempfile.TemporaryDirectory() as config:
            bridge=Bridge({},demo=True,settings_path=config)
            bridge.poll_timer.stop()
            bridge.update(ready=True,authenticated=True)
            calls=[];saves=[]
            original_demo=bridge.demo_action
            def demo_action(path,payload):
                if path=='/kuavo/motion':saves.append(payload.copy())
                return original_demo(path,payload)
            bridge.demo_action=demo_action
            bridge.action=lambda *args,**kw:calls.append((args,kw))
            bridge.configureTeleop('乐聚测试','kuavo-5w')
            assert bridge.state['robot']=='kuavo-5w' and bridge.state['teleopStep']==2
            assert not bridge.save_timer.isActive(), 'KUAVO must not save Tianji parameters'
            assert bridge.state['kuavoNamespace']=='current'
            bridge.teleop('check')
            assert calls[-1][0][1]=='/kuavo/check'
            assert calls[-1][0][2]['prepare_controller'] and calls[-1][0][2]['confirm_motion']
            assert calls[-1][0][2]['motor_ids_confirmed'] and calls[-1][0][2]['prepare_grippers']
            assert calls[-1][1]['timeout']==920000
            bridge.setValue('kuavoArmMode','left')
            bridge.setValue('kuavoPositionScale',.5)
            QTest.qWait(450)
            assert not bridge.state['kuavoMotionPending']
            bridge.teleop('start')
            assert calls[-1][0][1]=='/kuavo/start'
            assert calls[-1][0][2]['confirm_motion'] is True
            assert calls[-1][0][2]['network_namespace']=='current'
            assert calls[-1][0][2]['accept_unverified_gripper_bus'] is True
            assert calls[-1][0][2]['arm_mode']=='left' and calls[-1][0][2]['position_scale']==.5
            assert not bridge.can_move()
            status=dict(state='idle',active=False,control_ready=True,service={'management_ready':True,'service':{'ready_to_start':True}},
                        pico_sdk_available=True,vr_ready=True,pico_ready=True,vr_checked=True,preparation={},logs=[],limitations=['相机预览和同步数据集暂未接入'],session={})
            status['motion_settings']=dict(values=dict(position_scale=.5,max_velocity_rad_s=.7,planning_acceleration_rad_s2=5.),supported=True,saved=True)
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
            for collection,label in [({},'未开始遥操作'),({'armed':True},'等待手柄 A'),({'armed':True,'recording':True},'● 采集中'),({'busy':True},'正在保存'),({'state':'incomplete'},'本轮不完整')]:
                bridge.update(kuavo={**status,'collection':{**collection,'completed':2}})
                QTest.qWait(20)
                assert item('kuavoCompletedEpisodes').property('text')=='本次已完成  2 Episode'
                assert item('kuavoCollectionStatus').property('text')==label
            bridge.update(kuavo=status)
            assert all(item('kuavoCameraTile-'+role).isVisible() for role in ('head','left','right'))
            item('kuavoCameraSettings').clicked.emit();QTest.qWait(50)
            assert window.findChild(QObject,'kuavoCameraDialog').property('visible')
            assert all(item('cameraDraftResolution-'+role).property('count')==0 for role in ('head','left','right'))
            assert all(not item('cameraDraftBrightness-'+role).property('enabled') for role in ('head','left','right'))
            window.findChild(QObject,'kuavoCameraDialog').close();QTest.qWait(30)
            assert item('kuavoArmMode').property('model')==['双臂','双臂＋底盘','左臂','右臂']
            assert item('usageGuide').isVisible()
            assert item('usageGuide').property('source').toString().endswith('kuavo-usage-guide.png')
            assert not item('teleopWorkspace').isVisible()
            bridge.update(kuavo={**status,'state':'error','parameter_mode':'service','session_settings':None,'motion_controls':{}})
            QTest.qWait(30)
            for name in ('kuavoScale','kuavoVelocity','kuavoAcceleration'):
                assert item(name).isVisible() and not item(name).property('enabled')
            bridge.update(kuavo=status)
            bridge.update(kuavo={**status,'control_ready':False})
            QTest.qWait(20)
            assert not item('kuavoStart').property('enabled'), 'device checks alone are not session readiness'
            bridge.update(kuavo={**status,'control_ready':False,'can_restart':True})
            QTest.qWait(20)
            assert item('kuavoStart').property('enabled')
            bridge.teleop('start')
            request=calls[-1][0][2]
            assert calls[-1][0][1]=='/kuavo/start' and request['restart_after_stop']
            assert all(request[k] for k in ('prepare_controller','prepare_grippers','prepare_session','require_prepared','motor_ids_confirmed'))
            assert calls[-1][1]['timeout']==920000
            bridge.update(kuavo={**status,'state':'ready','active':True,'session_id':'prepared','control_ready':True})
            QTest.qWait(20)
            assert item('kuavoStart').isVisible() and item('kuavoStart').property('enabled')
            assert not item('kuavoStop').isVisible()
            assert item('kuavoScale').property('enabled')
            assert item('pageStepper').property('returnEnabled')
            for phase in ('checking','preparing'):
                bridge.update(kuavo={**status,'state':phase,'active':True},busy='check')
                QTest.qWait(20)
                assert not item('kuavoStop').isVisible()
                assert item('pageStepper').property('returnEnabled')
            bridge.update(busy='')
            bridge.update(kuavo={**status,'state':'starting','active':True,'session_id':'fake-session',
                'execution_notice':'机器人正在准备计算模块，手柄暂不生效',
                'preparation_progress':'厂家主控与初始化已就绪'})
            QTest.qWait(20)
            assert item('kuavoStop').isVisible() and item('kuavoStop').property('text')=='结束遥操作'
            assert not any(o.objectName() in ('kuavoExecutionNotice','kuavoPreparationProgress') for o in walk(window.contentItem()))
            bridge.update(error='button error',notice='button notice')
            assert not item('pageNotice').isVisible()
            bridge.update(error='',notice='')
            bridge.update(kuavo=status)
            QTest.qWait(20)
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
            assert not any(o.property('text')=='接受原项目夹爪与手臂共用 CAN 总线的使用条件（兼容性尚未完成验证）' for o in walk(window.contentItem()))
            item('kuavoVelocity').edited.emit(.4);QTest.qWait(20)
            assert not item('kuavoStart').property('enabled')
            assert not any(o.objectName() in ('kuavoMotionSave','kuavoConnection') for o in walk(window.contentItem()))
            QTest.qWait(450)
            assert saves[-1]==dict(position_scale=.5,max_velocity_rad_s=.4,planning_acceleration_rad_s2=5.)
            assert not bridge.state['kuavoMotionPending'] and not bridge.state['kuavoMotionSaving']
            assert not item('kuavoStart').property('enabled')
            bridge.setValue('kuavoVelocity',.7)
            QTest.qWait(450)
            assert item('kuavoStart').property('enabled')
            other=item('kuavoOtherParameters')
            point=other.mapToScene(QPointF(other.width()/2,other.height()/2)).toPoint()
            QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(50)
            dialog=window.findChild(__import__('PySide6.QtCore',fromlist=['QObject']).QObject,'kuavoGripperDialog')
            assert dialog.property('opened')
            window.grabWindow().save(str(screenshot.with_stem('kuavo-grippers')))
            assert not any(o.objectName()=='kuavoGripperClose' for o in walk(window.contentItem()))
            count=len(calls)
            original_open=dialog.property('draft').toVariant()['left']['open_rad']
            field=item('kuavo-left-open_rad');field.edited.emit(-2.5)
            QTest.qWait(450);assert len(calls)==count
            item('kuavoGripperCancel').clicked.emit();QTest.qWait(50)
            assert not dialog.property('opened') and len(calls)==count and not dialog.property('dirty')
            dialog.open();QTest.qWait(30)
            assert dialog.property('draft').toVariant()['left']['open_rad']==original_open
            item('kuavo-left-open_rad').edited.emit(-2.5)
            item('kuavoGripperConfirm').clicked.emit();QTest.qWait(30)
            assert not dialog.property('opened')
            assert len(calls)==count+1 and calls[-1][0][1]=='/kuavo/grippers'
            assert calls[-1][0][2]['left']['open_rad']==-2.5
            assert not item('kuavoStart').property('enabled')
            calls[-1][1]['on_failed']('offline validation failed');QTest.qWait(30)
            assert dialog.property('dirty') and dialog.property('error')=='offline validation failed'
            dialog.open();QTest.qWait(30)
            assert dialog.property('draft').toVariant()['left']['open_rad']==-2.5
            assert len(calls)==count+1
            item('kuavoGripperConfirm').clicked.emit();QTest.qWait(30)
            assert len(calls)==count+2
            saved_status={**status,'gripper_settings':dict(values=calls[-1][0][2],saved=True,matches_service=False)}
            calls[-1][1]['done'](saved_status);QTest.qWait(30)
            assert not dialog.property('opened') and not dialog.property('dirty') and not dialog.property('saving')
            dialog.open();QTest.qWait(30)
            assert dialog.property('draft').toVariant()['left']['open_rad']==-2.5
            item('kuavo-left-open_rad').edited.emit(-2.6)
            QTest.keyClick(window,Qt.Key_Escape);QTest.qWait(30)
            assert not dialog.property('opened') and len(calls)==count+2
            bridge.update(kuavo=status)
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
            before_click=len(calls)
            QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(50)
            assert len(calls)==before_click+1 and calls[-1][0][1]=='/kuavo/start'
            assert calls[-1][0][2]['confirm_motion'] is True
            assert window.findChild(QObject,'kuavoConfirm') is None
            bridge.update(kuavo={**status,'state':'running','active':True,'session_id':'test-session'})
            assert bridge.teleop_active()
            bridge.setValue('kuavoArmMode','right');bridge.setValue('kuavoPositionScale',.2)
            assert bridge.state['kuavoArmMode']=='left' and bridge.state['kuavoPositionScale']==.5
            bridge.returnTeleop();assert bridge.state['teleopStep']==2
            bridge.teleop('stop')
            assert calls[-1][0][1]=='/kuavo/stop'
            assert calls[-1][0][2]=={'session_id':'test-session'}
            bridge.update(kuavo=status)
            bridge.returnTeleop()
            assert calls[-1][0][1]=='/kuavo/cancel-preparation'
            calls[-1][1]['done'](status)
            assert bridge.state['teleopStep']==1
            bridge.update(error='',notice='')
            QTest.qWait(30)
            assert not item('usageGuide').isVisible()
            assert not item('pageTools').isVisible()
            assert item('robotType').property('model')==['天机 Marvin M6','乐聚 KUAVO 5-W','智元 G2']
            bridge.configureTeleop('天机','tianji')
            assert bridge.state['robot']=='tianji' and bridge.save_timer.isActive()
            bridge.save_timer.stop()
            original=dict(bridge.state['tianji'])
            bridge.update(paramSaved=True)
            QTest.qWait(30)
            assert item('usageGuide').isVisible() and not item('usageGuide').property('source').isEmpty()
            params=window.findChild(__import__('PySide6.QtCore',fromlist=['QObject']).QObject,'parameterDialog')
            params.open();QTest.qWait(30)
            previous=bridge.state['parameters']['right_gripper_speed']
            field=item('tianji-extra-right_gripper_speed')
            field.edited.emit(previous+.1);QTest.qWait(450)
            assert bridge.state['parameters']['right_gripper_speed']==previous
            assert not bridge.save_timer.isActive()
            # Escape discards drafts; confirm captures even a still-focused input.
            text_input=next(o for o in walk(field) if o.metaObject().className().startswith('TextField_'))
            text_input.forceActiveFocus();text_input.setProperty('text',str(previous+.2))
            QTest.keyClick(window,Qt.Key_Escape);QTest.qWait(450)
            assert not params.property('opened')
            assert bridge.state['parameters']['right_gripper_speed']==previous
            params.open();QTest.qWait(30)
            field=item('tianji-extra-right_gripper_speed')
            text_input=next(o for o in walk(field) if o.metaObject().className().startswith('TextField_'))
            text_input.forceActiveFocus();text_input.setProperty('text',str(previous+.2))
            item('tianjiParametersConfirm').clicked.emit();QTest.qWait(450)
            assert not params.property('opened')
            assert bridge.state['parameters']['right_gripper_speed']==previous+.2
            assert bridge.state['paramSaved'] and not bridge.state['paramSaving']
            for mode in ['dual','left','right']:
                bridge.setValue('uploadMode',mode);bridge.teleop('start')
                assert calls[-1][0][1]=='/tianji/start'
                assert calls[-1][0][2]['mode']==mode and calls[-1][0][2]['upload_mode']==mode
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
             PYTHONPATH=os.pathsep.join((str(root/'scripts'),str(root/'teleop_control/src'),os.environ.get('PYTHONPATH',''))),KUAVO_TEST_SCREENSHOT=str(tmp_path/'kuavo.png'))
    result=subprocess.run([sys.executable,'-c',script],cwd=root,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
