#!/usr/bin/python
"""Bounded, serial snapshot service. Python 2.7 compatible; no camera open at idle."""
from __future__ import print_function
import argparse
import ctypes
import fcntl
import glob
import hashlib
import json
import os
import resource
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import tempfile
import time
import sys

MAX_FRAME = 2 * 1024 * 1024
CIPHERS = 'ECDHE-RSA-AES128-GCM-SHA256:AES128-GCM-SHA256'
ACTIVE = None
STOP = False

class Timespec(ctypes.Structure):
    _fields_ = [('sec', ctypes.c_long), ('nsec', ctypes.c_long)]

_clock = ctypes.CDLL('librt.so.1', use_errno=True).clock_gettime
def monotonic():
    ts = Timespec()
    if _clock(1, ctypes.byref(ts)) != 0:
        raise RuntimeError('Monotonic clock unavailable')
    return ts.sec + ts.nsec / 1e9

def exact(sock, size):
    result = b''
    while len(result) < size:
        part = sock.recv(size-len(result))
        if not part:
            raise IOError('Connection closed')
        result += part
    return result

def secret_equal(a, b):
    if not isinstance(a, (str, type(u''))) or len(a) != len(b):
        return False
    difference = 0
    for x, y in zip(a, b):
        difference |= ord(x) ^ ord(y)
    return difference == 0

def reply(sock, metadata, body=b''):
    metadata = dict(metadata, bytes=len(body))
    encoded = json.dumps(metadata, separators=(',', ':')).encode('utf-8')
    sock.sendall(struct.pack('!I', len(encoded)) + encoded + body)

def read(path):
    with open(path) as handle:
        return handle.read().strip()

def device_info(camera):
    path = '/sys/bus/usb/devices/' + camera['usb_path']
    available = False
    bus = address = None
    try:
        available = (read(path+'/idVendor') == camera['vid'] and
                     read(path+'/idProduct') == camera['pid'])
        if camera.get('serial'):
            available = available and read(path+'/serial') == camera['serial']
        bus, address = int(read(path+'/busnum')), int(read(path+'/devnum'))
    except (IOError, ValueError):
        pass
    result = dict((k, camera[k]) for k in ('id','usb_path','vid','pid','format','width','height','fps'))
    result.update(available=available, bus=bus, address=address)
    return result

def interface_idle(camera):
    paths = glob.glob('/sys/bus/usb/devices/' + camera['usb_path'] + ':*')
    video = []
    for path in paths:
        if read(path+'/bInterfaceClass').lower() != '0e':
            continue
        video.append(path)
        if os.path.exists(path+'/driver') or int(read(path+'/bAlternateSetting'),16) != 0:
            return False
    return bool(video)

def stop(signum, frame):
    global STOP
    STOP = True
    if ACTIVE is not None and ACTIVE.poll() is None:
        try:
            os.killpg(ACTIVE.pid, signal.SIGTERM)
        except OSError:
            pass

def worker_limits():
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CPU, (3,3))
    resource.setrlimit(resource.RLIMIT_AS, (128*1024*1024,128*1024*1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FRAME+8192,MAX_FRAME+8192))
    resource.setrlimit(resource.RLIMIT_CORE, (0,0))

