"""Bounded authenticated TLS stream; Python 2.7; no robot commands or writes."""
from __future__ import print_function
import collections
import fcntl
import hashlib
import json
import os
import resource
import select
import signal
import struct
import subprocess
import tempfile
import threading
import time
from joints import JointReader
from camera_audit import dmesg_fingerprint, audit_changed

HEADER = struct.Struct('<4sIIQIIIII')
MAX_FRAME = 2*1024*1024

class Cancelled(Exception):
    pass

def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(65536)
            if not b: break
            h.update(b)
    return h.hexdigest()

def audit():
    out = {}
    for name, path in [('xenomai','/proc/xenomai/stat'), ('modules','/proc/modules'),
                       ('mounts','/proc/mounts'), ('cpu','/proc/stat'), ('memory','/proc/meminfo'),
                       ('network','/proc/net/dev')]:
        with open(path) as f: out[name] = f.read()
    out['robot_pids'] = subprocess.check_output(['pgrep','-x','FXProducer|FXPSI|FXTcpFileServer|EtherCAT-OP']).decode().split()
    out['config_hashes'] = {p:sha(p) for p in ['/etc/init.d/FXStart.sh','/home/FUSION/Shell/run.sh',
        '/home/FUSION/Shell/start.sh','/boot/config-3.14.18-ipipe','/etc/network/interfaces','/etc/fstab','/etc/rc.local']}
    out.update(dmesg_fingerprint(subprocess.check_output(['dmesg'])))
    return out

def limits():
    os.setsid()
    resource.setrlimit(resource.RLIMIT_AS, (256*1024*1024,)*2)
    resource.setrlimit(resource.RLIMIT_CORE, (0,0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024*1024,)*2)

class JointSampler(threading.Thread):
    def __init__(self, api, reader, seconds, hz=100, stationary_only=True):
        threading.Thread.__init__(self)
        self.daemon = True
        self.api, self.reader, self.seconds = api, reader, seconds
        self.hz = hz
        self.stationary_only = stationary_only
        self.done = threading.Event()
        self.queue = collections.deque()
        self.lock = threading.Lock()
        self.error = None
        self.count = 0
        self.skipped = 0
        self.max_lateness_ns = 0

    def run(self):
        start = self.api.monotonic()
        previous = None
        last_change = start
        try:
            for tick in range(self.seconds*self.hz):
                scheduled = start+tick/float(self.hz)
                if self.done.wait(max(0, scheduled-self.api.monotonic())): break
                now = self.api.monotonic()
                late = max(0, int((now-scheduled)*1e9))
                self.max_lateness_ns = max(self.max_lateness_ns, late)
                if late > 100000000:
                    raise RuntimeError('Joint sampler delayed over 100 ms')
                if late > int(1e9/self.hz):
                    self.skipped += 1
                    continue  # Do not fabricate catch-up samples.
                sample = self.reader.sample()
                if sample['frame_serial'] != previous:
                    previous, last_change = sample['frame_serial'], now
                if now-last_change > .1:
                    raise RuntimeError('Joint feedback stale for 100 ms')
                if self.stationary_only and max(abs(v) for v in sample['velocity_deg_s']) > 1:
                    raise RuntimeError('Stationary-test joint velocity limit exceeded')
                if sample['arm_state'][2] or sample['arm_state'][5]:
                    raise RuntimeError('Robot reports arm error; stopping our reader')
                sample.update(type='joints', ok=True, tick=tick, sequence=self.count,
                              scheduled_monotonic_ns=int(scheduled*1e9), lateness_ns=late)
                with self.lock:
                    if len(self.queue) >= 128:
                        raise RuntimeError('Joint output queue overflow')
                    self.queue.append(sample)
                self.count += 1
        except Exception as error:
            self.error = str(error)

    def pop(self):
        with self.lock:
            return self.queue.popleft() if self.queue else None

