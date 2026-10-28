"""Independent camera preview over ROS networking or SSH. No motion dependencies."""
import asyncio
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import struct
import subprocess
import sys
import threading
import time
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

ROLES={'head':'头部主相机','left':'左相机','right':'右相机'}
# The bundled head-camera reader uses this color topic; USB devices are side cameras.
HEAD_SOURCE='ros:/camera/color/image_raw'
HEAD_TOPICS=('/camera/color/image_raw','/head_camera/color/image_raw','/cam_h/color/image_raw')
HEAD_USB='/dev/v4l/by-id/usb-Orbbec_R__Orbbec_Gemini_335L_CP2H163000S6-video-index0'
SIDE_ROLES={role:'usb:/dev/v4l/by-path/pci-0000:00:14.0-usb-0:'+port+':1.0-video-index0'
            for role,port in (('left','2'),('right','4'))}
HEADER=struct.Struct('!II')


class CaptureProfile(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    width:int=Field(default=640,ge=1,le=4096)
    height:int=Field(default=480,ge=1,le=2160)
    brightness:int|None=None


class CameraConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    transport:Literal['network','auto','ros','ssh']='network'
    host:str='192.168.26.22'
    user:str='jt'
    ros_master:str='http://192.168.26.1:11311'
    ros_setup:str='/opt/ros/noetic/setup.bash'
    roles:dict[str,str]=Field(default_factory=dict)
    profiles:dict[str,CaptureProfile]=Field(default_factory=dict)

    def checked(self):
        value=self.model_dump()
        if self.transport=='ros' and self.ros_master.rstrip('/')=='http://kuavo_master:11311':
            value['ros_master']='http://192.168.26.1:11311'
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,252}',self.host):raise ValueError('工控机地址无效')
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]{0,31}',self.user):raise ValueError('SSH 用户名无效')
        if not re.fullmatch(r'http://[a-zA-Z0-9._-]+:[0-9]{1,5}/?',self.ros_master):raise ValueError('ROS 主控地址无效')
        if not self.ros_setup.startswith('/') or any(c in self.ros_setup for c in ('\n','\r','\x00')):raise ValueError('ROS 环境文件须为绝对路径')
        if (set(self.roles)|set(self.profiles))-set(ROLES):raise ValueError('相机角色无效')
        value['roles']={k:v for k,v in self.roles.items() if v}
        if self.transport in ('auto','network') and not self.roles:value['roles'].update(SIDE_ROLES)
        if value['roles'].get('head',HEAD_SOURCE)!=HEAD_SOURCE:
            raise ValueError('主相机固定使用机器人头部相机')
        value['roles']['head']=HEAD_SOURCE
        if len(set(value['roles'].values()))!=len(value['roles']):raise ValueError('同一相机不能同时分配给两个位置')
        return value


def head_aliases(sources):
    """Keep the physical head role while supporting official launch namespaces."""
    sources=copy.deepcopy(sources)
    known={topic+suffix for topic in HEAD_TOPICS for suffix in ('','/compressed')}
    for source in sources:
        if source.get('topic') in known:source['role_hint']='head'
    candidate=next((s for topic in HEAD_TOPICS for suffix in ('/compressed','') for s in sources
                    if s.get('topic')==topic+suffix),None)
    if candidate:
        sources=[s for s in sources if s['id']!=HEAD_SOURCE]
        sources.append({**candidate,'id':HEAD_SOURCE,'label':'头部相机 · '+candidate['topic']})
    return sources


def discover_ssh(config,socket):
    result=subprocess.run(ssh_command(config,socket,dict(action='discover')),stdin=subprocess.DEVNULL,
                          capture_output=True,text=True,timeout=20,env=clean_env())
    if result.returncode:raise RuntimeError(result.stderr.strip()[-2000:] or '工控机相机检测失败')
    value=json.loads(result.stdout)
    if not isinstance(value.get('sources'),list):raise ValueError('工控机未返回相机清单')
    return value


