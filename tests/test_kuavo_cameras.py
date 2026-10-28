"""Camera preview tests use fake SSH output and local synthetic producers only."""
import asyncio
import json
import shlex
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from teleop_control.kuavo_cameras import CameraConfig, CameraFeed, KuavoCameras, ssh_command


@pytest.mark.parametrize('mjpeg_fps,yuyv_fps,expected',[(120.1,30,'YUYV'),(30,30,'MJPG'),(120.1,120.1,None)])
def test_usb_checks_negotiated_fps_even_when_set_reports_success(mjpeg_fps,yuyv_fps,expected):
    from teleop_control.kuavo_camera_remote import configure_usb
    cv=SimpleNamespace(CAP_PROP_FOURCC='fourcc',CAP_PROP_FRAME_WIDTH='width',CAP_PROP_FRAME_HEIGHT='height',
                       CAP_PROP_FPS='fps',CAP_PROP_BUFFERSIZE='buffer',VideoWriter_fourcc=lambda *v:''.join(v))
    class Camera:
        def __init__(self):self.values={}
        def set(self,key,value):self.values[key]=value;return True
        def get(self,key):
            if key=='fps':return mjpeg_fps if self.values['fourcc']=='MJPG' else yuyv_fps
            return self.values[key]
    camera=Camera()
    if expected is None:
        with pytest.raises(RuntimeError,match='回读不匹配'):configure_usb(camera,cv)
    else:
        configure_usb(camera,cv)
        assert camera.values['fourcc']==expected
        assert camera.get('fps')<=30.5
        assert (camera.get('width'),camera.get('height'))==(640,480)


def test_identical_usb_serials_keep_roles_bound_to_physical_ports(monkeypatch):
    import struct
    from teleop_control import kuavo_camera_remote as remote
    ports=['/dev/v4l/by-path/port-left','/dev/v4l/by-path/port-right']
    shared='/dev/v4l/by-id/duplicated-serial'
    target={ports[0]:'/dev/video0',ports[1]:'/dev/video2',shared:'/dev/video0'}
    monkeypatch.setattr(remote.glob,'glob',lambda pattern:ports if '/by-path/' in pattern else [shared])
    monkeypatch.setattr(remote.os.path,'realpath',lambda path:target[path])
    monkeypatch.setattr(remote.os,'open',lambda *args:10)
    monkeypatch.setattr(remote.os,'close',lambda fd:None)
    def query(fd,request,buf):
        if request!=0x80685600:raise OSError('unsupported query')
        struct.pack_into('II',buf,84,1,0)
        buf[16:22]=b'Camera'
    monkeypatch.setattr('fcntl.ioctl',query)
    monkeypatch.setattr('socket.setdefaulttimeout',lambda value:None)
    monkeypatch.setitem(sys.modules,'rosgraph',SimpleNamespace(Master=lambda _:SimpleNamespace(getPublishedTopics=lambda _:[])))
    monkeypatch.setitem(sys.modules,'cv2',SimpleNamespace(__version__='fake'))
    before=remote.inventory()['sources']
    target[shared]='/dev/video2'  # Same serial resolves to the other device after reboot.
    after=remote.inventory()['sources']
    assert [s['path'] for s in before]==ports
    assert [s['id'] for s in after]==[s['id'] for s in before]


def test_status_does_not_connect_and_config_validation(tmp_path,monkeypatch):
    run=Mock(side_effect=AssertionError('unexpected SSH'));monkeypatch.setattr('subprocess.run',run)
    m=KuavoCameras(tmp_path)
    assert [c['id'] for c in m.snapshot()['cameras']]==['head','left','right']
    assert m.snapshot()['sources']==[]
    m.close();run.assert_not_called()
    for change in ({'host':'-oProxyCommand=oops'},{'user':'a;id'},{'ros_master':'file:///tmp/test'},
                   {'roles':{'head':'a','left':'a'}},{'roles':{'other':'a'}},{'ros_setup':'relative'}):
        with pytest.raises(ValueError):CameraConfig(**change).checked()


def test_shell_quotes_paths_and_does_not_reuse_another_peer_socket(tmp_path):
    config=CameraConfig(ros_setup="/tmp/a'$(oops); setup.bash").checked()
    command=ssh_command(config,tmp_path/'control',{'action':'discover'})
    assert '-T' in command and 'BatchMode=yes' in command and 'StrictHostKeyChecking=yes' in command
    shell=shlex.split(command[-1])[-1]
    assert shlex.quote(config['ros_setup']) in shell
    assert 'roslaunch' not in shell and 'roscore' not in shell
    other=ssh_command({**config,'host':'example.local'},tmp_path/'control',{'action':'discover'})
    assert command[command.index('-S')+1]!=other[other.index('-S')+1]


