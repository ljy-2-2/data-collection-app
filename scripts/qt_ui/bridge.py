"""Qt UI state and asynchronous service adapter. All state mutates on the Qt thread."""
from __future__ import annotations
import copy
from ipaddress import ip_address
import json
import math
import os
from pathlib import Path
import time
import uuid
from urllib.parse import quote
from .grippers import endpoints, closure, parameter_defaults
from .umi_telemetry import UmiTelemetry
from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer, QUrl, QSettings
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

ROOT = Path(__file__).resolve().parents[2]
FIELDS = [
    dict(key='scale',label='比例',unit='×',min=.01,max=1,step=.01,default=1),
    dict(key='max_joint_speed_deg_s',label='关节速度上限',unit='°/s',min=.1,max=180,step=.1,default=180),
    dict(key='max_joint_accel_deg_s2',label='关节加速度上限',unit='°/s²',min=1,max=450,step=1,default=450),
    dict(key='left_gripper_speed',label='左夹爪速度',unit='rad/s',min=.01,max=100,step=.1),
    dict(key='right_gripper_speed',label='右夹爪速度',unit='rad/s',min=.01,max=100,step=.1),
    dict(key='kp',label='电机 Kp',unit='',min=0,max=500,step=1),
    dict(key='kd',label='电机 Kd',unit='',min=.01,max=5,step=.1),
]
_PARAMETER_DEFAULTS = parameter_defaults()
for field in FIELDS:
    field['default'] = _PARAMETER_DEFAULTS[field['key']]
G2_JOINTS=json.loads((Path(__file__).with_name('g2_joints.json')).read_text())
LIMITS=[[-170,170],[-120,120],[-170,170],[-144.99,60],[-170,170],[-60,60],[-90,90]]
CAMERAS=[dict(id=k,label=v) for k,v in [('head_color','头部彩色'),('head_stereo_left','头部左目'),('head_stereo_right','头部右目'),('head_back_fisheye','头部后鱼眼'),('head_left_fisheye','头部左鱼眼'),('head_right_fisheye','头部右鱼眼'),('hand_left_color','左手彩色'),('hand_right_color','右手彩色'),('head_depth','头部深度'),('hand_left_depth','左手深度'),('hand_right_depth','右手深度')]]
TIANJI_CAMERAS=[dict(id=k,label=v) for k,v in [('usb_1','主相机'),('usb_2','右腕部相机'),('usb_4','左腕部相机')]]


def devices(robot):
    return [dict(id=k,name=n,status='offline',detail='') for k,n in [('robot',robot),('xr','Galaxis VR'),('headset','Pico 设备')]]


def initial_state():
    return dict(ready=False, startupError='', authenticated=False, username='', error='', notice='', busy='', closing=False,
        page='teleop',teleopStep=1,umiStep=1,robot='tianji',task='遥操作数据采集',umiTask='UMI 数据采集',profile='dual',
        parameters=parameter_defaults(),paramSaved=False,paramSaving=False,
        uploadMode='dual',g2Scale=.5,g2Speed=.5,extraCameras=[],g2Camera='head_color',tianjiCamera='usb_1',
        tianjiCameraLayout='left-center-right',g2CameraLayout='left-center-right',
        tianji=dict(state='idle',devices=devices('Tianji'),joints={},cameras=[],collection={}),
        g2=dict(devices=devices('G2'),session=dict(state='idle'),motion=dict(state='idle')),
        preview=dict(available=False,joint_positions={},cameras=[]),
        umi=dict(state='idle',monitor={},log_tail=[],monitor_connected=False,recording=False),
        encoders={},encoderHistory=dict(left=[],right=[]),minima={},cameraRoles={},cameraLive={},
        checkedProfile='', umiPedalMessage='', preferences={},poses=[],selectedPose='zero',theme='system')