def run(service, sock, request, api):
    seconds = request.get('seconds', 60)
    joint_hz = request.get('joint_hz',30)
    mode = request.get('observation_mode', 'stationary')
    if mode not in ('stationary', 'teleop_observation'):
        api.reply(sock, {'ok':False,'error':'invalid_observation_mode'})
        return
    if type(seconds) is not int or not 1 <= seconds <= 600 or request.get('hz',30) != 30 or type(joint_hz) is not int or joint_hz not in (30,100):
        api.reply(sock, {'ok':False,'error':'invalid_stream_limits'})
        return
    if service.fault or api.ACTIVE is not None:
        api.reply(sock, {'ok':False,'error':'capture_paused','detail':service.fault or 'busy'})
        return
    helper = os.path.join(os.path.dirname(service.config['helper']), 'capture-three')
    manifest_path = os.path.join(os.path.dirname(helper), 'stream-manifest.json')
    reader = sampler = process = errors = None
    before = after = None
    error = None
    counts = [0,0,0]
    started = False
    stop_reason = 'duration_limit'
    joint_sent = 0
    diagnostics = []
    try:
        with open(manifest_path) as f: manifest = json.load(f)
        for path, digest in manifest['sha256'].items():
            if sha(path) != digest: raise RuntimeError('Stream firmware/helper hash mismatch: '+path)
        infos = []
        for camera in service.config['cameras']:
            info = api.device_info(camera)
            if not info['available'] or info['bus'] != 1 or not api.interface_idle(camera):
                raise RuntimeError('Camera missing or busy: '+camera['id'])
            with open(os.devnull, 'wb') as null:
                if subprocess.call(['/bin/fuser','/dev/bus/usb/%03d/%03d' % (info['bus'],info['address'])],
                                   stdout=null,stderr=null) == 0:
                    raise RuntimeError('Camera already opened')
            infos.append(info)
        if [i['id'] for i in infos] != ['usb-1','usb-2','usb-4']:
            raise RuntimeError('Unexpected camera order')
        before = audit()
        reader = JointReader(api.monotonic)
        sock.settimeout(1.0)
        api.reply(sock, {'ok':True,'type':'start','version':'2.2.0','seconds':seconds,'hz':30,'joint_hz':joint_hz,
                        'observation_mode':mode,'graceful_stop':True,
                        'cameras':['usb-1','usb-2','usb-4'],'native_size':[1024,768],
                        'timestamp_clock':'FUSION CLOCK_MONOTONIC','joint_source':'/dev/FXDcss PROT_READ',
                        'boot_id':api.read('/proc/sys/kernel/random/boot_id'),
                        'controller_monotonic_ns':int(api.monotonic()*1e9),
                        'controller_unix_ns':int(time.time()*1e9)})
        started = True
        if True:
            errors = tempfile.TemporaryFile(dir=service.runtime)
            process = subprocess.Popen([helper,str(seconds)]+[str(i['address']) for i in infos]+['mjpeg'],
                stdout=subprocess.PIPE,stderr=errors,preexec_fn=limits,close_fds=True)
            api.ACTIVE = process
            fd = process.stdout.fileno()
            fcntl.fcntl(fd,fcntl.F_SETFL,fcntl.fcntl(fd,fcntl.F_GETFL)|os.O_NONBLOCK)
            buf = b''
            deadline = api.monotonic()+seconds+12
            eof = False
            last_monitor = api.monotonic()
            while not eof or (sampler is not None and sampler.is_alive()):
                if api.STOP or api.monotonic()>deadline: raise RuntimeError('Stream interrupted or timed out')
                if eof and process.poll() not in (None,0): raise RuntimeError('Native helper failed')
                if sampler is not None:
                    if sampler.error: raise RuntimeError(sampler.error)
                    while True:
                        sample = sampler.pop()
                        if sample is None: break
                        api.reply(sock, sample)
                        joint_sent += 1
                # Detect a closed client even between camera frames.
                ready = select.select(([fd] if not eof else [])+[sock], [], [], .005)[0]
                if sock in ready:
                    control = sock.recv(1)
                    if control == b'S':
                        stop_reason = 'client_stop'
                        raise Cancelled('Client requested normal end')
                    if not control: raise IOError('Client disconnected before audited end')
                    raise ValueError('Unexpected data during stream')
                if fd in ready:
                    chunk = os.read(fd,65536)
                    if not chunk:
                        eof = True
                        if process.poll() not in (None,0): raise RuntimeError('Native helper failed')
                    else:
                        buf += chunk
                while len(buf) >= HEADER.size:
                    magic, cam, seq, stamp, size, width, height, pts, scr = HEADER.unpack(buf[:40])
                    if magic != b'FJM1' or cam>2 or not 128<=size<=MAX_FRAME or (width,height)!=(1024,768):
                        raise ValueError('Invalid native camera record')
                    if len(buf)<40+size: break
                    body, buf = buf[40:40+size], buf[40+size:]
                    if sampler is None:
                        sampler = JointSampler(api,reader,seconds,joint_hz,mode == 'stationary')
                        sampler.start()
                    api.reply(sock, {'ok':True,'type':'image','camera_id':infos[cam]['id'],
                        'sequence':seq,'timestamp_ns':stamp,'format':'mjpeg','width':width,'height':height,
                        'device_pts_raw':pts,'device_scr_raw':scr,
                        'timestamp_kind':'controller_monotonic_frame_completion'},body)
                    counts[cam] += 1
                if api.monotonic()-last_monitor >= 2:
                    with open('/proc/xenomai/stat') as f: rt = f.read()
                    # Native helper also stops if RT mode-switch/page-fault counts change.
                    api.reply(sock, {'ok':True,'type':'health','timestamp_ns':int(api.monotonic()*1e9),
                                     'xenomai':rt,'frames':counts[:]})
                    last_monitor = api.monotonic()
            if buf: raise RuntimeError('Truncated native frame')
            if sampler is not None:
                while True:
                    sample = sampler.pop()
                    if sample is None: break
                    api.reply(sock,sample)
                    joint_sent += 1
            # EOF does not necessarily mean the helper has finished its final cleanup.
            until = api.monotonic()+3
            while process.poll() is None and api.monotonic()<until: time.sleep(.01)
            if process.poll() != 0: raise RuntimeError('Native helper failed or cleanup timed out')
            errors.seek(0)
            diagnostics = errors.read(1024*1024).decode('utf-8','replace').splitlines()
            if not all(counts) or sampler is None or sampler.count == 0:
                raise RuntimeError('Missing image or joint stream')
    except Exception as exc:
        error = None if isinstance(exc, Cancelled) else str(exc)
        if not isinstance(exc, (Cancelled, IOError)):
            service.fault = 'Stream stopped: '+error
    finally:
        if sampler is not None:
            sampler.done.set()
            sampler.join(1)
            if sampler.error:
                error = sampler.error
                service.fault = error
            if stop_reason == 'client_stop' and error is None:
                try:
                    while True:
                        sample = sampler.pop()
                        if sample is None: break
                        api.reply(sock, sample)
                        joint_sent += 1
                except Exception as exc:
                    error = str(exc)
        if process is not None and process.poll() is None:
            try: os.killpg(process.pid,signal.SIGTERM)
            except OSError: pass
            until = api.monotonic()+3
            while process.poll() is None and api.monotonic()<until: time.sleep(.02)
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL)
            process.wait()
        if process is not None: process.stdout.close()
        if errors is not None:
            errors.seek(0)
            diagnostics = errors.read(1024*1024).decode('utf-8','replace').splitlines()
            errors.close()
        api.ACTIVE = None
        if reader is not None: reader.close()
        service.last_finish = api.monotonic()
        if process is not None and any(not api.interface_idle(c) for c in service.config['cameras']):
            error = 'Camera interface did not return to idle'
            service.fault = error
        if before is not None:
            after = audit()
            for key in ('robot_pids','modules','mounts','config_hashes','dmesg_sha256'):
                if audit_changed(before, after, key, mode):
                    error = (error+'; ' if error else '')+'Audit changed: '+key
                    service.fault = error
        result = {'ok':error is None,'type':'end','error':error,'frames':counts,
            'joint_hz':joint_hz,
            'joint_samples':joint_sent, 'stop_reason':stop_reason, 'observation_mode':mode,
            'joint_skipped_ticks':sampler.skipped if sampler else 0,
            'joint_max_lateness_ns':sampler.max_lateness_ns if sampler else None,
            'native_exit':process.returncode if process else None,
            'native_diagnostics':diagnostics, 'audit_before':before,'audit_after':after}
        service.last_stream = dict((k,v) for k,v in result.items() if not k.startswith('audit_') and k!='native_diagnostics')
        try: api.reply(sock,result)
        except (IOError,ValueError): pass