def test_discovery_roles_and_persistence(tmp_path,monkeypatch):
    sources=[dict(id='usb:/dev/v4l/by-path/left',kind='usb',path='/dev/v4l/by-path/left',label='USB L'),
             dict(id='ros:/camera/color/image_raw',kind='ros',topic='/camera/color/image_raw',message_type='sensor_msgs/Image',label='Head')]
    run=Mock(return_value=SimpleNamespace(returncode=0,stdout=json.dumps(dict(sources=sources,warnings=[])),stderr=''))
    monkeypatch.setattr('subprocess.run',run)
    feed=Mock();feed.snapshot.return_value=dict(connected=True,state='streaming',error='')
    factory=Mock(return_value=feed);monkeypatch.setattr('teleop_control.kuavo_cameras.CameraFeed',factory)
    m=KuavoCameras(tmp_path);m.discover(CameraConfig(transport="ssh"))
    assert factory.call_count==0 and not m.path.exists()
    with pytest.raises(ValueError):m.connect(CameraConfig(roles={'right':'missing'}))
    with pytest.raises(ValueError):m.connect(CameraConfig(host='other',roles={'left':sources[0]['id']}))
    chosen=CameraConfig(transport='ssh',roles={'left':sources[0]['id'],'head':sources[1]['id']})
    state=m.connect(chosen)
    assert factory.call_count==2 and state['cameras'][2]['connected'] is False
    assert stat.S_IMODE(m.path.stat().st_mode)==0o600
    assert KuavoCameras(tmp_path).config['roles']==chosen.roles
    m.close();assert feed.stop.call_count==2
    assert not any(c['connected'] for c in m.snapshot()['cameras'])


def wait_for(predicate,timeout=3):
    deadline=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError('producer timed out')
        time.sleep(.01)


def test_feed_reuses_latest_frame_and_stops_owned_process():
    code="""import json,struct,sys
meta=json.dumps(dict(width=640,height=480,sequence=0)).encode();jpeg=b'\\xff\\xd8test\\xff\\xd9'
sys.stdout.buffer.write(struct.pack('!II',len(meta),len(jpeg))+meta+jpeg);sys.stdout.buffer.flush()
sys.stdin.read()
"""
    feed=CameraFeed([sys.executable,'-u','-c',code])
    try:
        wait_for(lambda:feed.snapshot()['connected'])
        async def first():
            reader=feed.stream()
            try:return await anext(reader)
            finally:await reader.aclose()
        assert asyncio.run(first())==asyncio.run(first())
        assert b'Content-Length: 8' in asyncio.run(first())
        feed.received-=3
        assert not feed.snapshot()['connected']
    finally:feed.stop()
    assert not feed.thread.is_alive() and feed.process.poll() is not None


def test_feed_rejects_bad_wire_and_reports_ssh_errors():
    for code in ("import sys;sys.stdout.buffer.write(b'xxxxxxxx');sys.stdout.buffer.flush()",
                 "import sys;print('Permission denied',file=sys.stderr);sys.exit(255)"):
        feed=CameraFeed([sys.executable,'-u','-c',code])
        try:
            wait_for(lambda:bool(feed.error))
            assert not feed.snapshot()['connected']
            if 'Permission' in code:assert 'Permission denied' in feed.error
        finally:feed.stop()


def test_failed_discovery_does_not_stop_working_video(tmp_path,monkeypatch):
    m=KuavoCameras(tmp_path);feed=Mock();m.feeds={'left':feed}
    monkeypatch.setattr('subprocess.run',Mock(return_value=SimpleNamespace(returncode=255,stderr='Permission denied')))
    with pytest.raises(RuntimeError,match='Permission denied'):m.discover(CameraConfig(transport="ssh"))
    feed.stop.assert_not_called()
    assert m.error=='Permission denied'