class Bridge(QObject):
    changed=Signal()
    umiTelemetryChanged=Signal()
    umiCameraBrightnessChanged=Signal()
    umiCameraResolutionChanged=Signal()
    result=Signal(str, bool)
    closeRequested=Signal()
    def __init__(self, values, *, demo=False, settings_path=None):
        super().__init__()
        self.values=values
        self.calibration=endpoints(ROOT)
        self.base=('http://127.0.0.1:1' if demo else values.get('LOCAL_CONTROL_URL','http://127.0.0.1:8011')).rstrip('/')
        self.network=QNetworkAccessManager(self)
        self.demo=demo
        self._state=initial_state()
        if self.umiOnly:self._state['page']='umi'
        self._camera_brightness={}
        self._camera_resolution={}
        self._camera_role_identities={}
        self.epoch=0
        self.revision=0
        self.pending=set()
        self.replies=set()
        self.last_poll={}
        self.last_encoder=0
        self.telemetry=UmiTelemetry(self.network, self)
        self.telemetry.changed.connect(self.umiTelemetryChanged.emit)
        config=Path(settings_path) if settings_path else Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'xiaokai-lab-data-collection'
        config.mkdir(parents=True,exist_ok=True)
        self.config=config
        self.settings=QSettings(str(config/'qt-ui.ini'),QSettings.IniFormat)
        self._state['theme']=self.settings.value('theme','system')
        for key in ('tianjiCameraLayout','g2CameraLayout'):
            layout=self.settings.value(key,'left-center-right')
            if layout in ('left-center-right','right-center-left'):self._state[key]=layout
        self.load_poses()
        self.poll_timer=QTimer(self);self.poll_timer.setInterval(100);self.poll_timer.timeout.connect(self.poll);self.poll_timer.start()
        self.save_timer=QTimer(self);self.save_timer.setSingleShot(True);self.save_timer.setInterval(350);self.save_timer.timeout.connect(self.save_parameters)

    @Property('QVariantMap',notify=changed)
    def state(self): return self._state
    @Property(bool,constant=True)
    def umiOnly(self): return self.values.get('APP_COLLECTION_MODE') == 'umi'
    @Property(bool,constant=True)
    def tianjiOnly(self): return self.values.get('APP_COLLECTION_MODE') == 'umi-tianji'
    @Property('QVariantMap',notify=umiTelemetryChanged)
    def umiTelemetry(self): return self.telemetry.value
    @Property('QVariantMap',notify=umiTelemetryChanged)
    def umiHistory(self): return self.telemetry.history
    @Property('QVariantMap',notify=umiCameraBrightnessChanged)
    def umiCameraBrightness(self): return self._camera_brightness
    @Property('QVariantMap',notify=umiCameraResolutionChanged)
    def umiCameraResolution(self): return self._camera_resolution
    @Property('QVariantList',constant=True)
    def fields(self): return FIELDS
    @Property('QVariantList',constant=True)
    def g2Joints(self): return G2_JOINTS
    @Property('QVariantList',constant=True)
    def limits(self): return LIMITS
    @Property('QVariantList',constant=True)
    def tianjiCameras(self): return TIANJI_CAMERAS
    @Property('QVariantList',constant=True)
    def g2Cameras(self): return CAMERAS
    @Property('QVariantMap',notify=changed)
    def gripperClosure(self):
        return {side:closure(self._state['tianji'],side,self.calibration) for side in ('left','right')}
    @Property(str,constant=True)
    def baseUrl(self): return self.base
    @Property(str,constant=True)
    def platformWebUrl(self):
        return self.values.get('PLATFORM_WEB_URL','http://127.0.0.1:5173')
    @Property(str,constant=True)
    def platformLocation(self):
        host=QUrl(self.platformWebUrl).host().lower()
        if host=='localhost':return '本地'
        try:return '本地' if ip_address(host).is_loopback else '云端'
        except ValueError:return '云端'
    @Property(str,constant=True)
    def assetRoot(self): return QUrl.fromLocalFile(str(ROOT/'public')+'/').toString()

    def update(self, **values):
        self._state.update(values);self.changed.emit()
        self.sync_umi_telemetry()

    def sync_umi_telemetry(self):
        s=self._state
        if (not self.demo and s['authenticated'] and not s['closing']
                and s['page']=='umi' and s['umiStep']==3 and s['busy']!='stop-umi'):
            self.telemetry.start(self.base+'/umi/telemetry')
        else:
            self.telemetry.stop()

    def fail(self, message):
        self.update(error=str(message))

    def request(self,path,data=None,done=None,*,method=None,timeout=10000,failed=None):
        epoch=self.epoch
        request=QNetworkRequest(QUrl(self.base+path));request.setHeader(QNetworkRequest.ContentTypeHeader,'application/json')
        request.setTransferTimeout(timeout)
        if method=='POST' or data is not None:
            reply=self.network.post(request,json.dumps(data).encode() if data is not None else b'')
        else:reply=self.network.get(request)
        self.replies.add(reply)
        def finished():
            self.replies.discard(reply)
            try:
                if epoch != self.epoch:return
                raw=bytes(reply.readAll())
                try: payload=json.loads(raw) if raw else {}
                except ValueError:payload={}
                if reply.error()!=QNetworkReply.NoError:
                    message=payload.get('detail') or reply.errorString()
                    if failed:failed(str(message))
                    else:self.fail(message)
                elif done:done(payload)
            finally:reply.deleteLater()
        reply.finished.connect(finished)

    def action(self,name,path,payload=None,done=None,*,timeout=120000,priority=False):
        s=self._state
        if s['closing'] or (s['busy'] and not priority):return
        self.revision+=1;revision=self.revision
        self.update(busy=name,error='')
        def finish(value):
            if revision != self.revision:return
            self.update(busy='')
            if done:done(value)
            self.last_poll.clear();self.poll()
            self.result.emit(name,True)
        def failed(error):
            if revision != self.revision:return
            self.update(busy='',error=error)
            self.last_poll.clear();self.poll()
            self.result.emit(name,False)
        if self.demo:
            QTimer.singleShot(30,lambda:finish(self.demo_action(path,payload)))
        else:self.request(path,payload,finish,method='POST',timeout=timeout,failed=failed)

    @Slot()
    def serviceReady(self):
        self.update(ready=True,startupError='',error='',notice='')
        if self.demo:return
        def preferences(value):
            self.update(preferences=value)
            if value.get('auto_login') and value.get('has_saved_password'):
                self.login(value.get('username',''),'',True,True,True)
        self.request('/platform/auth/preferences',done=preferences,timeout=125000)

    def serviceFailed(self, message):
        # Ignore pending login/actions from a service that has exited.
        self.epoch+=1;self.revision+=1
        self.pending.clear();self.last_poll.clear();self.save_timer.stop()
        self.reset_camera_brightness()
        self.update(ready=False,startupError=str(message),authenticated=False,
                    username='',busy='',error='',notice='')

    @Slot(str,str,bool,bool,bool)
    def login(self,username,password,remember,auto,saved):
        username=username.strip()
        if not self._state['ready'] or not username or not (password or saved):
            self.fail('请输入用户名和密码');return
        def logged_in(value):
            if not value.get('authenticated') or not value.get('user'):
                self.fail('登录状态无效');return
            self.update(authenticated=True,username=value['user']['username'],preferences={},notice=value.get('warning',''))
        self.action('login','/platform/auth/login',dict(username=username,password=password,remember_password=remember or auto,auto_login=auto,use_saved_password=saved),logged_in,timeout=125000)

    @Slot(str,'QVariant')
    def setValue(self,key,value):
        allowed={'robot','task','umiTask','profile','uploadMode','g2Scale','g2Speed','extraCameras','tianjiCamera','g2Camera','tianjiCameraLayout','g2CameraLayout','selectedPose','theme'}
        if key not in allowed or self._state['closing']:return
        if hasattr(value,'toVariant'):value=value.toVariant()
        if key in ('g2Scale','g2Speed'):
            try:value=float(value)
            except (ValueError,TypeError):return
            if not math.isfinite(value) or not .1<=value<=1:return
        if key=='robot':
            if value not in ('tianji','agibot-g2') or self._state['teleopStep']!=1 or self._state['busy'] or self.teleop_active():return
            if self.umiOnly or (self.tianjiOnly and value!='tianji'):return
        if key=='profile':
            if value not in ('dual','single-left','single-right') or self._state['umi'].get('state') not in ('idle','error'):return
            self.update(minima={},cameraRoles={},cameraLive={},checkedProfile='',encoders={},encoderHistory=dict(left=[],right=[]))
        if key=='theme':
            if value not in ('system','light','dark'):return
            self.settings.setValue('theme',value)
        if key in ('tianjiCameraLayout','g2CameraLayout'):
            if value not in ('left-center-right','right-center-left'):return
            self.settings.setValue(key,value)
        self.update(**{key:value})

    @Slot(QUrl)
    def importTianjiCameraCredentials(self, file):
        if not file.isLocalFile():
            self.fail('请选择本地相机配置文件');return
        self.action('camera-credentials','/tianji/camera-credentials',{'path':file.toLocalFile()},
                    done=lambda value:self.update(notice='相机配置已导入，请点击“检查设备”连接三路画面'),timeout=30000)

    @Slot(str)
    def navigate(self,page):
        if self.umiOnly and page != 'umi':return
        if page in ('teleop','umi','ego'):
            self.update(page=page,error='');self.last_poll.clear();self.poll()

    @Slot(str,str)
    def configureTeleop(self,task,robot):
        if self.umiOnly:
            self.fail('此安装包用于 UMI 采集；机器人遥操需要对应 ARM64 SDK');return
        if self.tianjiOnly and robot != 'tianji':
            self.fail('此开发板安装包支持 UMI 和天机采集');return
        if not self.valid_task(task) or robot not in ('tianji','agibot-g2'):return
        self.update(task=task.strip(),robot=robot,teleopStep=2,error='',selectedPose='zero' if robot=='tianji' else '')
        self.load_poses()
        if robot=='tianji':
            self.update(parameters=parameter_defaults(),paramSaved=False)
            self.save_timer.start()
        self.last_poll.clear();self.poll()

    def valid_task(self,value):
        if not value.strip() or len(value)>120 or any(ord(c)<32 for c in value):
            self.fail('任务名称必须为 1～120 个字符，不能包含控制字符');return False
        return True

    @Slot()
    def returnTeleop(self):
        if self.teleop_active():self.fail('请先停止遥操作、手动运动和采集');return
        if self._state['robot']=='tianji':
            self.action('close','/tianji/close',done=lambda _:self.update(teleopStep=1))
        else:self.update(teleopStep=1)

    def teleop_active(self):
        s=self._state;t=s['tianji'];g=s['g2'];c=t.get('collection',{})
        return bool(t.get('state') not in ('idle',None) or c.get('busy') or c.get('recording') or
            g.get('session',{}).get('state') not in ('idle',None) or g.get('motion',{}).get('state') not in ('idle',None))

    @Slot(str,float)
    def setParameter(self,key,value):
        field=next((f for f in FIELDS if f['key']==key),None)
        if not field or not math.isfinite(value) or not field['min']<=value<=field['max']:
            self.fail('参数超出允许范围');return
        if self.teleop_active() or self._state['busy']:return
        params=dict(self._state['parameters']);params[key]=value
        self.update(parameters=params,paramSaved=False,error='');self.save_timer.start()

    def save_parameters(self):
        if self._state['busy'] or self._state['paramSaving']:
            self.save_timer.start();return
        params=copy.deepcopy(self._state['parameters'])
        self.update(paramSaving=True)
        def done(_):
            unchanged=params==self._state['parameters']
            self.update(paramSaving=False,paramSaved=unchanged)
            if not unchanged:self.save_timer.start()
        def failed(message):self.update(paramSaving=False,paramSaved=False,error='参数自动保存失败：'+message)
        if self.demo:QTimer.singleShot(30,lambda:done(params))
        else:self.request('/tianji/parameters',params,done,failed=failed)

    @Slot(str)
    def teleop(self,action):
        s=self._state
        if not s['authenticated']:return
        if s['robot']=='tianji':
            if action=='start':
                if not self.valid_task(s['task']) or not s['paramSaved'] or s['paramSaving']:return
                self.action('start','/tianji/start',dict(task=s['task'],mode='dual',upload_mode=s['uploadMode'],parameters=s['parameters']))
            elif action in ('check','stop'):
                self.action(action,'/tianji/'+action,priority=action=='stop')
            elif action in ('record/start','record/stop'):
                self.action(action,'/tianji/'+action)
        elif action=='check':self.action('check','/devices/check')
        elif action=='start':
            if not self.valid_task(s['task']):return
            self.action('start','/sessions',dict(robot_model='agibot-g2',mode='arm',task=s['task'],scale=s['g2Scale'],max_joint_vel=s['g2Speed'],extra_cameras=s['extraCameras']))
        elif action=='stop':
            sid=s['g2'].get('session',{}).get('session_id')
            if sid:self.action('stop','/sessions/'+quote(sid,safe='')+'/stop',priority=True)

    def can_move(self):
        s=self._state
        if not s['authenticated'] or s['busy'] or self.teleop_active():return False
        if s['robot']=='tianji':return bool(s['tianji'].get('joints',{}).get('left')) and s['paramSaved']
        return bool(s['preview'].get('available')) and any(d.get('id')=='robot' and d.get('status')=='connected' for d in s['g2'].get('devices',[]))

    @Slot(str,int,int)
    def jog(self,side,joint,direction):
        if not self.can_move() or side not in ('left','right') or not 1<=joint<=7 or direction not in (-1,1):return
        value=self._state['tianji'].get('joints',{}).get(side,[])
        if len(value)!=7 or not LIMITS[joint-1][0]<=value[joint-1]+direction*10<=LIMITS[joint-1][1]:return
        self.action('move','/tianji/move',dict(kind='jog',mode='dual',side=side,joint=joint,delta=direction*10))

    @Slot(str,float)
    def gripper(self,side,closure):
        if self.can_move() and side in ('left','right') and closure in (0,1):
            self.action('move','/tianji/move',dict(kind='gripper',mode='dual',side=side,closure=closure))

    def g2_targets(self):
        return {j['name']:self._state['preview'].get('joint_positions',{}).get(j['name'],j['default']) for j in G2_JOINTS}

    @Slot(str,int)
    def jogG2(self,name,direction):
        if not self.can_move() or direction not in (-1,1):return
        spec=next((j for j in G2_JOINTS if j['name']==name),None)
        if not spec:return
        targets=self.g2_targets();targets[name]=max(spec['minimum'],min(spec['maximum'],targets[name]+direction*(.005 if 'gripper' in name else .01)))
        self.move_g2(targets,name)

    def move_g2(self,targets,changed=None):
        if not self.can_move():return
        payload=dict(command_id=str(uuid.uuid4()),robot_model='agibot-g2',mode='arm',joint_names=[j['name'] for j in G2_JOINTS],joint_positions=[targets[j['name']] for j in G2_JOINTS],preset='g2-recorded-pose')
        if changed:payload['changed_joint']=changed
        self.action('move','/move',payload)

    def pose_path(self):return self.config/('qt-poses-'+self._state['robot']+'.json')
    def load_poses(self):
        try:values=json.loads(self.pose_path().read_text())
        except (OSError,ValueError):values=[]
        valid=[]
        for pose in values if isinstance(values,list) else []:
            if not isinstance(pose,dict) or not isinstance(pose.get('id'),str) or not isinstance(pose.get('name'),str):continue
            targets=pose.get('targets',{})
            vectors=[targets.get(side,[]) for side in ('left','right')] if self._state['robot']=='tianji' else [list(targets.values())]
            if self._state['robot']=='tianji' and any(len(v)!=7 for v in vectors):continue
            if all(isinstance(x,(int,float)) and math.isfinite(x) for v in vectors for x in v):valid.append(pose)
        self.update(poses=valid)

    def persist_poses(self,poses):
        path=self.pose_path();temp=path.with_suffix('.tmp')
        try:
            temp.write_text(json.dumps(poses,ensure_ascii=False));temp.replace(path)
        except OSError as exc:self.fail('无法保存姿态：'+str(exc));return False
        self.update(poses=poses);return True

    @Slot(str)
    def savePose(self,name):
        name=name.strip()[:50]
        if not name:return
        s=self._state
        if s['robot']=='tianji':
            targets=s['tianji'].get('joints',{})
            if any(len(targets.get(side,[]))!=7 for side in ('left','right')):self.fail('尚无关节反馈');return
        else:targets=self.g2_targets()
        pose=dict(id=str(uuid.uuid4()),name=name,targets=copy.deepcopy(targets))
        if self.persist_poses([*s['poses'],pose]):self.update(selectedPose=pose['id'])

    @Slot()
    def deletePose(self):
        selected=self._state['selectedPose']
        if self.persist_poses([p for p in self._state['poses'] if p['id']!=selected]):
            self.update(selectedPose='zero' if self._state['robot']=='tianji' else '')

    @Slot()
    def restorePose(self):
        if not self.can_move():return
        s=self._state;pose=next((p for p in s['poses'] if p['id']==s['selectedPose']),None)
        if s['robot']=='tianji':
            if s['selectedPose']=='zero':self.action('move','/tianji/move',dict(kind='zero',mode='dual'))
            elif pose:self.action('move','/tianji/move',dict(kind='pose',mode='dual',targets=pose['targets']))
        elif pose:
            targets=self.g2_targets();targets.update(pose['targets']);self.move_g2(targets)

    @Slot()
    def umiNext(self):
        if self.valid_task(self._state['umiTask']):self.update(umiStep=2,error='')
    @Slot()
    def umiBack(self):
        if self._state['busy']:return
        if self._state['umi'].get('state') not in ('idle','error'):self.fail('请先结束 UMI 任务');return
        self.update(umiStep=1,error='')
    @Slot()
    def checkUmi(self):
        profile=self._state['profile']
        self.update(minima={},cameraLive={},checkedProfile='')
        def checked(value):
            candidates=value.get('monitor',{}).get('device_check',{}).get('camera_candidates',[])
            check=value.get('monitor',{}).get('device_check',{})
            roles={};used=set()
            for source in (self._state['cameraRoles'],check.get('camera_roles',{}),check.get('suggested_camera_roles',{})):
                for role in self.active_roles():
                    candidate=source.get(role)
                    if role not in roles and candidate in candidates and candidate not in used:roles[role]=candidate;used.add(candidate)
            for role in self.active_roles():
                if role not in roles:
                    candidate=next((c for c in candidates if c not in used),'')
                    roles[role]=candidate;used.add(candidate)
            self._camera_role_identities={r: next((d for d in check.get('camera_candidate_details',[]) if d['device']==device), {}) for r,device in roles.items()}
            self.update(umi=value,cameraRoles=roles,checkedProfile=profile)
        self.action('check-umi','/umi/check?collection_profile='+profile,done=checked)

    def reconcile_camera_devices(self,value):
        check=value.get('monitor',{}).get('device_check',{})
        details=check.get('camera_candidate_details')
        if details is None:return
        roles=dict(self._state['cameraRoles']);live=dict(self._state['cameraLive'])
        used=set()
        for role in self.active_roles():
            saved=self._camera_role_identities.get(role)
            candidate=None
            if self._state['umiStep']==3:
                candidate=next((d for d in details if d['device']==check.get('camera_roles',{}).get(role)),None)
            elif saved:
                matches=[d for d in details if all(d.get(k)==saved.get(k) for k in ('vendor','product','serial'))]
                if saved.get('serial') and len(matches)==1:candidate=matches[0]
                else:candidate=next((d for d in matches if d.get('usb_path')==saved.get('usb_path')),None)
            elif roles.get(role):
                candidate=next((d for d in details if d['device']==roles[role]),None)
            if candidate and candidate['device'] in used:candidate=None
            device=candidate['device'] if candidate else ''
            changed=roles.get(role,'')!=device or (saved and candidate and saved.get('generation')!=candidate.get('generation'))
            if changed:
                live[role]=False
                for key in (roles.get(role,''),device,role):
                    key=key.removeprefix('/dev/')
                    self._camera_resolution.pop(key,None);self._camera_brightness.pop(key,None)
                self.umiCameraResolutionChanged.emit();self.umiCameraBrightnessChanged.emit()
            roles[role]=device
            if candidate:
                used.add(device);self._camera_role_identities[role]=candidate
        if roles!=self._state['cameraRoles'] or live!=self._state['cameraLive']:
            self.update(cameraRoles=roles,cameraLive=live)

    @Slot(str,str)
    def selectLiveCamera(self,role,device):
        if self._state['busy'] or self._state['umi'].get('recording'):return
        self.action('select-camera','/umi/camera-selection',dict(role=role,device=device),
                    lambda value:(self.reconcile_camera_devices(value),self.update(umi=value)))

    def active_sides(self):return ['left','right'] if self._state['profile']=='dual' else [self._state['profile'].split('-')[1]]
    def active_roles(self):return [side for side in ('left','center','right') if side=='center' or side in self.active_sides()]

    @Slot(str,str)
    def cameraRole(self,role,device):
        if role not in self.active_roles():return
        roles=dict(self._state['cameraRoles']);live=dict(self._state['cameraLive'])
        for other,current in roles.items():
            if other!=role and current==device:roles[other]=roles.get(role,'');live[other]=False
        roles[role]=device;live[role]=False
        details=self._state['umi'].get('monitor',{}).get('device_check',{}).get('camera_candidate_details',[])
        self._camera_role_identities={r: next((d for d in details if d['device']==dev), {}) for r,dev in roles.items()}
        self.update(cameraRoles=roles,cameraLive=live)

    @Slot(str,bool)
    def cameraReady(self,role,ready):
        if self._state['cameraLive'].get(role)==ready:return
        self.update(cameraLive={**self._state['cameraLive'],role:ready})

    def reset_camera_brightness(self):
        self._camera_brightness={};self.umiCameraBrightnessChanged.emit()
        self._camera_resolution={};self.umiCameraResolutionChanged.emit()

    def camera_resolution_request(self,key,mode_id=None):
        if not self.camera_brightness_allowed(key) or self._camera_resolution.get(key,{}).get('busy'):return
        if mode_id is not None and self._state['umiStep']!=2:return
        def update(**values):
            self._camera_resolution={**self._camera_resolution,key:{**self._camera_resolution.get(key,{}),**values}}
            self.umiCameraResolutionChanged.emit()
        update(busy=True,error='')
        def done(value):
            if not isinstance(value,dict) or not isinstance(value.get('options'),list):
                update(busy=False,error='无法读取相机分辨率');return
            update(**value,busy=False,error='')
        if self.demo:
            options=[dict(id=f'{w}x{h}-MJPG-30',width=w,height=h,fps=30,label=f'{w} × {h} · 30 帧/秒') for w,h in ((640,480),(1280,720),(1920,1080))]
            selected=next((m for m in options if m['id']==mode_id),self._camera_resolution.get(key,{}).get('selected') or options[0])
            done(dict(options=options,selected=selected,editable=self._state['umiStep']==2));return
        self.request('/umi/camera-controls/'+quote(key,safe='')+'/resolution',
                     data=None if mode_id is None else dict(mode_id=mode_id),done=done,
                     method='GET' if mode_id is None else 'POST',timeout=20000,
                     failed=lambda message:update(busy=False,error=str(message)))

    @Slot(str)
    def loadCameraResolution(self,key):self.camera_resolution_request(key)

    @Slot(str,str)
    def setCameraResolution(self,key,mode_id):self.camera_resolution_request(key,mode_id)

    def camera_brightness_allowed(self,key):
        s=self._state
        if (not s['authenticated'] or s['closing'] or s['page']!='umi'
                or s['busy'] in ('check-umi','configure-umi','start-umi','stop-umi','closing')):return False
        if s['umiStep']==2:
            return bool(key) and key in [s['cameraRoles'].get(role,'').removeprefix('/dev/') for role in self.active_roles()]
        return (s['umiStep']==3 and key in self.active_roles() and bool(s['umi'].get('monitor_connected'))
                and s['umi'].get('state') in ('ready','recording'))

    def camera_brightness_update(self,key,**values):
        # This independent signal never changes global busy or the video source.
        self._camera_brightness={**self._camera_brightness,key:{**self._camera_brightness.get(key,{}),**values}}
        self.umiCameraBrightnessChanged.emit()

    def camera_brightness_request(self,key,value=None):
        previous=self._camera_brightness.get(key,{})
        if not self.camera_brightness_allowed(key) or previous.get('busy'):return
        self.camera_brightness_update(key,busy=True,error='')
        def failed(message):
            self.camera_brightness_update(key,busy=False,loaded=True,error=str(message))
        def done(result):
            if not isinstance(result,dict) or not isinstance(result.get('supported'),bool):failed('无法读取亮度参数');return
            if result.get('supported'):
                if (not all(isinstance(result.get(k),int) and not isinstance(result[k],bool) for k in ('value','min','max','step','default'))
                        or result['min']>result['max'] or result['step']<=0
                        or not result['min']<=result['value']<=result['max']
                        or not result['min']<=result['default']<=result['max']):
                    failed('相机返回了无效的亮度范围');return
            self.camera_brightness_update(key,**{k:v for k,v in result.items() if k in ('supported','value','min','max','step','default')},
                                          message=str(result.get('message') or ''),busy=False,loaded=True,error='')
        if self.demo:
            done(dict(supported=True,value=previous.get('value',0) if value is None else value,min=-64,max=64,step=1,default=0));return
        self.request('/umi/camera-controls/'+quote(key,safe='')+'/brightness',
                     data=None if value is None else dict(value=value),done=done,
                     method='GET' if value is None else 'POST',timeout=10000,failed=failed)

    @Slot(str)
    def loadCameraBrightness(self,key):self.camera_brightness_request(key)

    @Slot(str,int)
    def setCameraBrightness(self,key,value):
        current=self._camera_brightness.get(key,{})
        if not current.get('supported') or current.get('busy') or current.get('error'):return
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value!=int(value):return
        value=int(value)
        if (not current['min']<=value<=current['max'] or (value-current['min'])%current['step']):
            self.camera_brightness_update(key,error='亮度需在相机支持的范围内，并符合步长');return
        self.camera_brightness_request(key,value)

    @Slot(str,bool)
    def encoderMinimum(self,side,enabled):
        minima=dict(self._state['minima']);reading=self._state['encoders'].get(side,{})
        if not enabled:minima.pop(side,None)
        elif side in self.active_sides() and reading.get('online') and isinstance(reading.get('value'),(int,float)) and math.isfinite(reading['value']):minima[side]=reading['value']
        else:self.fail('编码器尚无有效实时数据');return
        self.update(minima=minima)

    @Slot()
    def startUmi(self):
        s=self._state
        if not self.valid_task(s['umiTask']) or s['busy']:return
        if any(v.get('busy') for v in self._camera_resolution.values()):
            self.fail('请等待相机分辨率应用完成');return
        check=s['umi'].get('monitor',{}).get('device_check',{})
        ready=(s['checkedProfile']==s['profile'] and check.get('hardware_ready') and not check.get('stack_occupied') and check.get('pico_ready') and
            all(s['cameraLive'].get(r) for r in self.active_roles()) and all(side in s['minima'] and s['encoders'].get(side,{}).get('online') for side in self.active_sides()))
        if not ready:self.fail('请确认相机出图、PICO 在线，并记录所有启用夹爪的闭合最小值');return
        payload=dict(collection_profile=s['profile'],camera_roles={r:s['cameraRoles'].get(r,'') for r in self.active_roles()},capture_encoder_zero=False,encoder_minima=s['minima'])
        task,profile=s['umiTask'],s['profile']
        def configured(_):
            self.action('start-umi','/umi/sessions',dict(task=task,collection_profile=profile),lambda value:self.update(umiStep=3,umiPedalMessage='',umi={**self._state['umi'],**value,'monitor':{}}))
        self.action('configure-umi','/umi/configure',payload,configured)

    @Slot(str)
    def recordUmi(self,action):
        s=self._state;u=s['umi']
        if action not in ('start','stop','cancel'):return
        if action=='start' and (u.get('state')!='ready' or not u.get('monitor_connected')):self.fail('采集服务尚未就绪');return
        self.action('record-'+action,'/umi/record/'+action,done=lambda value:self.update(umi=value),timeout=60000 if action=='stop' else 120000)

    @Slot()
    def stopUmi(self):
        sid=self._state['umi'].get('session_id')
        if not sid:self.update(umiStep=1);return
        self.action('stop-umi','/umi/sessions/'+quote(sid,safe='')+'/stop',done=lambda value:self.update(umi=value,umiStep=1,checkedProfile='',minima={},cameraLive={}),timeout=250000,priority=True)

    @Slot()
    def retryUploads(self):self.action('upload','/umi/import/scan',done=lambda v:self.update(umi=v))

    @Slot()
    def logout(self):
        if self.teleop_active() or self._state['umi'].get('state') not in ('idle','error'):
            self.fail('请先结束采集和控制，再退出登录');return
        def done(_):
            self.epoch+=1;self.pending.clear();self.last_poll.clear()
            self.reset_camera_brightness()
            appearance={key:self._state[key] for key in ('theme','tianjiCameraLayout','g2CameraLayout')}
            self._state=initial_state()
            if self.umiOnly:self._state['page']='umi'
            self.update(ready=True,**appearance)
        self.save_timer.stop()
        def clear_login(_):self.action('logout','/platform/auth/logout',done=done,timeout=30000)
        self.action('close','/tianji/close',done=clear_login)

    @Slot()
    def openPlatform(self):
        from PySide6.QtGui import QDesktopServices
        url=QUrl(self.platformWebUrl)
        if url.scheme() in ('http','https'):QDesktopServices.openUrl(url)

    @Slot()
    def clearError(self):self.update(error='')

    def poll(self):
        s=self._state
        if self.demo or not s['authenticated'] or s['closing']:return
        now=time.monotonic()
        requests=[('umi','/umi/status',1.)]
        if s['teleopStep']==2 and s['robot']=='tianji':requests.append(('tianji','/tianji/status',.1))
        else:requests.append(('g2','/status',.5))
        if s['teleopStep']==2 and s['robot']=='agibot-g2':requests.append(('preview','/preview',.15))
        if s['page']=='umi' and s['umiStep']==2 and s['checkedProfile']==s['profile'] and not s['busy']:
            requests.append(('encoders','/umi/encoders/preview?collection_profile='+s['profile'],.15))
        for key,path,interval in requests:
            if key in self.pending or now-self.last_poll.get(key,0)<interval:continue
            self.pending.add(key);self.last_poll[key]=now;revision=self.revision
            def done(value,key=key,revision=revision):
                self.pending.discard(key)
                if revision!=self.revision:return
                if key=='encoders':
                    value=value.get('encoders',{});self.history(value)
                if key=='umi' and self._state['umiStep'] in (2,3) and not self._state['busy']:
                    self.reconcile_camera_devices(value)
                self.update(**{key:value})
            def failed(message,key=key):
                self.pending.discard(key)
                if key=='tianji':self.update(tianji=dict(state='unknown',error='连接中断，等待确认控制已停止',devices=devices('Tianji'),joints={},cameras=[]))
                elif key=='encoders':self.update(encoders={})
                elif key=='preview':self.update(preview=dict(available=False,joint_positions={},cameras=[]))
                elif key=='g2':self.update(g2=dict(devices=devices('G2'),session=dict(state='unknown'),motion=dict(state='unknown')))
                elif key=='umi':self.update(umi={**s['umi'],'monitor_connected':False,'message':message})
            self.request(path,done=done,timeout=4000,failed=failed)

    def history(self,values):
        histories={}
        for side in ('left','right'):
            reading=values.get(side,{});value=reading.get('value')
            histories[side]=[*self._state['encoderHistory'][side],value if reading.get('online') and isinstance(value,(int,float)) and math.isfinite(value) else None][-120:]
        self.update(encoderHistory=histories)

    def shutdown(self):
        self.poll_timer.stop();self.save_timer.stop();self.epoch+=1
        self.update(closing=True,busy='closing',notice='正在保存采集数据并释放设备，请稍候…')
        for reply in list(self.replies):reply.abort()

    def demo_action(self,path,payload):
        s=self._state
        if path=='/platform/auth/login':return dict(authenticated=True,user=dict(username=payload['username']))
        if path=='/tianji/check':
            self.update(tianji=dict(state='idle',devices=[{**d,'status':'connected'} for d in devices('Tianji')],joints=dict(left=[0]*7,right=[0]*7),cameras=[]));return {}
        if path=='/tianji/start':self.update(tianji={**s['tianji'],'state':'running'})
        if path=='/tianji/stop':self.update(tianji={**s['tianji'],'state':'idle'})
        if path.startswith('/umi/check'):
            return dict(state='idle',monitor=dict(device_check=dict(camera_candidates=['/dev/video0','/dev/video2','/dev/video4'],encoder_candidates=['/dev/ttyACM0','/dev/ttyACM1'],hardware_ready=True,pico_ready=True,vr_service_ready=True)),monitor_connected=False)
        if path=='/umi/sessions':return dict(session_id='demo',state='ready',monitor_connected=True)
        if path.startswith('/umi/record/'):
            action=path.rsplit('/',1)[1];return {**s['umi'],'state':'recording' if action=='start' else 'ready','recording':action=='start'}
        if path.endswith('/stop') and path.startswith('/umi/sessions/'):return dict(state='idle',monitor={})
        return {}
