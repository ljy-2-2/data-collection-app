"""ROS camera protocol tests. Fake RPC and local socket pairs only; no robot IO."""
from io import BytesIO
import socket
import struct
import threading
import time
from unittest.mock import Mock

from PIL import Image
import pytest
from teleop_control import kuavo_camera_ros as ros
from teleop_control.kuavo_cameras import CameraConfig, KuavoCameras, HEAD_SOURCE, head_aliases


def header(seq=1):return struct.pack('<III',seq,10,seq)+ros.field(b'camera_frame')


def compressed(seq=1):
    b=BytesIO();Image.new('RGB',(4,3),(255,0,0)).save(b,format='JPEG')
    return header(seq)+ros.field(b'jpeg')+ros.field(b.getvalue())


def test_raw_decoder_honors_bgr_padding_and_compressed_type():
    raw=header()+struct.pack('<II',1,2)+ros.field(b'bgr8')+b'\0'+struct.pack('<I',8)+ros.field(b'\x00\x00\xff'*2+b'\x00\x00')
    jpeg,w,h,stamp=ros.decode_message(raw,'sensor_msgs/Image')
    assert (w,h,stamp)==(2,1,(10,1,1))
    pixel=Image.open(BytesIO(jpeg)).getpixel((0,0))
    assert pixel[0]>230 and pixel[1]<20 and pixel[2]<20
    jpeg,w,h,stamp=ros.decode_message(compressed(),'sensor_msgs/CompressedImage')
    assert (w,h)==(4,3)
    with pytest.raises(ValueError):ros.decode_message(raw[:-1],'sensor_msgs/Image')
    with pytest.raises(ValueError):ros.decode_message(compressed()+b'junk','sensor_msgs/CompressedImage')
    with pytest.raises(ValueError):ros.decode_message(header()+ros.field(b'16UC1; compressedDepth png')+ros.field(b'fake'),'sensor_msgs/CompressedImage')
    with pytest.raises(ValueError):ros.decode_message(header()+struct.pack('<II',100000,100000)+ros.field(b'rgb8')+b'\0'+struct.pack('<I',1)+ros.field(b'xx'),'sensor_msgs/Image')


def test_discovery_reads_only_and_excludes_ambiguous_publishers(monkeypatch):
    replies={'getPublishedTopics':[['/cam_h/color/image_raw/compressed','sensor_msgs/CompressedImage'],['/bad','sensor_msgs/Image'],['/cam/depth/compressedDepth','sensor_msgs/CompressedImage']],
             'getSystemState':[[['/cam_h/color/image_raw/compressed',['/head']],['/bad',['/one','/two']]],[],[]],
             'lookupNode':'http://127.0.0.1:1234'}
    calls=[]
    def fake(uri,method,*args):calls.append(method);return replies[method]
    monkeypatch.setattr(ros,'rpc',fake)
    sources=ros.discover('http://127.0.0.1:11311')['sources']
    assert len(sources)==1 and sources[0]['publisher']=='http://127.0.0.1:1234'
    assert calls==['getPublishedTopics','getSystemState','lookupNode']
    aliased=head_aliases(sources)
    assert next(s for s in aliased if s['id']==HEAD_SOURCE)['topic']=='/cam_h/color/image_raw/compressed'
    assert all(s['role_hint']=='head' for s in aliased)


def test_direct_manager_never_launches_ssh_and_blocks_head_as_side(tmp_path,monkeypatch):
    monkeypatch.setattr('subprocess.run',Mock(side_effect=AssertionError('SSH forbidden')))
    monkeypatch.setattr('subprocess.Popen',Mock(side_effect=AssertionError('SSH forbidden')))
    sources=[dict(id='ros:/cam_h/color/image_raw',kind='ros',topic='/cam_h/color/image_raw',message_type='sensor_msgs/Image',publisher='http://127.0.0.1:1',label='head')]
    monkeypatch.setattr(ros,'discover',Mock(return_value=dict(sources=sources,warnings=[])))
    feed=Mock();feed.snapshot.return_value=dict(connected=True,error='',state='streaming')
    factory=Mock(return_value=feed);monkeypatch.setattr(ros,'DirectRosFeed',factory)
    manager=KuavoCameras(tmp_path);config=CameraConfig(transport='ros')
    assert config.transport=='ros'
    manager.discover(config)
    with pytest.raises(ValueError,match='头部相机只能'):
        manager.connect(CameraConfig(transport='ros',roles={'left':sources[0]['id']}))
    result=manager.connect(config)
    assert result['cameras'][0]['connected']
    assert factory.call_args.args[0]['topic']=='/cam_h/color/image_raw'
    manager.close();feed.stop.assert_called_once()


def wait_for(fn):
    until=time.monotonic()+3
    while not fn():
        if time.monotonic()>until:raise AssertionError('fake producer timeout')
        time.sleep(.01)


def test_direct_feed_fragmentation_duplicates_disconnect_and_shared_consumers(monkeypatch):
    client,server=socket.socketpair()
    monkeypatch.setattr(socket,'create_connection',lambda *a,**kw:client)
    calls=[]
    def rpc(uri,method,*args):calls.append(method);return ['TCPROS','127.0.0.1',1234]
    monkeypatch.setattr(ros,'rpc',rpc)
    release=threading.Event();sent_duplicate=threading.Event()
    def producer():
        with server:
            fields=ros.packet(server,65536)
            assert b'topic=/head' in fields and b'callerid=' in fields
            response=ros.field(b'type=sensor_msgs/CompressedImage')
            server.sendall(ros.field(response))
            frame=ros.field(compressed())
            server.sendall(frame[:3]);server.sendall(frame[3:])
            release.wait(2)
            server.sendall(frame);sent_duplicate.set()
            try:
                while server.recv(100):pass
            except OSError:pass
    producer_thread=threading.Thread(target=producer);producer_thread.start()
    feed=ros.DirectRosFeed(dict(publisher='http://127.0.0.1:1234',topic='/head',message_type='sensor_msgs/CompressedImage'))
    try:
        wait_for(lambda:feed.snapshot()['connected'])
        initial=feed.received
        release.set();assert sent_duplicate.wait(2);time.sleep(.05)
        assert feed.sequence==1 and feed.received==initial
        assert (feed.width,feed.height)==(4,3)
        assert calls==['requestTopic']
    finally:
        release.set();feed.stop();producer_thread.join(2)
    assert not feed.thread.is_alive() and not producer_thread.is_alive()
    assert not feed.snapshot()['connected']


def test_bad_publisher_header_is_not_reported_online(monkeypatch):
    client,server=socket.socketpair()
    monkeypatch.setattr(socket,'create_connection',lambda *a,**kw:client)
    monkeypatch.setattr(ros,'rpc',lambda *a:['TCPROS','127.0.0.1',1234])
    def producer():
        with server:
            ros.packet(server,65536)
            server.sendall(ros.field(ros.field(b'type=std_msgs/Bool')))
    t=threading.Thread(target=producer);t.start()
    feed=ros.DirectRosFeed(dict(publisher='http://127.0.0.1:1',topic='/head',message_type='sensor_msgs/Image'))
    try:
        wait_for(lambda:bool(feed.error))
        assert '类型已改变' in feed.error and not feed.snapshot()['connected']
    finally:feed.stop();t.join(2)
