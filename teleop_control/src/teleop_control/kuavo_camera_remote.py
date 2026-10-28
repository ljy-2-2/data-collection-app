"""Camera-only SSH payload. No robot control imports, services or ROS publishers.

Discovery never opens a video stream. Streaming owns only its capture/subscriber.
A parent watches the SSH input lease and terminates its own child on disconnect.
"""
import glob
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

HEADER = struct.Struct('!II')
MAX_JPEG = 8*1024*1024
# These are the two commissioned side-camera ports, not the Orbbec head camera.
NATIVE_SIDE_PATHS = frozenset('/dev/v4l/by-path/pci-0000:00:14.0-usb-0:'+port+':1.0-video-index0'
                             for port in ('2','4'))


def native_jpeg(frame, width, height):
    """Validate the camera's compressed bytes and dimensions without decoding."""
    jpeg=frame.tobytes()
    # These UVC cameras pad bytesused to an eight-byte boundary. Remove only
    # the observed zero padding after EOI; never reconstruct a truncated JPEG.
    padding=len(jpeg)-len(jpeg.rstrip(b'\x00'))
    if 0<padding<=7:jpeg=jpeg[:-padding]
    if not (0<len(jpeg)<=MAX_JPEG and jpeg.startswith(b'\xff\xd8') and jpeg.endswith(b'\xff\xd9')):
        raise RuntimeError('相机未返回原生 JPEG 数据')
    offset=2
    while offset+4<=len(jpeg):
        if jpeg[offset]!=255:break
        while offset<len(jpeg) and jpeg[offset]==255:offset+=1
        if offset>=len(jpeg):break
        marker=jpeg[offset];offset+=1
        if marker in (0xda,0xd9):break
        if marker==1 or 0xd0<=marker<=0xd7:continue
        if offset+2>len(jpeg):break
        size=int.from_bytes(jpeg[offset:offset+2],'big')
        if size<2 or offset+size>len(jpeg):break
        if marker in (0xc0,0xc1,0xc2):
            if size<8:break
            actual_h=int.from_bytes(jpeg[offset+3:offset+5],'big')
            actual_w=int.from_bytes(jpeg[offset+5:offset+7],'big')
            if (actual_w,actual_h)!=(width,height):raise RuntimeError('原生 JPEG 分辨率与设置回读不匹配')
            return jpeg
        offset+=size
    raise RuntimeError('原生 JPEG 缺少有效尺寸信息')


def configure_native(camera, cv2, source):
    paths={source['path'],*source.get('aliases',[])}
    if not paths.intersection(NATIVE_SIDE_PATHS):return False
    # The existing FPS guard may require YUYV at small resolutions; that format
    # is uncompressed and still needs encoding. Never treat it as JPEG bytes.
    if camera.get(cv2.CAP_PROP_FOURCC)!=cv2.VideoWriter_fourcc(*'MJPG'):return False
    if not camera.set(cv2.CAP_PROP_CONVERT_RGB,0) or camera.get(cv2.CAP_PROP_CONVERT_RGB)!=0:
        raise RuntimeError('相机驱动未接受原生 JPEG 读取')
    return True


def capabilities(fd, formats):
    import fcntl
    sizes=set()
    for fmt in formats:
        if fmt not in ('MJPG','YUYV'):continue
        fourcc=struct.unpack('I',fmt.encode())[0]
        for i in range(64):
            buf=bytearray(44);struct.pack_into('II',buf,0,i,fourcc)
            try:fcntl.ioctl(fd,0xc02c564a,buf)
            except OSError:break
            kind,w,h=struct.unpack_from('III',buf,8)
            if kind==1 and 0<w<=4096 and 0<h<=2160:sizes.add((w,h))
    brightness=None
    try:
        buf=bytearray(68);struct.pack_into('I',buf,0,0x980900)
        fcntl.ioctl(fd,0xc0445624,buf)
        low,high,step,default,flags=struct.unpack_from('iiiii',buf,40)
        current=bytearray(struct.pack('Ii',0x980900,0));fcntl.ioctl(fd,0xc008561b,current)
        if not flags & 1:brightness=dict(min=low,max=high,step=step,default=default,value=struct.unpack_from('i',current,4)[0])
    except OSError:pass
    return dict(resolutions=[dict(width=w,height=h) for w,h in sorted(sizes)],brightness=brightness)