def discover_auto(config,socket):
    """Independent USB sides + physical head. Never start drivers or robot control."""
    from .kuavo_camera_ros import discover
    sources=[];warnings=[];errors={}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs={'usb':pool.submit(discover_ssh,config,socket),
              'ros':pool.submit(discover,config['ros_master'])}
        for kind,job in jobs.items():
            try:
                value=job.result()
                if kind=='usb':
                    sources.extend({**s,'transport':'ssh','peer':config} for s in value['sources'] if s.get('kind')=='usb')
                else:
                    sources.extend({**s,'transport':'ros'} for s in head_aliases(value['sources']) if s.get('role_hint')=='head')
            except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
                message=('左右 USB 相机：' if kind=='usb' else '头部 ROS 图像：')+str(exc)
                warnings.append(message)
                if kind=='usb':errors.update(left=message,right=message)
    if not any(s['id']==HEAD_SOURCE for s in sources):
        peer={**config,'host':'192.168.26.12','user':'leju_kuavo',
              'ros_setup':'/home/leju_kuavo/kuavo_ros_application/devel/setup.bash'}
        try:
            value=discover_ssh(peer,socket)
            head=next((s for s in value['sources'] if HEAD_USB in s.get('aliases',[s.get('path')])
                       and 'MJPG' in s.get('formats',[])),None)
            if head is None:raise RuntimeError('未发现机器人本体 Orbbec 彩色相机')
            sources.append({**head,'id':HEAD_SOURCE,'label':'机器人头部 Orbbec 彩色相机',
                            'role_hint':'head','transport':'ssh','peer':peer})
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
            errors['head']='头部相机：'+str(exc);warnings.append(errors['head'])
    return dict(sources=sources,warnings=warnings,role_errors=errors)


def discover_network(root,config):
    from .kuavo_network_client import discover
    peers=[(config['host'],('left','right')),('192.168.26.12',('head',))]
    sources=[];errors={}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=[(host,roles,pool.submit(discover,root,host)) for host,roles in peers]
        for host,roles,job in jobs:
            try:
                value=job.result()
                for source in value['sources']:
                    if source.get('kind')!='usb':continue
                    if roles==('head',):
                        if HEAD_USB not in source.get('aliases',[]):continue
                        sources.append({**source,'id':HEAD_SOURCE,'agent_source_id':source['id'],'role_hint':'head',
                                        'transport':'network','agent_host':host,'label':'机器人头部 Orbbec 彩色相机'})
                    else:sources.append({**source,'agent_source_id':source['id'],'transport':'network','agent_host':host})
                if roles==('head',) and not any(s['id']==HEAD_SOURCE for s in sources):raise RuntimeError('未发现本体彩色相机')
            except Exception as exc:
                for role in roles:errors[role]=host+' 网络采集服务未就绪：'+str(exc)
    return dict(sources=sources,warnings=list(dict.fromkeys(errors.values())),role_errors=errors)


def ssh_command(config, socket, options):
    # A multiplex socket is bound to one SSH peer; never reuse it for another host.
    if (config['host'],config['user']) != ('192.168.26.22','jt'):
        peer=(config['host'],config['user'])
        name={('192.168.26.1','lab'):'upper',('192.168.26.12','leju_kuavo'):'head'}.get(peer)
        socket=socket.with_name(name or hashlib.sha256(repr(peer).encode()).hexdigest()[:8])
    source=Path(__file__).with_name('kuavo_camera_remote.py').read_text()
    encoded=base64.b64encode(source.encode()).decode()
    program='import base64,json;exec(base64.b64decode('+repr(encoded)+'));run(json.loads('+repr(json.dumps(options))+'))'
    shell=('if [ -f '+shlex.quote(config['ros_setup'])+' ]; then source '+shlex.quote(config['ros_setup'])+'; fi; '
           +'export ROS_MASTER_URI='+shlex.quote(config['ros_master'])+' ROS_IP='+shlex.quote(config['host'])+'; unset ROS_HOSTNAME; '
           +'exec python3 -u -c '+shlex.quote(program))
    return ['ssh','-T','-F','/dev/null','-S',str(socket),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','ConnectTimeout=5','-o','ServerAliveInterval=2','-o','ServerAliveCountMax=2',
            config['user']+'@'+config['host'],'bash --noprofile --norc -c '+shlex.quote(shell)]


def clean_env():
    return {k:v for k,v in os.environ.items() if k not in ('LD_LIBRARY_PATH','LD_PRELOAD','PYTHONPATH','PYTHONHOME')}


