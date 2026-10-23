"""One-command USB PICO / wired AGX video. No robot-control imports or calls."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

from .protocol import read_frame

RUNTIME = Path(__file__).resolve().parents[3]
TARGET = 'leju_kuavo@192.168.26.12'
MASTER = '/tmp/kuavo5-agx-video-ssh-master'
SETUP = '/home/leju_kuavo/kuavo_ros_application/devel/setup.bash'
PACKAGE = 'com.galaxis.vrteleop'
VIDEO_PORT = 63903


@contextmanager
def cleanup_signals():
    """Coalesce repeated Ctrl-C/TERM while all owned children are being reaped."""
    handlers = {number: signal.signal(number, signal.SIG_IGN)
                for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)


def choose_socket(requested, directory, *, master_alive):
    if master_alive or not os.path.lexists(requested):
        return str(requested)
    # Preserve stale sockets and arbitrary files. Use a fresh video-only name.
    return str(Path(directory) / ('agx-' + uuid.uuid4().hex[:10]))


def pico_open_command(serial, request):
    return ['adb', '-s', serial, 'shell', 'am', 'start', '-W', '--activity-single-top',
            '-n', PACKAGE + '/com.unity3d.player.UnityPlayerActivity',
            '--es', 'galaxis_video_source', 'kuavo5w-head',
            '--es', 'galaxis_video_request', request]


def probe_stream(host, port, timeout=1.5):
    """A bound socket or repeated cached image is not evidence of live video."""
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        last = None
        for _ in range(3):
            frame = read_frame(connection)
            if frame is None or frame.camera_name != 'kuavo5w-head':
                raise RuntimeError('port is not serving the Leju head camera')
            age = (time.monotonic_ns() - frame.pc_received_monotonic_ns) / 1e9
            identity = (frame.sequence_id, frame.source_timestamp_ns)
            if not 0 <= age < 1.0 or (last is not None and
                    (identity[0] <= last[0] or identity[1] <= last[1])):
                raise RuntimeError('video frames are stale or duplicated')
            last = identity
        return frame


def port_in_use():
    try:
        with socket.create_connection(('127.0.0.1', VIDEO_PORT), timeout=0.3):
            return True
    except ConnectionRefusedError:
        return False


def private_directory():
    directory = Path('/tmp') / ('galaxis-video-' + str(os.getuid()))
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('video runtime directory must be private and owned by this user')
    return directory


def master_alive(path):
    return subprocess.run(['ssh', '-F', '/dev/null', '-S', str(path), '-O', 'check', TARGET],
                          capture_output=True, timeout=5).returncode == 0


def run(command, *, capture=False, timeout=30):
    result = subprocess.run(command, cwd=RUNTIME, close_fds=True, text=True,
                            capture_output=capture, timeout=timeout)
    if result.returncode:
        detail = (result.stderr or result.stdout or '').strip() if capture else ''
        raise RuntimeError(f'{Path(command[0]).name} failed ({result.returncode}): {detail}')
    return result.stdout if capture else ''


def prepare_pico():
    output = run(['adb', 'devices', '-l'], capture=True)
    candidates = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == 'device' and ('product:sparrow' in line or 'model:A9210' in line):
            candidates.append(fields[0])
    requested = os.environ.get('PICO_SERIAL')
    if requested:
        candidates = [value for value in candidates if value == requested]
    if len(candidates) != 1:
        raise RuntimeError('请连接一台 PICO、允许 USB 调试；多台时设置 PICO_SERIAL')
    serial = candidates[0]
    package = run(['adb', '-s', serial, 'shell', 'dumpsys', 'package', PACKAGE], capture=True)
    version = re.search(r'versionCode=(\d+)', package)
    if not version or int(version.group(1)) < 6:
        raise RuntimeError('纯视频自动打开需要 Galaxis 1.1.12/versionCode 6 或更新版，请先安装项目新 APK')
    run(['adb', '-s', serial, 'reverse', 'tcp:63903', 'tcp:63903'])
    return serial


def ssh_command(master, program):
    remote = ('source ' + shlex.quote(SETUP) + ' >&2 && '
              'export ROS_MASTER_URI=http://192.168.26.1:11311 ROS_IP=192.168.26.12 && '
              'exec ' + program)
    return ['ssh', '-F', '/dev/null', '-T', '-S', master, '-o', 'BatchMode=yes',
            '-o', 'ProxyCommand=false', '-o', 'ServerAliveInterval=2',
            '-o', 'ServerAliveCountMax=2', TARGET, 'bash -c ' + shlex.quote(remote)]


class Session:
    def __init__(self, log):
        self.log = log
        self.children = []
        self.threads = []
        self.output_lock = threading.Lock()

    def say(self, message):
        with self.output_lock:
            print(message, flush=True)
            self.log.write(message + '\n')
            self.log.flush()

    def spawn(self, command, *, source=None, ready=None):
        process = subprocess.Popen(command, cwd=RUNTIME, stdin=subprocess.PIPE if source else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True, close_fds=True)
        self.children.append(process)

        def collect():
            try:
                for line in process.stdout:
                    if line.startswith('KUAVO_CAMERA_SESSION_ALIVE '):
                        continue
                    self.say(line.rstrip())
                    if ready is not None and line.startswith('KUAVO_CAMERA_SESSION_READY '):
                        ready.set()
            finally:
                process.stdout.close()

        thread = threading.Thread(target=collect, daemon=True)
        thread.start()
        self.threads.append(thread)
        if source:
            try:
                process.stdin.write(source)
            finally:
                process.stdin.close()
        return process

    def connect(self, directory):
        alive = master_alive(MASTER)
        master = choose_socket(MASTER, directory, master_alive=alive)
        if not alive:
            self.say('VIDEO_STEP network：准备乐聚有线网络，可能提示电脑密码')
            run(['sudo', '-v'], timeout=None)
            run(['sudo', '-n', str(RUNTIME / 'scripts/kuavo-network'), 'up'], timeout=45)
        self.say('VIDEO_STEP ssh：检查或建立 AGX 专用连接，可能提示 AGX 密码')
        run([sys.executable, str(RUNTIME / 'apps/connect_kuavo_video_ssh.py'),
             '--ssh-socket', master], timeout=None)
        return master

    def camera(self, master):
        self.say('VIDEO_STEP camera：检查新帧；相机缺失时只启动专用彩色采集')
        ready = threading.Event()
        process = self.spawn(ssh_command(master, 'python3 -u -'),
                             source=(RUNTIME / 'apps/kuavo_camera_session.py').read_text(), ready=ready)
        deadline = time.monotonic() + 40
        while not ready.wait(0.1):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('头部相机未就绪；具体原因见上面的 KUAVO_CAMERA_SESSION_ERROR')
        return process

    def bridge(self, master, args):
        self.say('VIDEO_STEP bridge：启动 63903 视频服务并等待连续有效帧')
        command = [sys.executable, '-u', str(RUNTIME / 'apps/kuavo_video_bridge.py'),
                   '--ssh-direct', '--require-ssh-master', '--ssh-target', TARGET,
                   '--ssh-socket', master, '--ros-setup', SETUP,
                   '--ros-master-uri', 'http://192.168.26.1:11311', '--ros-ip', '192.168.26.12',
                   '--port', str(VIDEO_PORT),
                   '--duration', str(args.duration), '--fps', str(args.fps), '--quality', str(args.quality)]
        process = self.spawn(command)
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            try:
                return process, probe_stream('127.0.0.1', VIDEO_PORT)
            except (OSError, ValueError, RuntimeError):
                time.sleep(0.2)
        raise RuntimeError('视频桥未收到连续新帧，检查本次日志')

    def close(self):
        # Only terminate subprocesses created by this invocation. Never use pgrep/killall/rosnode kill.
        for process in reversed(self.children):
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
        for thread in self.threads:
            thread.join(timeout=2)


def open_pico(serial):
    if serial:
        run(pico_open_command(serial, uuid.uuid4().hex), timeout=30)


def main():
    parser = argparse.ArgumentParser(description='乐聚头部视频一键启动；仅相机，绝不启动机器人运动')
    parser.add_argument('--fps', type=int, choices=(30, 60), default=60, help='视频桥发送上限')
    parser.add_argument('--quality', type=int, default=90)
    parser.add_argument('--duration', type=float, default=0, help='秒；0 持续运行到 Ctrl-C')
    parser.add_argument('--no-pico', action='store_true', help='只启动 PC 视频，不检查或打开头显')
    parser.add_argument('--status', action='store_true', help='只读检查63903新帧，不启动任何服务')
    args = parser.parse_args()
    import math
    if not 1 <= args.quality <= 100 or not math.isfinite(args.duration) or args.duration < 0:
        parser.error('invalid quality or duration')
    if args.status:
        try:
            frame = probe_stream('127.0.0.1', VIDEO_PORT)
            print(f'KUAVO_VIDEO_RUNNING size={frame.width}x{frame.height} camera={frame.camera_name}')
            return 0
        except (OSError, ValueError, RuntimeError) as error:
            print(f'KUAVO_VIDEO_NOT_READY reason={error}')
            return 2
    lock_fd = None
    session = None
    log = None
    def request_stop(*_):
        raise KeyboardInterrupt
    old_term = signal.signal(signal.SIGTERM, request_stop)
    try:
        directory = private_directory()
        lock_fd = os.open(directory / 'session.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(lock_fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise RuntimeError('invalid video session lock')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            frame = probe_stream('127.0.0.1', VIDEO_PORT)
            open_pico(None if args.no_pico else prepare_pico())
            print(f'KUAVO_VIDEO_REUSED size={frame.width}x{frame.height} existing_session_untouched=True')
            return 0
        serial = None if args.no_pico else prepare_pico()
        if port_in_use():
            frame = probe_stream('127.0.0.1', VIDEO_PORT)
            open_pico(serial)
            print(f'KUAVO_VIDEO_REUSED size={frame.width}x{frame.height} existing_session_untouched=True')
            return 0
        logdir = RUNTIME / 'artifacts/kuavo-video'
        logdir.mkdir(parents=True, exist_ok=True)
        logpath = logdir / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.log')
        log = logpath.open('x', encoding='utf-8')
        session = Session(log)
        session.say(f'KUAVO_VIDEO_SESSION log={logpath} robot_commands_sent=False')
        master = session.connect(directory)
        camera = session.camera(master)
        bridge, frame = session.bridge(master, args)
        open_pico(serial)
        session.say(f'KUAVO_VIDEO_SESSION_READY size={frame.width}x{frame.height} '
                    f'fps_limit={args.fps} jpeg_quality={args.quality} pico_open_requested={bool(serial)} '
                    'robot_commands_sent=False；保持终端，Ctrl-C退出')
        while bridge.poll() is None:
            if camera.poll() not in (None, 0):
                raise RuntimeError('相机会话退出，请看日志并重新执行同一命令')
            time.sleep(0.2)
        if bridge.returncode:
            raise RuntimeError('视频桥已退出，请看日志并重新执行同一命令')
        return 0
    except KeyboardInterrupt:
        print('KUAVO_VIDEO_SESSION_STOP requested=True robot_commands_sent=False', flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        message = f'KUAVO_VIDEO_SESSION_ERROR reason={error} robot_commands_sent=False'
        if session:
            session.say(message)
        else:
            print(message, flush=True)
        return 2
    finally:
        with cleanup_signals():
            try:
                if session:
                    session.close()
            finally:
                if log:
                    log.close()
                if lock_fd is not None:
                    os.close(lock_fd)
        signal.signal(signal.SIGTERM, old_term)