def inventory():
    import fcntl
    sources = []
    seen = set()
    # Bind side cameras to physical ports. Identical USB cameras can advertise the
    # same serial, making their shared by-id symlink jump between devices on boot.
    paths = sorted(glob.glob('/dev/v4l/by-path/*')) + sorted(glob.glob('/dev/v4l/by-id/*'))
    for path in paths:
        real = os.path.realpath(path)
        if real in seen:continue
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            buf = bytearray(104)
            fcntl.ioctl(fd, 0x80685600, buf)  # VIDIOC_QUERYCAP; no capture/format writes.
            caps, device_caps = struct.unpack_from('II', buf, 84)
            caps = device_caps if caps & 0x80000000 else caps
            if not caps & 1:continue
            card = bytes(buf[16:48]).split(b'\0')[0].decode(errors='replace')
            seen.add(real)
            formats=[]
            for index in range(32):
                fmt=bytearray(64);struct.pack_into('II',fmt,0,index,1)
                try:fcntl.ioctl(fd,0xc0405602,fmt)  # VIDIOC_ENUM_FMT, read only.
                except OSError:break
                formats.append(bytes(fmt[44:48]).decode(errors='replace'))
            aliases=[p for p in paths if os.path.realpath(p)==real]
            sources.append(dict(id='usb:'+path,kind='usb',path=path,aliases=aliases,formats=formats,
                                label=card+' · '+Path(path).name,capabilities=capabilities(fd,formats)))
        except OSError:
            continue
        finally:
            if fd is not None:os.close(fd)
    warnings = []
    try:
        import socket
        socket.setdefaulttimeout(3)
        import rosgraph
        for topic, kind in rosgraph.Master('/app_camera_inventory').getPublishedTopics(''):
            if kind in ('sensor_msgs/Image','sensor_msgs/CompressedImage') and not topic.endswith('/compressedDepth'):
                sources.append(dict(id='ros:'+topic,kind='ros',topic=topic,message_type=kind,label=topic))
    except Exception as exc:
        warnings.append('ROS 图像话题读取失败：'+str(exc))
    try:
        import cv2
        opencv = cv2.__version__
    except ImportError:
        opencv = None
        warnings.append('工控机缺少 python3-opencv，USB 预览暂不可用')
    return dict(sources=sources,warnings=warnings,opencv=opencv)


def send(jpeg, width, height, sequence, settings=None):
    if not (0 < len(jpeg) <= MAX_JPEG and jpeg.startswith(b'\xff\xd8') and jpeg.endswith(b'\xff\xd9')):
        raise ValueError('无效 JPEG 图像')
    meta=json.dumps(dict(width=width,height=height,sequence=sequence,settings=settings,source_monotonic_ns=time.monotonic_ns())).encode()
    sys.stdout.buffer.write(HEADER.pack(len(meta),len(jpeg))+meta+jpeg)
    sys.stdout.buffer.flush()


def configure_usb(camera, cv2, profile=None):
    profile=profile or {}
    width,height=profile.get("width",640),profile.get("height",480)
    camera.set(cv2.CAP_PROP_FOURCC,cv2.VideoWriter_fourcc(*'MJPG'))
    camera.set(cv2.CAP_PROP_FRAME_WIDTH,width)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT,height)
    camera.set(cv2.CAP_PROP_FPS,30)
    # Some UVC cameras return success for 30 fps but negotiate ~120 fps at
    # 640x480 MJPEG. Check readback before STREAMON; that mode can starve a peer.
    if camera.get(cv2.CAP_PROP_FPS)>30.5:
        yuyv=cv2.VideoWriter_fourcc(*'YUYV')
        camera.set(cv2.CAP_PROP_FOURCC,yuyv)
        camera.set(cv2.CAP_PROP_FRAME_WIDTH,width)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT,height)
        camera.set(cv2.CAP_PROP_FPS,30)
        fps=camera.get(cv2.CAP_PROP_FPS)
        if not (camera.get(cv2.CAP_PROP_FOURCC)==yuyv
                and camera.get(cv2.CAP_PROP_FRAME_WIDTH)==width
                and camera.get(cv2.CAP_PROP_FRAME_HEIGHT)==height and 0<fps<=30.5):
            raise RuntimeError(f'相机未接受预览帧率限制，YUYV {width}×{height}/30 fps 回读不匹配')
    # Two buffers let UVC receive the next frame while OpenCV retrieves the
    # current one. A single buffer measured ~18 fps on the paired HD USB cameras;
    # two sustain ~30 fps without a large capture queue.
    camera.set(cv2.CAP_PROP_BUFFERSIZE,2)
    if profile.get('brightness') is not None:
        if not camera.set(cv2.CAP_PROP_BRIGHTNESS,profile['brightness']):raise RuntimeError('相机拒绝亮度设置')
        if abs(camera.get(cv2.CAP_PROP_BRIGHTNESS)-profile['brightness'])>.01:raise RuntimeError('相机亮度回读不匹配')
    actual=dict(width=int(camera.get(cv2.CAP_PROP_FRAME_WIDTH)),height=int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT)),fps=camera.get(cv2.CAP_PROP_FPS))
    if (actual['width'],actual['height'])!=(width,height):raise RuntimeError('相机分辨率回读不匹配')
    if profile.get('brightness') is not None:actual['brightness']=camera.get(cv2.CAP_PROP_BRIGHTNESS)
    return actual


