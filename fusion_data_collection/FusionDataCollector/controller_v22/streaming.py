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
import socket
import struct
import subprocess
import tempfile
import threading
import time
from joints import JointReader

HEADER = struct.Struct('<4sIIQIIIII')
MAX_FRAME = 2*1024*1024

def stream_profile(manifest, mode):
    """Use an explicit native format; never silently substitute compressed frames."""
    legacy = dict(format='mjpeg', width=1024, height=768, fps=30)
    profile = manifest.get('teleop_profile', legacy) if mode == 'teleop_observation' else legacy
    allowed = (legacy, dict(format='yuyv', width=640, height=480, fps=30))
    if profile not in allowed:
        raise ValueError('Unsupported native stream profile')
    return profile

def validate_header(record, profile):
    magic, cam, seq, stamp, size, width, height, pts, scr = record
    yuyv = profile['format'] == 'yuyv'
    if (magic != (b'FJY1' if yuyv else b'FJM1') or cam > 2 or
            (width, height) != (profile['width'], profile['height']) or
            (size != width*height*2 if yuyv else not 128 <= size <= MAX_FRAME)):
        raise ValueError('Invalid native camera record')

class Cancelled(Exception):
    pass

class NativeHelperError(Exception):
    pass

def compact_text(value, limit=512):
    """Bound JSON-escaped size, including non-ASCII diagnostics on Python 2."""
    while len(json.dumps(value)) > limit:
        value = value[:max(0, len(value)-max(1, len(value)//4))]
    return value

def diagnostic_tail(lines):
    """Keep the status response comfortably below the client's 8192-byte cap."""
    tail = []
    for line in reversed(lines):
        candidate = [compact_text(line)] + tail
        if len(json.dumps(candidate)) > 2048:
            break
        tail = candidate
    return tail

def native_failure_reason(lines):
    # The helper writes a human-readable reason before JSON cleanup counters.
    for line in reversed(lines):
        if line.strip() and not line.lstrip().startswith(('{', '[')):
            return compact_text(line.strip())
    return ''

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
    out['dmesg_sha256'] = hashlib.sha256(subprocess.check_output(['dmesg'])).hexdigest()
    return out

def rt_snapshot(text):
    tasks = {}
    for line in text.splitlines():
        fields = line.split()
        if not any(name in fields for name in ('RTPSI', 'FX_RtTask')):
            continue
        if len(fields) != 8 or fields[-1] not in ('RTPSI', 'FX_RtTask') or fields[-1] in tasks:
            raise ValueError('Malformed or duplicate RT task')
        values = [int(v) for v in fields[:5]]
        if min(values) < 0: raise ValueError('Negative RT counters')
        tasks[fields[-1]] = values  # CPU, PID, MSW, CSW, PF
    if set(tasks) != set(('RTPSI', 'FX_RtTask')): raise ValueError('Missing RT task')
    return tasks

AUDIT_KEYS = ('robot_pids','modules','mounts','config_hashes','dmesg_sha256')

def unchanged_for_recovery(before, after, mode='stationary'):
    try:
        from camera_audit import audit_changed
        if any(audit_changed(before, after, k, mode) for k in AUDIT_KEYS): return False
        old, new = rt_snapshot(before['xenomai']), rt_snapshot(after['xenomai'])
        for name in old:
            a, b = old[name], new[name]
            if a[:2] != b[:2] or b[4] != a[4] or b[2] < a[2] or b[3] < a[3]: return False
            if name == 'FX_RtTask' and b[2] != a[2]: return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def native_rt_unchanged(lines):
    states = {}
    try:
        for line in lines:
            if not line.startswith('{'): continue
            row = json.loads(line)
            if row.get('event') in ('rt_baseline', 'rt_final'):
                key = (row['event'], row['task'])
                if key in states: return False
                states[key] = row
        for task in ('RTPSI', 'FX_RtTask'):
            a, b = states['rt_baseline', task], states['rt_final', task]
            if any(a[k] != b[k] for k in ('cpu','pid','pf','process_start_ticks')): return False
            if b['csw'] < a['csw'] or b['msw'] < a['msw']: return False
            if task == 'FX_RtTask' and b['msw'] != a['msw']: return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def recover(service, api):
    """One explicit next-round retry after an isolated, fully cleaned camera fault."""
    baseline = getattr(service, 'recovery_audit', None)
    if api.ACTIVE is not None or not service.fault or baseline is None:
        return {'ok':False, 'error':'recovery_not_available'}
    try:
        if api.monotonic()-service.last_finish < service.config['cooldown_seconds']:
            raise RuntimeError('Camera cooldown has not finished')
        current = audit()
        mode = (service.last_stream or {}).get('observation_mode', 'stationary')
        if not unchanged_for_recovery(baseline, current, mode):
            raise RuntimeError('Controller audit changed; recovery refused')
        with open(os.path.join(os.path.dirname(service.config['helper']), 'stream-manifest.json')) as f:
            manifest = json.load(f)
        for path, digest in manifest['sha256'].items():
            if sha(path) != digest: raise RuntimeError('Helper integrity mismatch')
        for camera in service.config['cameras']:
            info = api.device_info(camera)
            if not info['available'] or not api.interface_idle(camera):
                raise RuntimeError('Camera missing or busy')
            with open(os.devnull,'wb') as null:
                result = subprocess.call(['/bin/fuser','/dev/bus/usb/%03d/%03d' % (info['bus'],info['address'])],
                                         stdout=null,stderr=null)
            if result != 1: raise RuntimeError('Camera busy or resource check failed')
    except Exception as exc:
        return {'ok':False,'error':'recovery_refused','detail':str(exc)}
    service.fault = None
    service.recovery_audit = None
    service.recovery_count = getattr(service, 'recovery_count', 0)+1
    return {'ok':True,'recovered':True,'recovery_count':service.recovery_count}

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
    service.recovery_audit = None
    helper = os.path.join(os.path.dirname(service.config['helper']),
                          'capture-three-teleop' if mode == 'teleop_observation' else 'capture-three')
    timestamp_kind = 'controller_monotonic_frame_completion'
    manifest_path = os.path.join(os.path.dirname(helper), 'stream-manifest.json')
    reader = sampler = process = errors = None
    before = after = None
    error = None
    counts = [0,0,0]
    started = False
    stop_reason = 'duration_limit'
    joint_sent = 0
    diagnostics = []
    native_failed = False
    cleanup_timed_out = False
    native_termination = None
    try:
        with open(manifest_path) as f: manifest = json.load(f)
        profile = stream_profile(manifest, mode)
        if helper not in manifest['sha256']:
            raise RuntimeError('Stream helper is missing from the integrity manifest: '+helper)
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
        colors = {}
        if profile['format'] == 'yuyv':
            from camera_color import read_camera_color
            colors = {c['id']:read_camera_color(c['usb_path']) for c in service.config['cameras']}
        before = audit()
        reader = JointReader(api.monotonic)
        sock.settimeout(1.0)
        # Interleaved small joint records must not delay the tail of a large
        # image while TCP waits for an acknowledgement of a partial segment.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        api.reply(sock, {'ok':True,'type':'start','version':'2.3.2','seconds':seconds,'hz':30,'joint_hz':joint_hz,
                        'observation_mode':mode,'graceful_stop':True,
                        'timestamp_kind':timestamp_kind,
                        'rt_policy':'teleop_observation_v1' if mode == 'teleop_observation' else 'stationary',
                        'cameras':['usb-1','usb-2','usb-4'],
                        'format':profile['format'],'native_size':[profile['width'],profile['height']],
                        'camera_color_matching':colors,
                        'timestamp_clock':'FUSION CLOCK_MONOTONIC','joint_source':'/dev/FXDcss PROT_READ',
                        'boot_id':api.read('/proc/sys/kernel/random/boot_id'),
                        'controller_monotonic_ns':int(api.monotonic()*1e9),
                        'controller_unix_ns':int(time.time()*1e9)})
        started = True
        if True:
            errors = tempfile.TemporaryFile(dir=service.runtime)
            command = [helper,str(seconds)]+[str(i['address']) for i in infos]
            command.append(profile['format'])
            process = subprocess.Popen(command,
                stdout=subprocess.PIPE,stderr=errors,preexec_fn=limits,close_fds=True)
            api.ACTIVE = process
            fd = process.stdout.fileno()
            fcntl.fcntl(fd,fcntl.F_SETFL,fcntl.fcntl(fd,fcntl.F_GETFL)|os.O_NONBLOCK)
            buf = b''
            deadline = api.monotonic()+seconds+30
            eof = False
            last_monitor = api.monotonic()
            while not eof or (sampler is not None and sampler.is_alive()):
                if api.STOP or api.monotonic()>deadline: raise RuntimeError('Stream interrupted or timed out')
                if eof and process.poll() not in (None,0): raise NativeHelperError('Native helper failed')
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
                        if sampler is not None: sampler.done.set()
                        if process.poll() not in (None,0): raise NativeHelperError('Native helper failed')
                    else:
                        buf += chunk
                while len(buf) >= HEADER.size:
                    record = HEADER.unpack(buf[:40])
                    validate_header(record, profile)
                    magic, cam, seq, stamp, size, width, height, pts, scr = record
                    if len(buf)<40+size: break
                    body, buf = buf[40:40+size], buf[40+size:]
                    if sampler is None:
                        sampler = JointSampler(api,reader,seconds+30,joint_hz,mode == 'stationary')
                        sampler.start()
                    metadata = {'ok':True,'type':'image','camera_id':infos[cam]['id'],
                        'sequence':seq,'timestamp_ns':stamp,'format':profile['format'],'width':width,'height':height,
                        'device_pts_raw':pts,'device_scr_raw':scr,
                        'timestamp_kind':timestamp_kind}
                    metadata.update(colors.get(infos[cam]['id'], {}))
                    api.reply(sock, metadata, body)
                    counts[cam] += 1
                if api.monotonic()-last_monitor >= 2:
                    with open('/proc/xenomai/stat') as f: rt = f.read()
                    # Teleop records RTPSI switches; page faults and FX_RtTask
                    # switches still stop capture. Stationary keeps strict checks.
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
            if process.poll() is None:
                cleanup_timed_out = True
                raise NativeHelperError('Native helper cleanup timed out')
            if process.poll() != 0: raise NativeHelperError('Native helper failed')
            errors.seek(0)
            diagnostics = errors.read(1024*1024).decode('utf-8','replace').splitlines()
            if not all(counts) or sampler is None or sampler.count == 0:
                raise RuntimeError('Missing image or joint stream')
    except Exception as exc:
        native_failed = isinstance(exc, NativeHelperError)
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
        if process is not None:
            exit_before_cleanup = process.poll()
            if exit_before_cleanup not in (None, 0):
                native_failed = True
            if exit_before_cleanup is None:
                native_termination = 'client_stop' if stop_reason == 'client_stop' else 'error_cleanup'
                try: os.killpg(process.pid,signal.SIGTERM)
                except OSError: pass
                until = api.monotonic()+3
                while process.poll() is None and api.monotonic()<until: time.sleep(.02)
                if process.poll() is None:
                    cleanup_timed_out = True
                    os.killpg(process.pid,signal.SIGKILL)
                process.wait()
                if process.returncode not in (0, -signal.SIGTERM):
                    native_failed = True
        if process is not None: process.stdout.close()
        if errors is not None:
            errors.seek(0)
            diagnostics = errors.read(1024*1024).decode('utf-8','replace').splitlines()
            errors.close()
        reason = native_failure_reason(diagnostics) if native_failed else ''
        if native_failed or cleanup_timed_out:
            detail = 'Native helper cleanup timed out' if cleanup_timed_out else 'Native helper failed'
            detail += ' (exit %s)' % (process.returncode if process else 'unknown')
            if reason:
                detail += ': '+reason
            error = detail if not error or error.startswith('Native helper ') else error+'; '+detail
            error = compact_text(error, 1024)
            service.fault = 'Stream stopped: '+error
        api.ACTIVE = None
        if reader is not None: reader.close()
        service.last_finish = api.monotonic()
        if process is not None and any(not api.interface_idle(c) for c in service.config['cameras']):
            error = 'Camera interface did not return to idle'
            service.fault = error
        if before is not None:
            after = audit()
            for key in ('robot_pids','modules','mounts','config_hashes','dmesg_sha256'):
                if before[key] != after[key]:
                    error = (error+'; ' if error else '')+'Audit changed: '+key
                    service.fault = error
        # Retain the failed round and its error. Only a later authenticated
        # recover request may clear an isolated camera fault. Invalid frames
        # still fail this round; recovery never marks its data complete.
        recoverable = bool(reason in ('Camera source sequence gap/regression; stop',
                'Camera has no new frame for 2 seconds; stop',
                'Camera did not produce first frame after bounded startup retry; stop',
                'Invalid MJPEG frame or dimensions; stop')
            and process is not None and process.returncode == 1 and not cleanup_timed_out
            and (sampler is None or not sampler.error)
            and error == 'Native helper failed (exit 1): '+reason
            and native_rt_unchanged(diagnostics) and unchanged_for_recovery(before, after, mode))
        service.recovery_audit = after if recoverable else None
        if error is not None: stop_reason = 'stream_error'
        result = {'ok':error is None,'type':'end','error':error,'frames':counts,
            'joint_hz':joint_hz,
            'joint_samples':joint_sent, 'stop_reason':stop_reason, 'observation_mode':mode,
            'joint_skipped_ticks':sampler.skipped if sampler else 0,
            'joint_max_lateness_ns':sampler.max_lateness_ns if sampler else None,
            'native_exit':process.returncode if process else None,
            'native_failure_reason':reason,
            'native_termination':native_termination,
            'native_cleanup_timed_out':cleanup_timed_out, 'recoverable_camera_fault':recoverable,
            'native_diagnostics':diagnostics, 'audit_before':before,'audit_after':after}
        service.last_stream = dict((k,v) for k,v in result.items() if not k.startswith('audit_') and k!='native_diagnostics')
        service.last_stream['native_diagnostics_tail'] = diagnostic_tail(diagnostics)
        try: api.reply(sock,result)
        except (IOError,ValueError): pass