def test_remote_lease_shutdown_and_failed_producer_are_clean():
    import subprocess
    from pathlib import Path
    from teleop_control import kuavo_camera_remote
    source=Path(kuavo_camera_remote.__file__).read_text()
    for producer,expected in (("time.sleep(120)",0),("raise RuntimeError('fake camera offline')",2)):
        code=source+'\ndef capture(source):\n    '+producer+"\nrun({'action':'stream','source':{}})\n"
        process=subprocess.Popen([sys.executable,'-u','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if expected==0:
            time.sleep(.15);process.stdin.close();process.stdin=None
        try:
            stdout,stderr=process.communicate(timeout=4)
            assert process.returncode==expected,stderr.decode()
            assert b'Fatal Python error' not in stderr
        finally:
            if process.poll() is None:process.kill();process.wait()



def test_head_camera_is_fixed_and_never_replaced_by_side_usb(tmp_path,monkeypatch):
    from teleop_control.kuavo_cameras import HEAD_SOURCE
    assert CameraConfig().checked()['roles']['head']==HEAD_SOURCE
    with pytest.raises(ValueError,match='头部'):
        CameraConfig(roles={'head':'usb:/dev/v4l/by-path/left'}).checked()
    with pytest.raises(ValueError,match='同一相机'):
        CameraConfig(roles={'left':HEAD_SOURCE}).checked()
    m=KuavoCameras(tmp_path)
    source=dict(id='usb:/dev/v4l/by-path/left',kind='usb',path='/dev/v4l/by-path/left',label='USB L')
    monkeypatch.setattr('subprocess.run',Mock(return_value=SimpleNamespace(returncode=0,stdout=json.dumps(dict(sources=[source],warnings=[])),stderr='')))
    state=m.discover(CameraConfig(transport="ssh"))
    assert '头部相机' in state['warnings'][0]
    feed=Mock();feed.snapshot.return_value=dict(connected=True,state='streaming',error='')
    factory=Mock(return_value=feed);monkeypatch.setattr('teleop_control.kuavo_cameras.CameraFeed',factory)
    state=m.connect(CameraConfig(transport='ssh',roles={'left':source['id']}))
    assert factory.call_count==1
    assert state['cameras'][0]['source']==HEAD_SOURCE and not state['cameras'][0]['connected']
    assert state['cameras'][1]['connected']
    assert not state['cameras'][2]['connected']
    assert m.config['roles']=={'head':HEAD_SOURCE,'left':source['id']}
    m.close()


def test_auto_discovers_physical_head_and_usb_sides_independently(tmp_path,monkeypatch):
    from teleop_control import kuavo_cameras as cameras,kuavo_camera_ros as ros
    calls=[]
    def discover(peer,socket):
        calls.append(peer['host'])
        if peer['host']=='192.168.26.22':
            return dict(sources=[dict(id=key,path=key[4:],kind='usb',label=role) for role,key in cameras.SIDE_ROLES.items()])
        return dict(sources=[dict(id='usb:head-port',path='/dev/v4l/by-path/head-color',kind='usb',label='Orbbec',
                                 aliases=[cameras.HEAD_USB],formats=['MJPG'])])
    monkeypatch.setattr(cameras,'discover_ssh',discover)
    monkeypatch.setattr(ros,'discover',lambda _:dict(sources=[],warnings=[]))
    commands=[];feeds=[]
    def factory(command):
        commands.append(command);f=Mock();f.snapshot.return_value=dict(connected=True,state='streaming',error='');feeds.append(f);return f
    monkeypatch.setattr(cameras,'CameraFeed',factory)
    m=KuavoCameras(tmp_path);m.config['transport']='auto'
    m.request_reconnect();m.reconnect_thread.join(3)
    state=m.snapshot()
    assert not state['reconnecting'] and all(c['connected'] for c in state['cameras'])
    assert state['config']['roles']['left']==cameras.SIDE_ROLES['left']
    assert any('leju_kuavo@192.168.26.12' in cmd for cmd in commands)
    assert sum('jt@192.168.26.22' in cmd for cmd in commands)==2
    first=state['stream_id']
    m.request_reconnect();m.reconnect_thread.join(3)
    assert m.snapshot()['stream_id']!=first
    assert all(f.stop.call_count==1 for f in feeds[:3])
    m.close()
    assert all(f.stop.call_count==1 for f in feeds)


def test_auto_head_failure_does_not_block_usb_and_never_uses_chassis_ros_as_head(tmp_path,monkeypatch):
    from teleop_control import kuavo_cameras as cameras,kuavo_camera_ros as ros
    monkeypatch.setattr(ros,'discover',lambda _:dict(sources=[dict(id='ros:/camera_1/color/image_raw',kind='ros',topic='/camera_1/color/image_raw')]))
    def discover(peer,socket):
        if peer['host']=='192.168.26.12':raise RuntimeError('head SSH unavailable')
        return dict(sources=[dict(id=v,path=v[4:],kind='usb',label=k) for k,v in cameras.SIDE_ROLES.items()])
    monkeypatch.setattr(cameras,'discover_ssh',discover)
    f=Mock();f.snapshot.return_value=dict(connected=True,state='streaming',error='')
    monkeypatch.setattr(cameras,'CameraFeed',lambda command:f)
    m=KuavoCameras(tmp_path);m.config['transport']='auto';m.request_reconnect();m.reconnect_thread.join(3)
    state=m.snapshot()
    assert not state['cameras'][0]['connected'] and 'head SSH unavailable' in state['cameras'][0]['error']
    assert all(c['connected'] for c in state['cameras'][1:])
    m.close()


def test_close_during_discovery_prevents_late_camera_start(tmp_path,monkeypatch):
    import threading
    from teleop_control import kuavo_cameras as cameras
    entered=threading.Event();release=threading.Event()
    def discover(config,socket):entered.set();release.wait(3);return dict(sources=[],warnings=[])
    monkeypatch.setattr(cameras,'discover_network',discover)
    factory=Mock(side_effect=AssertionError('late start'));monkeypatch.setattr(cameras,'CameraFeed',factory)
    m=KuavoCameras(tmp_path);m.request_reconnect();assert entered.wait(2)
    first=m.reconnect_thread;m.request_reconnect();assert m.reconnect_thread is first
    generation=m.generation
    closer=threading.Thread(target=m.close);closer.start()
    wait_for(lambda:m.generation!=generation)
    release.set();closer.join(3);first.join(3)
    assert not closer.is_alive() and not first.is_alive()
    assert not m.snapshot()['reconnecting'] and not m.feeds
    factory.assert_not_called()

@pytest.mark.parametrize('accepted',[True,False])
def test_camera_settings_readback_and_rollback(tmp_path,monkeypatch,accepted):
    from teleop_control.kuavo_cameras import SIDE_ROLES
    m=KuavoCameras(tmp_path);key=SIDE_ROLES['left']
    m.sources=[dict(id=key,kind='usb',capabilities=dict(resolutions=[dict(width=640,height=480),dict(width=1280,height=720)],brightness=dict(min=-64,max=64,step=1)))]
    original=dict(m.config)
    profile=dict(width=1280,height=720,brightness=12)
    calls=[]
    def connect(config,**kw):
        calls.append((config,kw));m.config=config.checked()
        values=profile if accepted else {**profile,'brightness':0}
        m.feeds={'left':SimpleNamespace(snapshot=lambda:dict(connected=True,settings=values))}
    monkeypatch.setattr(m,'connect',connect)
    if accepted:
        m.settings({'left':profile})
        assert json.loads(m.path.read_text())['profiles']['left']==profile
        assert calls[0][1]['persist'] is False
    else:
        with pytest.raises(RuntimeError,match='回读'):m.settings({'left':profile})
        assert m.config==original and not m.path.exists()
        assert len(calls)==2
    for bad in ({**profile,'width':111},{**profile,'brightness':65}):
        with pytest.raises(ValueError):m.settings({'left':bad})


def test_camera_settings_reject_recording_without_io(tmp_path,monkeypatch):
    m=KuavoCameras(tmp_path);m.recording_guard=lambda:True
    connect=Mock(side_effect=AssertionError('must not reconnect'));monkeypatch.setattr(m,'connect',connect)
    with pytest.raises(RuntimeError,match='采集'):m.settings({'left':dict(width=640,height=480)})
    connect.assert_not_called()


def test_network_default_migrates_old_config_and_never_calls_ssh(tmp_path,monkeypatch):
    from teleop_control import kuavo_cameras as cameras,kuavo_network_client as network
    path=tmp_path/'teleop_control/runtime/kuavo/cameras.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps(CameraConfig(transport='auto').model_dump()))
    def discover(root,host):
        if host=='192.168.26.12':return dict(sources=[dict(id='usb:head-port',kind='usb',aliases=[cameras.HEAD_USB])])
        return dict(sources=[dict(id=v,kind='usb') for v in cameras.SIDE_ROLES.values()])
    monkeypatch.setattr(network,'discover',discover)
    monkeypatch.setattr(network,'profile',lambda root,host:dict(url='https://'+host+':7444'))
    monkeypatch.setattr(cameras,'discover_ssh',Mock(side_effect=AssertionError('SSH forbidden')))
    commands=[]
    def factory(command):
        commands.append(command);f=Mock();f.snapshot.return_value=dict(connected=True,state='streaming',error='');return f
    monkeypatch.setattr(cameras,'CameraFeed',factory)
    m=KuavoCameras(tmp_path);assert m.config['transport']=='network'
    m.request_reconnect();m.reconnect_thread.join(3)
    assert len(commands)==3 and all('kuavo_network_client.py' in c[2] and '/camera' in c for c in commands)
    assert all(c['connected'] for c in m.snapshot()['cameras'])
    m.close()


def test_native_usb_capture_forwards_bytes_without_decoding_or_encoding(monkeypatch):
    import struct
    from teleop_control import kuavo_camera_remote as remote
    width,height=1920,1080
    # Synthetic SOF with a compressed-body placeholder: forwarding must be byte exact.
    jpeg=b'\xff\xd8\xff\xe0\x00\x04ab\xff\xc0'+struct.pack('>HBHHB',17,8,height,width,3)+b'\x01\x11\x00\x02\x11\x00\x03\x11\x00'+b'\xff\xda\x00\x02payload\xff\xd9'
    raw=SimpleNamespace(tobytes=lambda:jpeg,shape=(1,len(jpeg)))
    class Camera:
        def __init__(self):self.values={};self.count=0;self.released=False
        def isOpened(self):return True
        def set(self,k,v):self.values[k]=v;return True
        def get(self,k):return self.values[k]
        def read(self):
            self.count+=1
            if self.count>1:return False,None
            return True,raw
        def release(self):self.released=True
    camera=Camera();encode=Mock(side_effect=AssertionError('native frame re-encoded'))
    cv=SimpleNamespace(CAP_PROP_FOURCC='fourcc',CAP_PROP_FRAME_WIDTH='width',CAP_PROP_FRAME_HEIGHT='height',
        CAP_PROP_FPS='fps',CAP_PROP_BUFFERSIZE='buffer',CAP_PROP_CONVERT_RGB='convert',CAP_V4L2=0,
        VideoWriter_fourcc=lambda *v:''.join(v),setNumThreads=lambda n:None,VideoCapture=lambda *a:camera,imencode=encode)
    monkeypatch.setitem(sys.modules,'cv2',cv)
    monkeypatch.setattr(remote.Path,'exists',lambda _:True)
    monkeypatch.setattr(remote.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=1))
    send=Mock();monkeypatch.setattr(remote,'send',send)
    with pytest.raises(RuntimeError,match='读取失败'):
        remote.capture(dict(kind='usb',path=next(iter(remote.NATIVE_SIDE_PATHS)),profile=dict(width=width,height=height)))
    assert send.call_args.args[:4]==(jpeg,width,height,0)
    assert send.call_args.args[4]['encoding']=='camera_native_jpeg'
    assert camera.values['convert']==0 and camera.released
    encode.assert_not_called()
    for count in range(8):
        assert remote.native_jpeg(SimpleNamespace(tobytes=lambda:jpeg+b'\x00'*count),width,height)==jpeg
    with pytest.raises(RuntimeError,match='分辨率'):remote.native_jpeg(raw,640,480)
    for bad in (b'notjpeg',b'\xff\xd8\xff\xc0\x00\x01\xff\xd9',jpeg[:-2],jpeg+b'\x00'*8,jpeg+b'junk'):
        with pytest.raises(RuntimeError):remote.native_jpeg(SimpleNamespace(tobytes=lambda:bad),width,height)


def test_native_mode_does_not_change_head_or_treat_yuyv_as_jpeg():
    from teleop_control.kuavo_camera_remote import configure_native,NATIVE_SIDE_PATHS
    cv=SimpleNamespace(CAP_PROP_FOURCC='fourcc',CAP_PROP_CONVERT_RGB='convert',VideoWriter_fourcc=lambda *v:''.join(v))
    camera=Mock();camera.get.return_value='YUYV'
    assert configure_native(camera,cv,{'path':next(iter(NATIVE_SIDE_PATHS))}) is False
    camera.set.assert_not_called()
    assert configure_native(camera,cv,{'path':'/dev/v4l/by-path/head-color'}) is False
    camera.set.assert_not_called()
    camera.get.return_value='MJPG';camera.set.return_value=False
    with pytest.raises(RuntimeError,match='未接受'):configure_native(camera,cv,{'path':next(iter(NATIVE_SIDE_PATHS))})
