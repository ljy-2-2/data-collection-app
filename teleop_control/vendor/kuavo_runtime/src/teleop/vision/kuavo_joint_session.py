"""Explicit PC joint video/control lifecycle. Import/construction perform no IO.

The existing formal network client remains the sole motion session owner. Video
uses its existing independent AGX camera transport. This module never starts a
robot controller, publishes ROS, discovers CAN or reads the PICO input SDK.
"""
import json
import signal
import subprocess
import sys
import select
import socket
import threading
import time
import uuid

from . import kuavo_session as video


STATUS_PORT = 63904
PROTOCOL = 'kuavo_joint_status_v1'


class StatusServer:
    """Latest-only, one-way display data on PC loopback/USB ADB, no commands."""
    def __init__(self, snapshot, *, receipt=None, port=STATUS_PORT):
        self.snapshot, self.port = snapshot, port
        self.receipt = receipt or (lambda value: None)
        self.stop = threading.Event()
        self.listener = self.thread = None

    def start(self):
        self.listener = socket.socket()
        try:
            self.listener.bind(('127.0.0.1', self.port))
            self.listener.listen(1)
            self.listener.settimeout(.2)
        except BaseException:
            self.listener.close()
            self.listener = None
            raise
        self.thread = threading.Thread(target=self._run, name='joint-display', daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(.3)
                pending = b''
                try:
                    while not self.stop.is_set():
                        raw = (json.dumps(self.snapshot(), ensure_ascii=False, allow_nan=False)+'\n').encode()
                        if len(raw) > 4096:
                            raise ValueError('bounded joint display packet required')
                        connection.sendall(raw)
                        if select.select([connection], [], [], 0)[0]:
                            chunk = connection.recv(1024)
                            if not chunk:
                                break
                            pending += chunk
                            if len(pending) > 2048:
                                raise ValueError('oversized headset receipt')
                            while b'\n' in pending:
                                line, pending = pending.split(b'\n', 1)
                                self.receipt(json.loads(line))
                        self.stop.wait(.1)
                except (OSError, ValueError):
                    pass

    def close(self):
        self.stop.set()
        if self.listener:
            self.listener.close()
        if self.thread:
            self.thread.join(timeout=1.)


class JointVideoSession:
    """Optional video beside the sole formal arm session; no video authority.

    Neither video startup, stream freshness, headset display receipts nor
    telemetry availability can stop, pause or prevent admission of controls.
    PICO input freshness is checked independently by the existing control path.
    """
    def __init__(self, control, *, fps=30, quality=90, installation_sha256='', base_integrated=False):
        if fps not in (30, 60) or type(quality) is not int or not 1 <= quality <= 100:
            raise ValueError('invalid joint video profile')
        self.control, self.fps, self.quality = control, fps, quality
        self.installation_sha256 = installation_sha256
        if type(base_integrated) is not bool:
            raise ValueError('base integration display flag must be a boolean')
        self.base_integrated = base_integrated
        self.process = self.log = self.monitor = self.display = None
        self.stop = threading.Event()
        self.warning = None
        self._video_ready = False
        self._sequence = 0
        self._display_sequence = 0
        self._display_advanced_ns = None
        self._tracking_enabled = False
        self._lock = threading.Lock()

    def snapshot(self):
        state = self.control.status()
        base_state = state.get('base_state', 'unknown')
        if self.base_integrated and base_state == 'not_integrated':
            base_state = 'starting'
        with self._lock:
            self._sequence += 1
            return dict(protocol=PROTOCOL, session_id=state['session_id'], sequence=self._sequence,
                phase=state['phase'], arm_state=state['arm_state'],
                left_gripper=state['left_gripper'], right_gripper=state['right_gripper'],
                return_state=state['return_state'], video_ready=self._video_ready,
                robot_report_fresh=0 <= time.monotonic_ns()-state.get('robot_report_monotonic_ns', 0) < 2_000_000_000,
                installation=self.installation_sha256[:12],
                base_state=(base_state if self.base_integrated else 'not_integrated'),
                reason=str(self.warning or state.get('error') or '')[:256])

    def _receipt(self, packet):
        if (not isinstance(packet, dict) or set(packet) != {
                'protocol', 'session_id', 'video_display_sequence', 'tracking_enabled'}
                or packet['protocol'] != PROTOCOL or packet['session_id'] != self.control.status()['session_id']
                or type(packet['video_display_sequence']) is not int or packet['video_display_sequence'] < 0
                or type(packet['tracking_enabled']) is not bool):
            raise ValueError('invalid headset receipt')
        with self._lock:
            self._tracking_enabled = packet['tracking_enabled']
            if packet['video_display_sequence'] > self._display_sequence:
                self._display_sequence = packet['video_display_sequence']
                self._display_advanced_ns = time.monotonic_ns()

    def _display_healthy(self):
        with self._lock:
            return (self._display_advanced_ns is not None and
                    0 <= time.monotonic_ns()-self._display_advanced_ns < 1_500_000_000)

    def _warn(self, reason):
        with self._lock:
            changed = reason != self.warning
            self.warning = reason
        if changed and reason:
            print('VR 视频提示：'+str(reason)+'；控制会话独立运行。', flush=True)

    def __enter__(self):
        try:
            self.start()
            return self
        except BaseException:
            self.close()
            raise

    def start(self):
        # All video setup is optional. In particular a dead AGX/SSH must not
        # delay control admission: its video-only producer runs in a child.
        try:
            self.display = StatusServer(self.snapshot, receipt=self._receipt)
            self.display.start()
        except (OSError, ValueError, RuntimeError) as exc:
            self._warn('头显状态通道不可用：'+str(exc))
        try:
            serial = video.prepare_pico(joint=True)
            video.run(video.pico_open_command(serial, uuid.uuid4().hex,
                mode='teleop_video', session_id=self.control.status()['session_id']), timeout=30)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self._warn(str(exc))
        try:
            logdir = video.RUNTIME/'artifacts/kuavo-video'
            logdir.mkdir(parents=True, exist_ok=True)
            self.log = (logdir/('joint-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.log')).open('x')
            bootstrap = ('import sys; sys.path.insert(0,sys.argv.pop(1)); '
                         'from teleop.vision.kuavo_session import main; raise SystemExit(main())')
            self.process = subprocess.Popen([sys.executable, '-I', '-B', '-c', bootstrap,
                str(video.RUNTIME/'src'), '--no-pico', '--non-interactive',
                '--fps', str(self.fps), '--quality', str(self.quality)],
                stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self._warn('视频生产者未启动：'+str(exc))
        self.monitor = threading.Thread(target=self._watch, name='joint-video-watch', daemon=True)
        try:
            self.monitor.start()
        except RuntimeError as exc:
            self.monitor = None
            self._warn('视频提示线程未启动：'+str(exc))
        print('联合入口已准备：视频独立运行；Y 回位，再按 Y 取消。', flush=True)

    def _producer_error(self):
        """Report an early producer failure instead of hiding it as black video."""
        if self.process is None or self.process.poll() in (None, 0):
            return None
        if self.log:
            try:
                with open(self.log.name, 'rb') as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell()-8192))
                    tail = stream.read(8192).decode('utf-8', errors='replace')
                for line in reversed(tail.splitlines()):
                    marker = 'KUAVO_VIDEO_SESSION_ERROR reason='
                    if line.startswith(marker):
                        return line[len(marker):].removesuffix(' robot_commands_sent=False')[:512]
            except OSError:
                pass
        return '视频程序已退出，请查看本次视频日志'

    def _check_video(self):
        try:
            video.probe_stream('127.0.0.1', video.VIDEO_PORT, timeout=.4)
            with self._lock:
                self._video_ready = True
            self._warn(None if self._display_healthy() else '头显尚无新画面或显示回执')
        except (OSError, ValueError, RuntimeError) as exc:
            with self._lock:
                self._video_ready = False
            self._warn(self._producer_error() or '没有连续新画面：'+str(exc))

    def _watch(self):
        while not self.stop.wait(.5):
            self._check_video()

    def close(self):
        self.stop.set()
        with video.cleanup_signals():
            if self.monitor:
                self.monitor.join(timeout=2.)
            if self.display:
                self.display.close()
            try:
                if self.process and self.process.poll() is None:
                    self.process.send_signal(signal.SIGINT)
                    try:
                        self.process.wait(timeout=25.)
                    except subprocess.TimeoutExpired:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=3.)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                            self.process.wait(timeout=2.)
            finally:
                if self.log:
                    self.log.close()

    def __exit__(self, *_):
        self.close()