class CameraFeed:
    def __init__(self, command):
        self.lock=threading.Lock();self.stop_event=threading.Event();self.process=None
        self.frame=None;self.sequence=0;self.received=0.;self.width=0;self.height=0
        self.settings={};self.source_stamp=None;self.error='';self.started=time.monotonic();self.diagnostics=deque(maxlen=5)
        self.thread=threading.Thread(target=self._run,args=(command,),daemon=True)
        self.thread.start()

    def _run(self,command):
        selector=selectors.DefaultSelector()
        try:
            self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=clean_env())
            selector.register(self.process.stdout,selectors.EVENT_READ,'frame')
            selector.register(self.process.stderr,selectors.EVENT_READ,'error')
            buffer=bytearray();last_frame=time.monotonic();source_sequence=-1
            while not self.stop_event.is_set():
                if time.monotonic()-last_frame>12:raise RuntimeError('相机未返回新画面')
                for key,_ in selector.select(.2):
                    data=os.read(key.fd,65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        if not selector.get_map():raise RuntimeError('相机连接已结束')
                        continue
                    if key.data=='error':
                        self.diagnostics.append(data.decode(errors='replace')[-1000:]);continue
                    buffer.extend(data)
                    while len(buffer)>=HEADER.size:
                        meta_size,jpeg_size=HEADER.unpack_from(buffer)
                        if not 0<meta_size<=1024 or not 0<jpeg_size<=8*1024*1024:raise ValueError('无效相机帧长度')
                        size=HEADER.size+meta_size+jpeg_size
                        if len(buffer)<size:break
                        meta=json.loads(bytes(buffer[HEADER.size:HEADER.size+meta_size]))
                        jpeg=bytes(buffer[HEADER.size+meta_size:size]);del buffer[:size]
                        if not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):raise ValueError('无效相机图像')
                        if any(type(meta.get(k)) is not int or not 0<meta[k]<=8192 for k in ('width','height')):raise ValueError('无效相机尺寸')
                        if type(meta.get('sequence')) is not int or meta['sequence']<=source_sequence:raise ValueError('相机帧序号未递增')
                        source_sequence=meta['sequence'];last_frame=time.monotonic()
                        with self.lock:
                            self.frame=jpeg;self.sequence+=1;self.received=last_frame
                            self.width=meta['width'];self.height=meta['height']
                            self.settings=meta.get('settings') or {};self.source_stamp=meta.get('source_monotonic_ns')
        except Exception as exc:
            if not self.stop_event.is_set():self.error=str(exc)+'；'+''.join(self.diagnostics).strip()
        finally:
            selector.close()
            process=self.process
            if process:
                # stdin EOF revokes only this camera worker's lease on the remote host.
                try:process.stdin.close()
                except OSError:pass
                try:process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:process.wait(timeout=2)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
                for pipe in (process.stdout,process.stderr):pipe.close()

    def snapshot(self):
        with self.lock:
            age=time.monotonic()-self.received if self.received else None
            return dict(connected=bool(age is not None and age<2.5 and not self.error and not self.stop_event.is_set()),
                        state='error' if self.error else 'stopped' if self.stop_event.is_set() else 'streaming' if age is not None and age<2.5 else 'waiting',
                        error=self.error,width=self.width,height=self.height,age_s=age,settings=dict(self.settings))

    def stop(self):
        self.stop_event.set();self.thread.join(timeout=9)

    async def stream(self):
        sequence=0
        while not self.stop_event.is_set() and not self.error:
            with self.lock:
                jpeg=self.frame if self.sequence!=sequence and time.monotonic()-self.received<2.5 else None
                if jpeg is not None:sequence=self.sequence
            if jpeg is not None:
                yield b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(jpeg)).encode()+b'\r\n\r\n'+jpeg+b'\r\n'
            await asyncio.sleep(.025)