class Service(object):
    def __init__(self, config, runtime='/run/fusion-camera'):
        self.config = config
        self.runtime = runtime
        self.last_finish = -1e9
        self.fault = None
        self.count = 0
        self.last_stream = None
        self.last_snapshot_error = None
        self.recovery_audit = None
        self.recovery_count = 0
        self.settings_supported = True
        self.camera_settings_rollback = {}

    def dispatch(self, request):
        if not isinstance(request, dict) or not secret_equal(request.get('token'), self.config['token']):
            return {'ok':False,'error':'unauthorized'}, b''
        method = request.get('method')
        if method == 'clock':
            return {'ok':True,'controller_monotonic_ns':int(monotonic()*1e9),
                    'boot_id':read('/proc/sys/kernel/random/boot_id')}, b''
        if method == 'status':
            return {'ok':True,'version':'2.3.2','mode':'snapshot_and_stream',
                    'observation_modes':['stationary','teleop_observation'],'graceful_stop':True,
                    'teleop_rt_policy':'teleop_observation_v1','stream_camera_selection':True,
                    'camera_hotplug_recovery':True,'camera_inventory_events':True,
                    'camera_controls':True,'per_camera_profiles':True,'stream_camera_controls':True,
                    'stream_joint_health':True,
                    'stream_transport_recovery':True,
                    'teleop_timing_policy':'diagnostic_only','max_teleop_stream_seconds':None,
                    'teleop_queue_overflow_policy':'drop_incoming',
                    'captures':self.count,'fault':self.fault,'idle':ACTIVE is None,
                    'last_snapshot_error':self.last_snapshot_error,
                    'recoverable_camera_fault':self.recovery_audit is not None,
                    'camera_recovery_requires_manual':bool(self.recovery_audit is not None and
                        (self.last_stream or {}).get('camera_recovery_requires_manual')),
                    'recovery_count':self.recovery_count,
                    'stream_hz':30,'supported_joint_hz':[30,100],'max_stream_seconds':600,'stream_unlimited':True,'last_stream':self.last_stream,
                    'cooldown_seconds':self.config['cooldown_seconds']}, b''
        if method == 'recover':
            from streaming import recover
            return recover(self, sys.modules[__name__], manual=request.get('manual', False)), b''
        if method == 'list':
            return {'ok':True,'cameras':[device_info(c) for c in self.config['cameras']]}, b''
        if method == 'camera_settings':
            try:
                from camera_controls import control
                return control(self, request, sys.modules[__name__]), b''
            except Exception as error:
                return {'ok':False,'error':'camera_settings_rejected','detail':str(error)}, b''
        if method == 'camera_settings_restore':
            try:
                from camera_controls import restore
                return restore(self, request, sys.modules[__name__]), b''
            except Exception as error:
                return {'ok':False,'error':'camera_settings_restore_rejected','detail':str(error)}, b''
        if method != 'snapshot':
            return {'ok':False,'error':'unknown_method'}, b''
        camera = next((c for c in self.config['cameras'] if c['id']==request.get('camera_id')), None)
        if camera is None:
            return {'ok':False,'error':'unknown_camera'}, b''
        if self.fault:
            return {'ok':False,'error':'capture_paused','detail':self.fault}, b''
        remaining = self.config['cooldown_seconds'] - (monotonic()-self.last_finish)
        if remaining > 0:
            return {'ok':False,'error':'rate_limited','retry_after':remaining}, b''
        info = device_info(camera)
        if not info['available']:
            return {'ok':False,'error':'camera_unavailable'}, b''
        try:
            if not interface_idle(camera):
                return {'ok':False,'error':'camera_busy'}, b''
            device = '/dev/bus/usb/%03d/%03d' % (info['bus'],info['address'])
            with open(os.devnull,'wb') as null:
                if subprocess.call(['/bin/fuser',device],stdout=null,stderr=null) == 0:
                    return {'ok':False,'error':'camera_busy'}, b''
            return self.capture(camera, info)
        except Exception as error:
            self.fault = 'Capture check failed: ' + type(error).__name__
            return {'ok':False,'error':'capture_paused','detail':self.fault}, b''

    def capture(self, camera, info):
        global ACTIVE
        directory = tempfile.mkdtemp(prefix='frame-',dir=self.runtime)
        start = monotonic()
        try:
            output = os.path.join(directory,'frame.bin')
            cmd = [self.config['helper'],str(info['bus']),str(info['address']),
                   camera['vid'],camera['pid'],camera['format'],str(camera['width']),
                   str(camera['height']),str(camera['fps']),output]
            with open(directory+'/metadata','wb') as out, open(directory+'/errors','wb') as err:
                ACTIVE = subprocess.Popen(cmd,stdout=out,stderr=err,preexec_fn=worker_limits,close_fds=True)
                while ACTIVE.poll() is None and not STOP and monotonic()-start<6:
                    time.sleep(0.02)
                if ACTIVE.poll() is None:
                    os.killpg(ACTIVE.pid,signal.SIGTERM)
                    deadline = monotonic()+1
                    while ACTIVE.poll() is None and monotonic()<deadline:
                        time.sleep(0.02)
                    if ACTIVE.poll() is None:
                        os.killpg(ACTIVE.pid,signal.SIGKILL)
                    ACTIVE.wait()
                    self.fault = 'Worker timed out or service stopped; no automatic capture retries'
                    return {'ok':False,'error':'capture_paused','detail':self.fault}, b''
                if ACTIVE.returncode != 0:
                    err.flush()
                    self.last_snapshot_error = read(directory+'/errors')[-2048:]
                    self.fault = 'Worker failed (exit %d); manual inspection required' % ACTIVE.returncode
                    return {'ok':False,'error':'capture_paused','detail':self.fault}, b''
            metadata = json.loads(read(directory+'/metadata'))
            size = os.path.getsize(output)
            if not 4 <= size <= MAX_FRAME or metadata.get('bytes') != size:
                raise ValueError('Invalid frame size')
            if metadata.get('format') != camera['format'] or metadata.get('width') != camera['width'] or metadata.get('height') != camera['height']:
                raise ValueError('Invalid frame metadata')
            if not interface_idle(camera):
                raise ValueError('Camera interface did not return to idle')
            with open(output,'rb') as handle:
                body = handle.read(MAX_FRAME+1)
            self.count += 1
            metadata.update(ok=True,camera_id=camera['id'],sequence=self.count,
                            sha256=hashlib.sha256(body).hexdigest(),
                            received_unix_time=time.time(),
                            completed_monotonic_seconds=monotonic(),
                            total_seconds=monotonic()-start)
            return metadata, body
        finally:
            ACTIVE = None
            self.last_finish = monotonic()
            shutil.rmtree(directory)

