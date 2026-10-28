"""Read existing ROS1 image publishers directly. No SSH, ROS install or control calls.

Protocol: ROS TCPROS subscriber header and sensor_msgs Image/CompressedImage.
Only discovery, publisher negotiation and inbound image sockets are used.
"""
import http.client
from io import BytesIO
import socket
import struct
import time
from urllib.parse import urlsplit
import uuid
import xmlrpc.client

from PIL import Image
from .kuavo_cameras import CameraFeed

TYPES={'sensor_msgs/Image','sensor_msgs/CompressedImage'}
MAX_IMAGE=32*1024*1024
CALLER='/app_camera_readonly'


class TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self,host):
        return http.client.HTTPConnection(host,timeout=3)


def rpc(uri,method,*args):
    parsed=urlsplit(uri)
    if parsed.scheme!='http' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('无效 ROS 节点地址')
    try:
        with xmlrpc.client.ServerProxy(uri,transport=TimeoutTransport()) as peer:
            result=getattr(peer,method)(*args)
    except (OSError,xmlrpc.client.Error,http.client.HTTPException) as exc:
        raise RuntimeError('ROS 网络连接失败：'+str(exc)) from exc
    if not isinstance(result,(list,tuple)) or len(result)!=3 or result[0]!=1:
        raise RuntimeError('ROS 查询未成功：'+str(result)[:400])
    return result[2]


def discover(master):
    topics=rpc(master,'getPublishedTopics',CALLER,'')
    state=rpc(master,'getSystemState',CALLER)
    pubs=dict(state[0]);nodes={};sources=[]
    for topic,kind in topics:
        if kind not in TYPES or topic.endswith('/compressedDepth'):continue
        publishers=pubs.get(topic,[])
        if len(publishers)!=1:continue  # Never mix two publishers under one camera identity.
        name=publishers[0]
        if name not in nodes:nodes[name]=rpc(master,'lookupNode',CALLER,name)
        sources.append(dict(id='ros:'+topic,kind='ros',topic=topic,message_type=kind,
                            publisher=nodes[name],label=topic))
    return dict(sources=sources,warnings=[])


def field(data):return struct.pack('<I',len(data))+data


def exact(sock,size,deadline=None):
    out=bytearray()
    while len(out)<size:
        if deadline is not None:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise socket.timeout('图像帧接收超时')
            sock.settimeout(remaining)
        chunk=sock.recv(size-len(out))
        if not chunk:raise RuntimeError('相机发布端已关闭连接')
        out.extend(chunk)
    return bytes(out)


def packet(sock,limit):
    deadline=time.monotonic()+5
    size=struct.unpack('<I',exact(sock,4,deadline))[0]
    if not 0<size<=limit:raise ValueError('无效 ROS 图像数据长度')
    return exact(sock,size,deadline)


class Cursor:
    def __init__(self,data):self.data=data;self.offset=0
    def take(self,size):
        if size<0 or self.offset+size>len(self.data):raise ValueError('ROS 图像数据不完整')
        value=self.data[self.offset:self.offset+size];self.offset+=size
        return value
    def u32(self):return struct.unpack('<I',self.take(4))[0]
    def blob(self,limit):
        size=self.u32()
        if size>limit:raise ValueError('ROS 字段超出长度限制')
        return self.take(size)