class KuavoCameras:
    def __init__(self,root):
        self.root=Path(root)
        self.path=self.root/'teleop_control/runtime/kuavo/cameras.json'
        self.socket=self.path.parent/'camera-ssh/control'
        self.lock=threading.RLock();self.operation=threading.RLock()
        self.config=CameraConfig().checked();self.sources=[];self.warnings=[];self.error='';self.feeds={}
        self.recording_guard=lambda:False
        self.role_errors={};self.reconnecting=False;self.generation=0;self.stream_id='';self.reconnect_thread=None
        try:
            if self.path.exists():
                saved=json.loads(self.path.read_text())
                if saved.get('transport') in ('auto','ssh'):saved['transport']='network'
                self.config=CameraConfig(**saved).checked()
        except (OSError,ValueError) as exc:self.error='相机配置读取失败：'+str(exc)

    def snapshot(self):
        with self.lock:
            return dict(config=copy.deepcopy(self.config),sources=copy.deepcopy(self.sources),warnings=list(self.warnings),error=self.error,
                        reconnecting=self.reconnecting,stream_id=self.stream_id,
                        profile=dict(width=640,height=480,target_fps=30),
                        cameras=[dict(id=role,label=label,source=self.config['roles'].get(role,''),
                            **(self.feeds[role].snapshot() if role in self.feeds else dict(connected=False,
                              state='waiting' if self.reconnecting else 'error' if self.role_errors.get(role) else 'disconnected',
                              error=self.role_errors.get(role,''))))
                            for role,label in ROLES.items()])

    def _save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.tmp')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:json.dump(self.config,f,ensure_ascii=False,indent=2)
        os.replace(temp,self.path)

    def discover(self,body):
        config=body.checked()
        with self.operation:
            try:
                if config['transport']=='network':
                    value=discover_network(self.root,config)
                elif config['transport']=='auto':
                    value=discover_auto(config,self.socket)
                elif config['transport']=='ros':
                    from .kuavo_camera_ros import discover
                    value=discover(config['ros_master'])
                else:
                    value=discover_ssh(config,self.socket)
                if not isinstance(value.get('sources'),list):raise ValueError('工控机未返回相机清单')
                with self.lock:
                    changed=any(self.config[k]!=config[k] for k in ('transport','host','user','ros_master','ros_setup'))
                if changed:self._stop()
                with self.lock:
                    if changed:self.config=config
                    self.sources=head_aliases(value['sources']);self.warnings=list(value.get('warnings',[]));self.error=''
                    self.role_errors=dict(value.get('role_errors',{}))
                    if config['transport']=='ros':
                        self.warnings.append('网络直连只读取已发布的画面；未启动驱动的 USB 相机需要在机器人端启用。')
                    if HEAD_SOURCE not in {s['id'] for s in self.sources}:
                        self.warnings.append('头部相机尚未发布画面，主画面保持未连接；请检查头部相机视频连接。')
            except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
                with self.lock:self.error=str(exc)
                raise
            return self.snapshot()

    def connect(self,body,*,partial=False,persist=True):
        config=body.checked()
        with self.operation:
            with self.lock:
                if any(config[k]!=self.config[k] for k in ('transport','host','user','ros_master','ros_setup')):raise ValueError('连接方式或地址改变后请先检测相机')
                sources={s['id']:s for s in self.sources}
                if any(role!='head' and sources.get(key,{}).get('role_hint')=='head' for role,key in config['roles'].items()):
                    raise ValueError('头部相机只能用于主画面')
                missing=set(config['roles'].values())-set(sources)-{HEAD_SOURCE}
                if missing and not partial:raise ValueError('相机身份不在本次检测清单中，请重新检测')
                if config['transport'] in ('auto','network') and any(role!='head' and sources.get(key,{}).get('kind') not in (None,'usb')
                                                      for role,key in config['roles'].items()):
                    raise ValueError('左右画面须选择 USB 相机')
                selected={role:key for role,key in config['roles'].items() if key in sources}
                if not selected and not partial:raise ValueError('尚未发现头部相机画面，请检测相机并配置左右相机')
            self._stop()
            with self.lock:
                self.config=config
                if persist:self._save()
                self.error=''
                self.stream_id=uuid.uuid4().hex
                for role,key in config['roles'].items():
                    if key not in sources:self.role_errors.setdefault(role,'未发现已配置相机，请检查连接后重试')
                for role,key in selected.items():
                    source={**sources[key],'profile':config.get('profiles',{}).get(role,{})};self.role_errors.pop(role,None)
                    if source.get('transport',config['transport'])=='network':
                        from .kuavo_network_client import profile
                        self.feeds[role]=CameraFeed([sys.executable,'-u',str(Path(__file__).with_name('kuavo_network_client.py')),
                            json.dumps(profile(self.root,source['agent_host'])),'/camera',
                            json.dumps(dict(source_id=source['agent_source_id'],profile=source['profile']))])
                    elif source.get('transport',config['transport'])=='ros':
                        from .kuavo_camera_ros import DirectRosFeed
                        self.feeds[role]=DirectRosFeed(source)
                    else:
                        self.feeds[role]=CameraFeed(ssh_command(source.get('peer',config),self.socket,
                                                              dict(action='stream',source=source)))
            return self.snapshot()

    def settings(self, profiles):
        if self.recording_guard():raise RuntimeError('请先按 B 保存本轮采集，再修改相机参数')
        with self.operation:
            old=copy.deepcopy(self.config)
            candidate={**old,'profiles':{**old.get('profiles',{}),**profiles}}
            checked=CameraConfig(**candidate).checked()
            if not profiles:raise ValueError('没有可应用的相机参数')
            for role,profile in profiles.items():
                source=next((s for s in self.sources if s['id']==old['roles'].get(role)),{})
                caps=source.get('capabilities') or {}
                if source.get('kind')!='usb':raise ValueError(ROLES.get(role,role)+'：当前图像源未提供可写参数')
                if {'width':profile['width'],'height':profile['height']} not in caps.get('resolutions',[]):raise ValueError('相机不支持此分辨率')
                b=profile.get('brightness');spec=caps.get('brightness')
                if b is not None and (not spec or not spec['min']<=b<=spec['max'] or (b-spec['min'])%max(1,spec['step'])):raise ValueError('亮度超出相机支持范围')
            try:
                self.connect(CameraConfig(**checked),partial=True,persist=False)
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    states={role:self.feeds[role].snapshot() for role in profiles if role in self.feeds}
                    if len(states)!=len(profiles):raise RuntimeError('相机未连接')
                    if any(v.get('error') for v in states.values()):raise RuntimeError('; '.join(v['error'] for v in states.values() if v.get('error')))
                    if all(v.get('connected') for v in states.values()):
                        for role,v in states.items():
                            if any(v.get('settings',{}).get(k)!=val for k,val in profiles[role].items() if val is not None):raise RuntimeError('相机参数回读不匹配')
                        self._save()
                        return self.snapshot()
                    time.sleep(.05)
                raise RuntimeError('相机参数应用后未收到画面')
            except Exception:
                self.connect(CameraConfig(**old),partial=True)
                raise

    def request_reconnect(self):
        """Check-device hook: video work runs independently of motion readiness."""
        with self.lock:
            if self.reconnecting:return self.snapshot()
            self.generation+=1;generation=self.generation;self.reconnecting=True;self.error=''
        def run():
            try:
                with self.operation:
                    with self.lock:
                        if generation!=self.generation:return
                        config=CameraConfig(**copy.deepcopy(self.config))
                    self.discover(config)
                    with self.lock:
                        if generation!=self.generation:return
                    self.connect(config,partial=True)
            except Exception as exc:
                with self.lock:
                    if generation==self.generation:self.error=str(exc)
            finally:
                with self.lock:
                    if generation==self.generation:self.reconnecting=False
        self.reconnect_thread=threading.Thread(target=run,daemon=True)
        self.reconnect_thread.start()
        return self.snapshot()

    def _stop(self):
        with self.lock:feeds,self.feeds=self.feeds,{}
        for feed in feeds.values():feed.stop()

    def close(self):
        with self.lock:
            self.generation+=1;self.reconnecting=False
        with self.operation:self._stop()
        return self.snapshot()


def camera_router(manager):
    router=APIRouter(prefix='/kuavo/cameras')
    def invoke(fn,*args):
        try:return fn(*args)
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=409,detail=str(exc)) from None
    @router.get('/status')
    def status():return manager.snapshot()
    @router.post('/discover')
    def discover(body:CameraConfig):return invoke(manager.discover,body)
    @router.post('/connect')
    def connect(body:CameraConfig):return invoke(manager.connect,body)
    @router.post('/disconnect')
    def disconnect():return invoke(manager.close)
    @router.post('/reconnect')
    def reconnect():return invoke(manager.request_reconnect)
    @router.post('/settings')
    def settings(body:dict[str,CaptureProfile]):return invoke(manager.settings,{role:profile.model_dump() for role,profile in body.items()})
    @router.get('/{role}/stream')
    def stream(role:str):
        with manager.lock:feed=manager.feeds.get(role)
        if not feed:raise HTTPException(status_code=409,detail='该相机尚未连接')
        return StreamingResponse(feed.stream(),media_type='multipart/x-mixed-replace; boundary=frame',headers={'Cache-Control':'no-store'})
    return router