def validate(config):
    if len(config['token']) != 64 or any(c not in '0123456789abcdef' for c in config['token']):
        raise ValueError('Expected a 256-bit hex token')
    if config['port'] != 57491 or config['cooldown_seconds'] < 2:
        raise ValueError('Unsafe service limits')
    seen = set()
    for c in config['cameras']:
        if c['id'] in seen: raise ValueError('Duplicate camera ID')
        seen.add(c['id'])
        if c['usb_path'] not in ('1-1','1-2','1-4') or c['format'] not in ('mjpeg','yuyv'):
            raise ValueError('Invalid camera configuration')
        if not (160<=c['width']<=1024 and 120<=c['height']<=768 and 5<=c['fps']<=30):
            raise ValueError('Unsafe capture mode')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--check',action='store_true')
    args = parser.parse_args()
    with open(args.config) as handle: config = json.load(handle)
    validate(config)
    if args.check:
        print('Configuration valid'); return
    os.umask(0o077)
    runtime = '/run/fusion-camera'
    if not os.path.isdir(runtime): os.mkdir(runtime,0o700)
    lock = open(runtime+'/service.lock','a')
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    service = Service(config,runtime)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    # The dedicated address may appear after init starts; this job never blocks robot init.
    for attempt in range(120):
        try: listener.bind((config['host'],config['port'])); break
        except socket.error:
            if STOP or attempt==119: raise
            time.sleep(1)
    listener.listen(2); listener.settimeout(1)
    print('Fusion camera snapshot service ready; idle until authenticated request')
    while not STOP:
        client = None
        try:
            try: client,peer = listener.accept()
            except socket.timeout: continue
            client.settimeout(3)
            client = ssl.wrap_socket(client,server_side=True,certfile=config['certificate'],
                                     keyfile=config['private_key'],ssl_version=ssl.PROTOCOL_SSLv23,
                                     ciphers=CIPHERS)
            length = struct.unpack('!I',exact(client,4))[0]
            if not 1 <= length <= 1024: continue
            request = json.loads(exact(client,length).decode('utf-8'))
            if isinstance(request,dict) and request.get('method') == 'stream' and secret_equal(request.get('token'),config['token']):
                from streaming import run
                run(service,client,request,sys.modules[__name__])
                continue
            metadata,body = service.dispatch(request)
            if not STOP: reply(client,metadata,body)
        except (IOError,ValueError,TypeError,socket.error,ssl.SSLError):
            pass
        finally:
            if client is not None: client.close()
    listener.close()

if __name__ == '__main__': main()