def decode_message(data,kind):
    """Return JPEG and the actual source header; honor padded rows and channel order."""
    c=Cursor(data);seq,sec,nsec=c.u32(),c.u32(),c.u32()
    if nsec>=1_000_000_000:raise ValueError('无效相机时间戳')
    c.blob(4096)  # frame_id
    if kind=='sensor_msgs/CompressedImage':
        fmt=c.blob(1024).decode()
        if 'compressedDepth' in fmt:raise ValueError('主画面需要彩色图像，不能使用压缩深度消息')
        encoded=c.blob(8*1024*1024)
        image=Image.open(BytesIO(encoded))
        if image.format not in ('JPEG','PNG'):raise ValueError('不支持的压缩图像格式')
    elif kind=='sensor_msgs/Image':
        height,width=c.u32(),c.u32();encoding=c.blob(128).decode()
        c.take(1)  # endian flag: only 8-bit encodings supported below.
        step=c.u32();pixels=c.blob(MAX_IMAGE)
        modes={'rgb8':('RGB','RGB',3),'bgr8':('RGB','BGR',3),'mono8':('L','L',1),
               'rgba8':('RGBA','RGBA',4),'bgra8':('RGBA','BGRA',4)}
        if encoding not in modes:raise ValueError('不支持的预览像素格式：'+encoding)
        mode,raw,channels=modes[encoding]
        if not 0<width<=8192 or not 0<height<=8192 or width*height>16_777_216:
            raise ValueError('无效相机图像尺寸')
        if step<width*channels or len(pixels)!=step*height:raise ValueError('图像行长度不匹配')
        image=Image.frombytes(mode,(width,height),pixels,'raw',raw,step,1)
    else:raise ValueError('不支持的 ROS 图像类型')
    if c.offset!=len(data):raise ValueError('ROS 图像消息含多余数据')
    if not 0<image.width<=8192 or not 0<image.height<=8192 or image.width*image.height>16_777_216:
        raise ValueError('无效相机图像尺寸')
    image=image.convert('RGB')
    image.thumbnail((1280,1280))
    out=BytesIO();image.save(out,format='JPEG',quality=80)
    return out.getvalue(),image.width,image.height,(sec,nsec,seq)


class DirectRosFeed(CameraFeed):
    def __init__(self,source):
        self.socket=None
        super().__init__(source)

    def _run(self,source):
        try:
            caller=CALLER+'_'+uuid.uuid4().hex[:10]
            negotiated=rpc(source['publisher'],'requestTopic',caller,source['topic'],[['TCPROS']])
            if (not isinstance(negotiated,list) or len(negotiated)!=3 or negotiated[0]!='TCPROS'
                    or not isinstance(negotiated[1],str) or type(negotiated[2]) is not int
                    or not 0<negotiated[2]<65536):raise ValueError('无效 ROS 图像传输地址')
            if self.stop_event.is_set():return
            with socket.create_connection((negotiated[1],negotiated[2]),timeout=3) as sock:
                self.socket=sock;sock.settimeout(5)
                if self.stop_event.is_set():return
                header={'callerid':caller,'topic':source['topic'],'type':source['message_type'],'md5sum':'*','tcp_nodelay':'1'}
                sock.sendall(field(b''.join(field((k+'='+v).encode()) for k,v in header.items())))
                cursor=Cursor(packet(sock,65536));response={}
                while cursor.offset<len(cursor.data):
                    key,value=cursor.blob(65536).decode().split('=',1);response[key]=value
                if 'error' in response:raise ValueError(response['error'])
                if response.get('type')!=source['message_type']:raise ValueError('相机发布消息类型已改变，请重新检测')
                previous=None;last_new=time.monotonic()
                while not self.stop_event.is_set():
                    data=packet(sock,MAX_IMAGE)
                    jpeg,width,height,stamp=decode_message(data,source['message_type'])
                    if previous is not None and stamp<=previous:
                        if time.monotonic()-last_new>5:raise RuntimeError('相机时间戳没有递增，请重连视频')
                        continue
                    previous=stamp;last_new=time.monotonic()
                    with self.lock:
                        self.frame=jpeg;self.width=width;self.height=height
                        self.received=last_new;self.sequence+=1
        except socket.timeout:
            if not self.stop_event.is_set():self.error='相机未返回新图像；请检查相机是否已开流'
        except Exception as exc:
            if not self.stop_event.is_set():self.error=str(exc)
        finally:self.socket=None

    def stop(self):
        self.stop_event.set()
        sock=self.socket
        if sock is not None:
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        self.thread.join(timeout=9)
