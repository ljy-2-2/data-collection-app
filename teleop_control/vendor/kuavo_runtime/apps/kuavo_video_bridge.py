"""Host video-only entry. User starts this explicitly; no camera/controller launch."""

import argparse
from collections import deque
import math
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import threading
import time

from teleop.vision import VideoHub
from teleop.vision.pipe import read_pipe_frame
from teleop.vision.protocol import ProtocolError


class ReceptionStats:
    """PC receive statistics; no cross-machine timestamp subtraction."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frames = 0
        self._bytes = 0
        self._first = None
        self._last = None
        self._max_gap = 0.0
        self._width = self._height = 0
        self._recent = deque()

    def record(self, frame, now):
        with self._lock:
            if self._last is not None:
                self._max_gap = max(self._max_gap, now - self._last)
            if self._first is None:
                self._first = now
            self._last = now
            self._frames += 1
            self._bytes += len(frame.payload)
            self._width, self._height = frame.width, frame.height
            self._recent.append(now)
            while self._recent and self._recent[0] <= now - 5.0:
                self._recent.popleft()

    def snapshot(self, now):
        with self._lock:
            span = 0.0 if self._first is None else now - self._first
            cadence = 0.0 if self._first is None else self._last - self._first
            while self._recent and self._recent[0] <= now - 5.0:
                self._recent.popleft()
            recent_span = min(5.0, span)
            recent_frames = len(self._recent) - (1 if self._first is not None and span < 5.0 else 0)
            return dict(frames=self._frames, width=self._width, height=self._height,
                        receive_fps=(self._frames - 1) / cadence if cadence > 0 else 0.0,
                        recent_receive_fps=max(0, recent_frames) / recent_span if recent_span > 0 else 0.0,
                        jpeg_mbit_s=self._bytes * 8 / span / 1e6 if span > 0 else 0.0,
                        max_receive_gap_ms=self._max_gap * 1000,
                        last_frame_age_ms=(now - self._last) * 1000 if self._last is not None else 0.0)


def format_stats(stats):
    return (f"frames={stats['frames']} width={stats['width']} height={stats['height']} "
            f"receive_fps={stats['receive_fps']:.2f} recent_receive_fps={stats['recent_receive_fps']:.2f} "
            f"jpeg_mbit_s={stats['jpeg_mbit_s']:.2f} "
            f"max_receive_gap_ms={stats['max_receive_gap_ms']:.1f} "
            f"last_frame_age_ms={stats['last_frame_age_ms']:.1f}")


def build_ssh_command(args):
    remote_args = ['python3', '-u', '-', '--topic', args.topic, '--fps', str(args.fps),
                   '--quality', str(args.quality), '--duration', str(args.duration)]
    setup = 'source ' + shlex.quote(args.ros_setup) + ' >&2 && '
    if args.ros_master_uri:
        setup += 'export ROS_MASTER_URI=' + shlex.quote(args.ros_master_uri) + ' && '
    if getattr(args, 'ros_ip', ''):
        setup += 'export ROS_IP=' + shlex.quote(args.ros_ip) + ' && '
    # Explicit ROS setup replaces login profiles, whose stdout can corrupt GVID.
    remote = 'bash -c ' + shlex.quote(setup + 'exec ' + shlex.join(remote_args))
    ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
           '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=2']
    if args.ssh_socket:
        ssh += ['-S', args.ssh_socket]
    if getattr(args, 'require_ssh_master', False):
        # Existing Master bypasses this. If it disappears, fail instead of routing
        # a new connection through the host Wi-Fi/VPN or a configured jump host.
        ssh += ['-F', '/dev/null', '-o', 'ProxyCommand=false']
    ssh += [args.ssh_target, remote]
    if not args.ssh_direct:
        network = Path(__file__).resolve().parents[1] / 'scripts' / 'kuavo-network'
        return ['sudo', '-n', str(network), 'run', '--'] + ssh
    return ssh


def main():
    parser = argparse.ArgumentParser(description='Kuavo read-only camera to PC/PICO video bridge')
    parser.add_argument('--ssh-target', default='lab@192.168.26.1')
    parser.add_argument('--ssh-socket', default='/tmp/kuavo5-ssh-master')
    parser.add_argument('--ssh-direct', action='store_true', help='SSH without kuavons, for an already reachable ROS PC')
    parser.add_argument('--require-ssh-master', action='store_true',
                        help='Fail if the specified Master is absent; never open a fallback SSH connection')
    parser.add_argument('--ros-setup', default='/opt/ros/noetic/setup.bash')
    parser.add_argument('--ros-master-uri', default='')
    parser.add_argument('--ros-ip', default='', help='ROS_IP of the SSH camera computer, e.g. AGX 192.168.26.12')
    parser.add_argument('--topic', default='/camera/color/image_raw')
    parser.add_argument('--fps', type=float, default=30)
    parser.add_argument('--quality', type=int, default=80)
    parser.add_argument('--duration', type=float, default=300, help='Seconds; 0 runs until Ctrl-C')
    parser.add_argument('--startup-timeout', type=float, default=10)
    parser.add_argument('--frame-timeout', type=float, default=5)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=63903)
    args = parser.parse_args()
    if (not 1 <= args.port <= 65535 or not math.isfinite(args.duration) or args.duration < 0
            or not math.isfinite(args.fps) or not 1 <= args.fps <= 60 or not 1 <= args.quality <= 100
            or not math.isfinite(args.startup_timeout) or args.startup_timeout <= 0
            or not math.isfinite(args.frame_timeout) or args.frame_timeout <= 0):
        parser.error('invalid port, duration, fps or quality')
    if not args.ssh_target or args.ssh_target.startswith('-'):
        parser.error('invalid SSH target')
    if args.require_ssh_master and not args.ssh_socket:
        parser.error('--require-ssh-master needs --ssh-socket')

    process = None
    hub = None
    stop = threading.Event()
    requested_stop = threading.Event()
    stats = ReceptionStats()
    frames = [0]
    last_complete_frame = [None]
    errors = []

    def request_stop(_signal, _frame):
        requested_stop.set()
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        if args.require_ssh_master:
            check = subprocess.run(['ssh', '-F', '/dev/null', '-S', args.ssh_socket,
                                    '-O', 'check', args.ssh_target], timeout=5)
            if check.returncode != 0:
                raise RuntimeError('SSH master is unavailable; run apps/connect_kuavo_video_ssh.py first')
        with tempfile.TemporaryDirectory(prefix='kuavo-video-') as directory:
            hub = VideoHub(unix_path=Path(directory) / 'producer.sock', tcp_host=args.host,
                           tcp_port=args.port, max_frame_age_s=1.0)
            hub.start()
            # Ctrl-C belongs to this entry, not both this entry and the SSH child.
            # Terminate only the reader we created, never the reused Master.
            process = subprocess.Popen(build_ssh_command(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       start_new_session=True)
            worker = Path(__file__).with_name('kuavo_camera_worker.py').read_bytes()
            process.stdin.write(worker)
            process.stdin.close()

            def receive():
                try:
                    while not stop.is_set():
                        frame = read_pipe_frame(process.stdout, expected_camera='kuavo5w-head')
                        if frame is None:
                            break
                        hub.mailbox.publish(frame)
                        last_complete_frame[0] = time.monotonic()
                        stats.record(frame, last_complete_frame[0])
                        frames[0] += 1
                except Exception as error:
                    # Only discard incomplete wire reads caused by an explicit stop.
                    # A genuine parse failure/timeout before that remains an error.
                    if not (requested_stop.is_set() and isinstance(error, ProtocolError)
                            and str(error).startswith('video stream ended after')):
                        errors.append(str(error))
                finally:
                    stop.set()

            reader = threading.Thread(target=receive, daemon=True)
            reader.start()
            print(f'KUAVO_VIDEO_READY tcp={args.host}:{args.port} source=kuavo5w-head topic={args.topic} '
                  f'reader={args.ssh_target} requested_fps={args.fps:g} quality={args.quality} '
                  'physical_robot_command_sent=False', flush=True)
            startup_deadline = time.monotonic() + args.startup_timeout
            while frames[0] == 0 and not stop.wait(0.05):
                if time.monotonic() >= startup_deadline:
                    errors.append('no initial camera frame within startup timeout')
                    stop.set()
            running_deadline = time.monotonic() + args.duration + 10 if args.duration else math.inf
            next_stats = time.monotonic() + 5
            while not stop.wait(0.05):
                now = time.monotonic()
                if now >= next_stats:
                    print('KUAVO_VIDEO_STATS ' + format_stats(stats.snapshot(now))
                          + ' physical_robot_command_sent=False', flush=True)
                    next_stats = now + 5
                if last_complete_frame[0] is not None and now - last_complete_frame[0] > args.frame_timeout:
                    errors.append('no complete camera frame within frame timeout')
                    break
                if now >= running_deadline:
                    errors.append('camera worker exceeded its duration')
                    break
            stop.set()
            if process.poll() is None:
                process.terminate()  # only the SSH subprocess created above; no robot control calls.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            reader.join(timeout=2)
            hub.close()
            hub = None
            if errors:
                raise RuntimeError('; '.join(errors))
            if frames[0] == 0 and not requested_stop.is_set():
                raise RuntimeError('no camera frames received; check ROS topic and SSH environment')
            if (process.returncode not in (0, -signal.SIGTERM)
                    and not (requested_stop.is_set() and process.returncode == -signal.SIGINT)):
                raise RuntimeError(f'camera SSH exited with {process.returncode}')
    except Exception as error:
        print(f'KUAVO_VIDEO_ERROR reason={error} physical_robot_command_sent=False', flush=True)
        return 2
    finally:
        stop.set()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if hub is not None:
            hub.close()
        if process is not None:
            for name in ('stdin', 'stdout'):
                close = getattr(getattr(process, name, None), 'close', None)
                if close is not None:
                    close()
        print('KUAVO_VIDEO_SUMMARY ' + format_stats(stats.snapshot(time.monotonic()))
              + f' stop_requested={requested_stop.is_set()} physical_robot_command_sent=False', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
