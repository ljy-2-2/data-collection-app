#!/usr/bin/env python3
"""Qt integration/visual smoke test. Only an ephemeral loopback fixture is contacted."""
from __future__ import annotations
import argparse
import asyncio
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
os.environ.setdefault('QSG_RHI_BACKEND','opengl')
os.environ.setdefault('QSG_RENDER_LOOP','basic')
import faulthandler
faulthandler.dump_traceback_later(90,exit=True)
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'teleop_control/src'))
from PySide6.QtCore import QEventLoop,QTimer,QObject,QPoint,QPointF,QUrl,QBuffer,QByteArray,QIODevice,qInstallMessageHandler,Qt
from PySide6.QtGui import QGuiApplication,QImage,QColor
from PySide6.QtQml import QQmlApplicationEngine,qmlRegisterType
from PySide6.QtTest import QTest
from qt_ui.bridge import Bridge,initial_state,G2_JOINTS
from qt_ui.visuals import CameraItem,CurveItem,Backdrop,G2View,RobotScene
from qt_ui.record_keys import UmiRecordKeys
from teleop_control.tianji_models import TianjiParameters,TianjiStart,TianjiMove
from teleop_control.models import UmiStartRequest,UmiDeviceConfigureRequest,StartSessionRequest,MoveRequest
from teleop_control.umi_telemetry import telemetry_events


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--screenshots',type=Path);args=parser.parse_args()
    app=QGuiApplication(sys.argv[:1]);warnings=[]
    qInstallMessageHandler(lambda kind,context,message:warnings.append(message))
    for name,cls in [('CameraItem',CameraItem),('CurveItem',CurveItem),('Backdrop',Backdrop),('G2View',G2View)]:qmlRegisterType(cls,'Collection.Native',1,0,name)
    frame=QImage(160,120,QImage.Format_RGB32);frame.fill(QColor('#537a93'))
    data=QByteArray();buffer=QBuffer(data);buffer.open(QIODevice.WriteOnly);assert frame.save(buffer,'JPEG');jpeg=bytes(data)
    camera_colors={'usb_4':'#b74444','usb_1':'#386bbb','usb_2':'#34956c',
                   'hand_left_color':'#b74444','head_color':'#386bbb','hand_right_color':'#34956c'}
    camera_frames={}
    for camera_id,color in camera_colors.items():
        sample=QImage(160,120,QImage.Format_RGB32);sample.fill(QColor(color))
        encoded=QByteArray();output=QBuffer(encoded);output.open(QIODevice.WriteOnly)
        assert sample.save(output,'JPEG');camera_frames[camera_id]=bytes(encoded)
    state=initial_state();requests=[];failures=[]
    targets=[dict(kind='cloud',api_url='https://cloud.test/api',web_url='https://cloud.test'),
             dict(kind='local',api_url='http://127.0.0.1:8000',web_url='http://127.0.0.1:5173')]
    state['uploadTarget']=dict(selected='cloud',default_kind='cloud',destinations=targets,**targets[0])
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path.startswith('/umi/camera-controls/') and self.path.endswith('/resolution'):
                mode=dict(id='1920x1080-MJPG-30',width=1920,height=1080,fps=30,fourcc='MJPG',pixel_format='mjpeg2rgb',label='1920 × 1080 · 30 帧/秒')
                self.respond(dict(options=[mode],selected=mode,editable=state['umi'].get('state')!='ready'))
                return
            if self.path == '/api/state':
                stamp=time.time()
                self.respond(dict(ros_connected=True,ts=stamp,
                    vr={key:dict(online=True,rate_hz=90,position=[stamp%1,.2,.3],rotation=[0,0,0,1]) for key in ('hmd','left','right')},
                    encoders={side:dict(online=True,rate_hz=100,position=stamp%1) for side in ('left','right')}))
                return
            if self.path == '/umi/telemetry':
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                async def send():
                    stream=telemetry_events(f'http://127.0.0.1:{server.server_port}')
                    try:
                        async for event in stream:
                            self.wfile.write(event.encode());self.wfile.flush()
                    except (BrokenPipeError,ConnectionResetError):pass
                    finally:await stream.aclose()
                asyncio.run(send())
                return
            if '/cameras/' in self.path or '/device-cameras/' in self.path:
                image=camera_frames.get(self.path.split('/')[-2],jpeg)
                if self.path.startswith('/cameras/') and self.path.endswith('/frame'):
                    self.send_response(200);self.send_header('Content-Type','image/jpeg');self.end_headers()
                    try:self.wfile.write(image)
                    except (BrokenPipeError,ConnectionResetError):pass
                    return
                self.send_response(200);self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame');self.end_headers()
                try:self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(image)).encode()+b'\r\n\r\n'+image+b'\r\n--frame--\r\n')
                except (BrokenPipeError,ConnectionResetError):pass
                return
            mapping={'/platform/destinations':state['uploadTarget'],'/platform/auth/preferences':{},'/tianji/status':state['tianji'],'/status':state['g2'],'/preview':state['preview'],'/umi/status':state['umi']}
            if self.path.startswith('/umi/encoders/preview'):value={'encoders':{'left':{'value':.12,'online':True},'right':{'value':.34,'online':True}}}
            else:value=mapping.get(self.path,{})
            self.respond(value)
        def respond(self,value,code=200):
            body=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_POST(self):
            raw=self.rfile.read(int(self.headers.get('Content-Length',0)));payload=json.loads(raw) if raw else None;path=self.path;requests.append((path,payload));value={}
            try:
                if path=='/platform/auth/login':value=dict(authenticated=True,user=dict(username=payload['username']))
                if path=='/platform/destinations':
                    target=next(t for t in targets if t['kind']==payload['kind'])
                    state['uploadTarget']=dict(selected=target['kind'],default_kind='cloud',destinations=targets,**target)
                    value=dict(state['uploadTarget'],authenticated=True,user=dict(username='fixture'),preferences={})
                if path=='/tianji/parameters':TianjiParameters.model_validate(payload);value=payload
                if path=='/tianji/check':state['tianji'].update(devices=[{**d,'status':'connected'} for d in state['tianji']['devices']],joints={'left':[0]*7,'right':[0]*7},cameras=[dict(id=k,available=True) for k in ('usb_1','usb_2','usb_4')])
                if path=='/tianji/start':TianjiStart.model_validate(payload);state['tianji']['state']='running'
                if path=='/tianji/stop':state['tianji']['state']='idle'
                if path=='/tianji/move':TianjiMove.model_validate(payload)
                if path=='/devices/check':
                    state['g2']['devices']=[{**d,'status':'connected'} for d in state['g2']['devices']];state['preview'].update(available=True,joint_positions={j['name']:j['default'] for j in G2_JOINTS},cameras=[dict(id=k,available=True) for k in ('head_color','hand_left_color','hand_right_color')])
                if path=='/sessions':StartSessionRequest.model_validate(payload);state['g2']['session']={'state':'running','session_id':'g2-fixture'};value={'session_id':'g2-fixture'}
                if path=='/sessions/g2-fixture/stop':state['g2']['session']={'state':'idle'}
                if path=='/move':MoveRequest.model_validate(payload)
                if path.startswith('/umi/check'):
                    state['umi']['monitor']={'device_check':dict(camera_candidates=['/dev/video0','/dev/video2','/dev/video4'],encoder_candidates=['/dev/ttyACM0','/dev/ttyACM1'],hardware_ready=True,pico_ready=True,vr_service_ready=True)};value=state['umi']
                if path=='/umi/configure':UmiDeviceConfigureRequest.model_validate(payload)
                if path=='/umi/sessions':UmiStartRequest.model_validate(payload);state['umi'].update(state='ready',session_id='umi-fixture',monitor_connected=True);value=state['umi']
                if path.startswith('/umi/record/'):
                    action=path.rsplit('/',1)[1];state['umi'].update(state='recording' if action=='start' else 'ready',recording=action=='start');value=state['umi']
                    if action=='start':state['umi']['monitor']['episode']={'elapsed_sec':0}
                if path=='/umi/sessions/umi-fixture/stop':state['umi'].update(state='idle',recording=False,session_id=None,monitor={});value=state['umi']
                self.respond(value)
            except Exception as exc:failures.append(str(exc));self.respond({'detail':str(exc)},422)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='qt-ui-test-') as config:
        bridge=Bridge({'LOCAL_CONTROL_URL':f'http://127.0.0.1:{server.server_port}'},settings_path=config)
        scene=RobotScene(ROOT);bridge.changed.connect(lambda:scene.update_pose(bridge.state['tianji']))
        engine=QQmlApplicationEngine();context=engine.rootContext()
        context.setContextProperty('backend',bridge);context.setContextProperty('robotScene',scene);context.setContextProperty('systemDark',True)
        engine.load(QUrl.fromLocalFile(str(ROOT/'scripts/qt_ui/qml/Main.qml')))
        assert engine.rootObjects(),warnings
        window=engine.rootObjects()[0];window.resize(1024,640)
        record_keys=UmiRecordKeys(app,window,bridge)
        def wait(predicate=lambda:True,timeout=6000):
            deadline=time.monotonic()+timeout/1000
            QTest.qWait(80)
            while not predicate():
                assert time.monotonic()<deadline,(bridge.state,failures,warnings[-12:]);QTest.qWait(40)
        def item(name):
            result=window.findChild(QObject,name)
            if result is None:
                pending=[window.contentItem()]
                while pending:
                    candidate=pending.pop()
                    if candidate.objectName()==name:result=candidate;break
                    pending.extend(candidate.childItems())
            assert result is not None,name
            return result
        def click(name):
            control=item(name);assert control.property('enabled'),name
            point=control.mapToScene(QPointF(control.width()/2,control.height()/2)).toPoint()
            QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point);wait()
        def snap(name):
            print('CHECK',name,flush=True)
            if args.screenshots:
                args.screenshots.mkdir(parents=True,exist_ok=True);QTest.qWait(200)
                assert window.grabWindow().save(str(args.screenshots/(name+'.png')))
        def verify_three_cameras(robot):
            roles=('left','center','right')
            ids=('usb_4','usb_1','usb_2') if robot=='tianji' else ('hand_left_color','head_color','hand_right_color')
            cameras=[item('teleopCamera-'+role) for role in roles]
            wait(lambda:all(camera.online for camera in cameras))
            for role,camera,camera_id,label in zip(roles,cameras,ids,('左腕部相机','主相机','右腕部相机')):
                assert '/'+camera_id+'/' in camera.source,camera.source
                assert item('teleopCameraLabel-'+role).property('text')==label
                actual=camera.frame.pixelColor(20,20);expected=QColor(camera_colors[camera_id])
                assert max(abs(actual.red()-expected.red()),abs(actual.green()-expected.green()),abs(actual.blue()-expected.blue()))<=4
            tiles=[item('teleopCameraTile-'+role) for role in roles]
            left,main,right=tiles
            assert main.x()==0 and left.x()==right.x()>main.x()+main.width()
            assert main.y()==left.y()==0 and right.y()>left.height()
            assert main.width()>left.width() and main.height()>left.height()
            assert abs(right.y()+right.height()-main.height())<.01
            sources=[camera.source for camera in cameras]
            geometry=[(t.x(),t.y(),t.width(),t.height()) for t in tiles]
            bridge.setValue(robot+'CameraLayout','right-center-left');wait()
            assert [(t.x(),t.y(),t.width(),t.height()) for t in tiles]==geometry
            assert [camera.source for camera in cameras]==sources
            assert item('teleopCameraCard').height()==373 and item('teleopCameraImages').height()==302
            for name in ('teleopCameraLayout','teleopCameraImport','teleopCameraFileDialog'):
                assert window.findChild(QObject,name) is None,name
            scroll=item('pageScroll');card=item('teleopCameraCard')
            scroll.setProperty('contentY',scroll.property('contentY')+card.mapToScene(QPointF(0,0)).y()-180)
            wait();snap(robot+'-three-cameras')
            scroll.setProperty('contentY',0);wait()
            print('PASS '+robot+' three camera identities, main-left/wrists-right layout and removed settings',flush=True)
        print("Qt fixture loaded",flush=True)
        bridge.serviceReady();wait();snap('login')
        # Deliberately return a cached valid session from the fixture. Merely
        # choosing the platform on the login screen must never dismiss it.
        chooser=item('loginUploadTarget')
        wait(lambda:chooser.property('count')==2 and chooser.isVisible())
        window.requestActivate();wait()
        for _ in range(3):
            for key,kind in ((Qt.Key_End,'local'),(Qt.Key_Home,'cloud')):
                chooser.forceActiveFocus();QTest.keyClick(window,key)
                wait(lambda:bridge.state['uploadTarget'].get('selected')==kind and not bridge.state['busy'])
                assert not bridge.state['authenticated']
                assert item('loginPage').isVisible() and not item('collectionPage').isVisible()
        assert not any(path=='/platform/auth/login' for path,_ in requests)
        print('PASS real login selector: six local/cloud switches never bypass login',flush=True)
        item('loginUsername').setProperty('text','operator');item('loginPassword').setProperty('text','fixture-password')
        click('loginSubmit');wait(lambda:bridge.state['authenticated']);snap('setup')
        for _ in range(3):
            for page in ('umi','ego','teleop'):
                click('nav-'+page)
                assert bridge.state['page']==page and bridge.state['authenticated']
                assert bridge.state['uploadTarget']['selected']=='cloud'
        print('PASS real navigation buttons preserve login and destination',flush=True)
        # Exercise user selection, then a state notification/poll before advancing.
        robot_type=item('robotType');robot_type.forceActiveFocus()
        QTest.keyClick(window,Qt.Key_Home)
        wait(lambda:bridge.state['robot']=='agibot-g2')
        bridge.update(notice='型号选择刷新检查');QTest.qWait(500)
        assert robot_type.property('currentIndex')==0
        QTest.keyClick(window,Qt.Key_End)
        wait(lambda:bridge.state['robot']=='tianji')
        bridge.update(notice='');wait()
        assert robot_type.property('currentIndex')==1
        click('teleopNext');wait(lambda:bridge.state['paramSaved']);snap('tianji')
        bridge.setValue('robot','agibot-g2')
        assert bridge.state['robot']=='tianji', 'Do not switch the active workspace from the setup selector'

        click('otherParameters')
        assert {'left_gripper_accel','right_gripper_accel'} <= {field['key'] for field in bridge.fields}
        assert not {'kp','kd'} & {field['key'] for field in bridge.fields}
        assert not {'kp','kd'} & set(bridge.state['parameters'])
        snap('params');item('parameterDialog').setProperty('visible',False)
        bridge.setParameter('left_gripper_speed',8.5);wait(lambda:bridge.state['paramSaved']);assert requests[-1][0]=='/tianji/parameters'
        bridge.setParameter('left_gripper_accel',12)
        bridge.setParameter('right_gripper_accel',35)
        wait(lambda:bridge.state['paramSaved'])
        assert requests[-1][1]['left_gripper_accel']==12 and requests[-1][1]['right_gripper_accel']==35
        click('teleopCheck');wait(lambda:bridge.can_move())
        verify_three_cameras('tianji')
        bridge.setValue('uploadMode','left');click('teleopStartStop');wait(lambda:bridge.state['tianji']['state']=='running')
        start=next(p for path,p in reversed(requests) if path=='/tianji/start');assert start['mode']=='dual' and start['upload_mode']=='left' and start['parameters']['left_gripper_speed']==8.5
        assert start['parameters']['left_gripper_accel']==12 and start['parameters']['right_gripper_accel']==35
        before=len(requests);bridge.jog('left',1,1);wait();assert len(requests)==before,'Motion must be blocked while teleoperating'
        click('teleopStartStop');wait(lambda:bridge.state['tianji']['state']=='idle')
        bridge.jog('left',1,1);wait(lambda:not bridge.state['busy']);assert requests[-1]==('/tianji/move',dict(kind='jog',mode='dual',side='left',joint=1,delta=-10))
        bridge.savePose('测试姿态');assert len(bridge.state['poses'])==1
        bridge.restorePose();wait(lambda:not bridge.state['busy']);assert requests[-1][1]['kind']=='pose'
        scroll=item('pageScroll');scroll.setProperty('contentY',500);wait();snap('tianji-model')
        control=item('jog-left0-1');point=control.mapToScene(QPointF(0,0));scroll.setProperty('contentY',scroll.property('contentY')+point.y()-230);wait()
        click('jog-left0-1');wait(lambda:not bridge.state['busy']);assert requests[-1][1]['joint']==1 and requests[-1][1]['delta']==-10
        snap('manual');scroll.setProperty('contentY',0)
        # Qt transforms every visual from the original CAD file, without proxies.
        assert len(scene.parts_value)==sum(len(link['visuals']) for link in scene.model['links'])
        old=[part.matrix.copy() for part in scene.parts_value];scene.update_pose({'joints':{'left':[30,0,0,0,0,0,0]}})
        assert any(not (a==b.matrix).all() for a,b in zip(old,scene.parts_value))
        bridge.returnTeleop();wait(lambda:bridge.state['teleopStep']==1)
        robot_type=item('robotType');robot_type.forceActiveFocus()
        QTest.keyClick(window,Qt.Key_Home)
        wait(lambda:bridge.state['robot']=='agibot-g2')
        bridge.update(notice='刷新后保持 G2');QTest.qWait(500)
        assert robot_type.property('currentIndex')==0
        bridge.update(notice='');wait()
        click('teleopNext');wait(lambda:bridge.state['teleopStep']==2)
        assert bridge.state['robot']=='agibot-g2'
        print('PASS robot selection survives state refresh and Next preserves G2',flush=True)
        click('teleopCheck');wait(lambda:bridge.can_move())
        verify_three_cameras('g2')
        bridge.jogG2(G2_JOINTS[0]['name'],1);wait(lambda:not bridge.state['busy']);assert requests[-1][0]=='/move'
        click('teleopStartStop');wait(lambda:bridge.state['g2']['session']['state']=='running')
        click('teleopStartStop');wait(lambda:bridge.state['g2']['session']['state']=='idle');snap('g2')
        bridge.navigate('umi');snap('umi-setup');click('umiNext');snap('umi-check');click('umiCheck')
        wait(lambda:len(bridge.state['cameraLive'])==3 and all(bridge.state['cameraLive'].values()) and bool(bridge.state['encoders']))
        bridge.encoderMinimum('left',True);bridge.encoderMinimum('right',True);snap('umi-hardware')
        chooser=item('umiUploadTarget');assert chooser.property('enabled')
        bridge.selectUploadTarget('local');wait(lambda:bridge.state['uploadTarget'].get('selected')=='local' and not bridge.state['busy'])
        assert bridge.platformLocation=='本地' and bridge.state['umiStep']==2
        assert bridge.state['minima']=={'left':.12,'right':.34}
        wait(lambda:all(bridge.state['cameraLive'].values()))
        snap('umi-local-target')
        # Move the real button into view for the full hardware flow.
        scroll.setProperty('contentY',scroll.property('contentHeight')-scroll.height());wait();click('umiStart')
        wait(lambda:bridge.state['umiStep']==3 and bridge.state['umi'].get('monitor_connected'))
        before=len(requests);bridge.selectUploadTarget('cloud');wait()
        assert len(requests)==before and bridge.state['uploadTarget']['selected']=='local'
        bridge.clearError()
        print('PASS UMI destination selection reaches API and is locked during the collection session',flush=True)
        configuration=next(p for path,p in requests if path=='/umi/configure');assert configuration['encoder_minima']=={'left':.12,'right':.34};assert len(set(configuration['camera_roles'].values()))==3
        scroll.setProperty('contentY',0);snap('umi-monitor')
        wait(lambda:bridge.umiTelemetry.get('monitor_connected'))
        live_updates=[];slow_updates=[]
        def live_sample():live_updates.append(time.monotonic())
        def slow_sample():slow_updates.append(time.monotonic())
        bridge.umiTelemetryChanged.connect(live_sample);bridge.changed.connect(slow_sample)
        live_loop=QEventLoop();QTimer.singleShot(2200,live_loop.quit);live_loop.exec()
        bridge.umiTelemetryChanged.disconnect(live_sample);bridge.changed.disconnect(slow_sample)
        hz=(len(live_updates)-1)/(live_updates[-1]-live_updates[0])
        assert 23<=hz<=35,(hz,len(live_updates),warnings[-10:])
        assert len(slow_updates)<20,slow_updates
        assert len(bridge.umiHistory['left'])>=40
        assert len(set(bridge.umiHistory['left']))>=30
        print(f'PASS: live UMI telemetry {hz:.1f} Hz; slow UI notifications {len(slow_updates)}',flush=True)
        # Exercise actual pedal key events with a real focused QML button.
        window.requestActivate();item('umiRecord').forceActiveFocus();wait()
        before=len([path for path,_ in requests if path.startswith('/umi/record/')])
        QTest.keyPress(window,Qt.Key_Space)
        wait(lambda:bridge.state['umi']['recording'] and not bridge.state['busy'])
        wait(lambda:item('umiEpisodeDuration').property('text')=='00:00')
        state['umi']['monitor']['episode']={'elapsed_sec':125.8}
        wait(lambda:item('umiEpisodeDuration').property('text')=='02:05')
        duration=item('umiEpisodeDuration')
        print('DURATION_GEOMETRY',duration.width(),duration.height(),duration.parentItem().width(),flush=True)
        assert duration.width()>=60, 'Timer text exists but its layout has collapsed'
        for viewport in ((2180,1040),(1280,800),(1024,640),(560,700)):
            window.resize(*viewport);wait()
            assert duration.width()>=60,(viewport,duration.width())
            assert duration.isVisible() and duration.height()>0
        window.resize(1280,800);wait()
        scroll.setProperty('contentY',max(0,scroll.property('contentHeight')-scroll.height()));wait()
        position=duration.mapToScene(QPointF(0,0))
        assert 0<=position.y()<window.height()-duration.height(),position
        snap('umi-visible-duration')
        window.resize(1024,640);wait()


        QTest.keyRelease(window,Qt.Key_Space);wait()
        assert bridge.state['umi']['recording'], 'Space release must not click the focused button again'
        QTest.keyClick(window,Qt.Key_Space)
        wait(lambda:not bridge.state['umi']['recording'] and not bridge.state['busy'])
        assert item('umiEpisodeDuration').property('text')=='02:05'
        print('PASS current recording duration resets, formats minutes/seconds and remains visible after stop',flush=True)
        actions=[path for path,_ in requests if path.startswith('/umi/record/')][before:]
        assert actions==['/umi/record/start','/umi/record/stop'],actions
        print('PASS Space pedal emits exactly one start and one save/stop through the recording API',flush=True)
        bridge.stopUmi();wait(lambda:bridge.state['umiStep']==1)
        assert bridge.telemetry.reply is None and not bridge.telemetry.retry.isActive()
        assert not bridge.umiTelemetry['monitor_connected'] and not bridge.umiHistory['left']
        bridge.setValue('theme','light')
        switches=sum(path=='/platform/destinations' for path,_ in requests)
        for _ in range(3):
            bridge.navigate('teleop');wait()
            assert bridge.state['authenticated'] and bridge.state['uploadTarget']['selected']=='local'
            assert bridge.state['username']=='fixture'
            bridge.navigate('umi');wait();assert bridge.state['authenticated']
        assert sum(path=='/platform/destinations' for path,_ in requests)==switches
        bridge.navigate('teleop');wait();snap('local-teleop-navigation')
        print('PASS local UMI/teleop navigation keeps login and upload destination',flush=True)
        bridge.returnTeleop();wait();snap('light-setup')
        window.resize(560,700);wait();snap('compact');assert item('teleopNext').width()>0
        bridge.configureTeleop('小窗口测试','tianji');wait(lambda:bridge.state['paramSaved']);window.resize(360,380);wait()
        item('parameterDialog').setProperty('visible',True);wait();snap('compact-params')
        assert item('parameterDialog').property('height')<=window.height()-48
        item('parameterDialog').setProperty('visible',False)
        bridge.logout();wait(lambda:not bridge.state['authenticated'])
        bridge.shutdown();window.hide();server.shutdown()
        bad=[message for message in warnings if any(term in message for term in ('ReferenceError','TypeError','Unable to assign','Cannot open','failed to load','Binding loop','is not a type'))]
        assert not failures,failures
        assert not bad,'\n'.join(bad)
        print(f'PASS: Qt UI, login, Tianji/G2 action contracts, poses, parameter save, UMI camera/encoder/configure/record/stop, themes, resize; {len(requests)} POSTs; {len(scene.parts_value)} CAD visuals')
        if warnings: print('Qt diagnostics:', '\n'.join(warnings))
    faulthandler.cancel_dump_traceback_later()
    return 0
if __name__=='__main__':raise SystemExit(main())