def capture(source):
    import cv2
    cv2.setNumThreads(1)
    sequence=0
    if source['kind']=='usb':
        path=source['path']
        if not path.startswith(('/dev/v4l/by-id/','/dev/v4l/by-path/')) or not Path(path).exists():
            raise RuntimeError('相机身份不存在，请重新检测；不会替换为其它 video 设备')
        # Refuse an already opened device. Never stop or replace another camera process.
        occupied=subprocess.run(['fuser',os.path.realpath(path)],capture_output=True,timeout=3)
        if occupied.returncode==0:raise RuntimeError('相机已被其它进程占用：'+path)
        if occupied.returncode!=1:raise RuntimeError('无法核对相机占用状态')
        camera=cv2.VideoCapture(path,cv2.CAP_V4L2)
        try:
            if not camera.isOpened():raise RuntimeError('无法打开 USB 相机：'+path)
            applied=configure_usb(camera,cv2,source.get("profile"))
            native=configure_native(camera,cv2,source)
            applied['encoding']='camera_native_jpeg' if native else 'software_jpeg'
            while True:
                ok,frame=camera.read()
                if not ok:raise RuntimeError('USB 相机读取失败：'+path)
                if native:
                    jpeg=native_jpeg(frame,applied['width'],applied['height'])
                    send(jpeg,applied['width'],applied['height'],sequence,applied)
                    sequence+=1
                    continue
                # Preview only; never claim these frames form a synchronized dataset.
                ok,jpeg=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,80])
                if not ok:raise RuntimeError('图像编码失败')
                send(jpeg.tobytes(),int(frame.shape[1]),int(frame.shape[0]),sequence,applied)
                sequence+=1
        finally:camera.release()
    else:
        import rospy
        from cv_bridge import CvBridge
        from sensor_msgs.msg import Image,CompressedImage
        latest=[None];changed=threading.Condition();bridge=CvBridge()
        def receive(message):
            with changed:latest[0]=message;changed.notify()
        rospy.init_node('app_camera_preview',anonymous=True,disable_signals=True)
        compressed=source['message_type']=='sensor_msgs/CompressedImage'
        subscriber=rospy.Subscriber(source['topic'],CompressedImage if compressed else Image,receive,
                                    queue_size=1,buff_size=16*1024*1024)
        try:
            while not rospy.is_shutdown():
                with changed:
                    if latest[0] is None:changed.wait(5)
                    message,latest[0]=latest[0],None
                if message is None:raise RuntimeError('相机话题没有新画面：'+source['topic'])
                frame=(bridge.compressed_imgmsg_to_cv2(message,'bgr8') if compressed
                       else bridge.imgmsg_to_cv2(message,'bgr8'))
                height,width=frame.shape[:2]
                if width>1280:frame=cv2.resize(frame,(1280,max(1,round(height*1280/width))))
                ok,jpeg=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,80])
                if not ok:raise RuntimeError('图像编码失败')
                send(jpeg.tobytes(),int(frame.shape[1]),int(frame.shape[0]),sequence);sequence+=1
        finally:subscriber.unregister()


def run(options):
    if options['action']=='discover':
        print(json.dumps(inventory(),ensure_ascii=False),flush=True)
        return
    import multiprocessing
    import signal
    stopped=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):
        signal.signal(sig,lambda *_:stopped.set())
    def lease():
        try:
            # Unbuffered read avoids a daemon holding stdin's Python IO lock at exit.
            while os.read(0,1):pass
        finally:stopped.set()
    # fork before starting threads; child owns only one video capture/subscription.
    def child():
        for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(sig,signal.SIG_DFL)
        try:capture(options['source'])
        except Exception as exc:
            print(str(exc),file=sys.stderr,flush=True)
            raise SystemExit(2)
    process=multiprocessing.get_context('fork').Process(target=child)
    process.start()
    threading.Thread(target=lease,daemon=True).start()
    try:
        while process.is_alive() and not stopped.wait(.1):pass
    finally:
        if process.is_alive():process.terminate()
        process.join(2)
        if process.is_alive():process.kill();process.join(2)
    if process.exitcode not in (0,-15):raise SystemExit(2)
