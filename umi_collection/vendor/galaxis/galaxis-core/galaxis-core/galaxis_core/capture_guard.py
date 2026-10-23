"""Recorder parent/desktop lease watchdog, with graceful MCAP flush."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .capture_recovery import interrupt_capture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--parent-pid', type=int, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    value = json.loads(args.journal.read_text())
    lease_file = value.get('lease_file')
    local_raw = value.get('metadata', {}).get('recording_format') == 'umi-mcap-h264-v1'
    stopped = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.append(True))
    parent = os.getpid()
    def child_setup():
        # If this guard is SIGKILLed, the actual rosbag writer still flushes.
        if ctypes.CDLL(None).prctl(1, signal.SIGINT) != 0:
            os._exit(125)
        if os.getppid() != parent: os.kill(os.getpid(), signal.SIGINT)
    inherited = tuple(int(fd) for fd in os.environ.get('GALAXIS_CAPTURE_FDS', '').split(',') if fd)
    child = subprocess.Popen(command, preexec_fn=child_setup, pass_fds=inherited)
    try:
        while child.poll() is None and not stopped:
            reason = None
            if os.getppid() != args.parent_pid:
                reason = 'record_manager_exited'
            elif lease_file:
                try:
                    lease = json.loads(Path(lease_file).read_text())
                    if not lease.get('available') and not local_raw:
                        reason = 'platform_connection_lost'
                    elif not 0 <= time.time()-lease['checked_at'] < 10:
                        reason = 'desktop_heartbeat_lost'
                except (OSError, ValueError, KeyError, TypeError):
                    reason = 'desktop_heartbeat_lost'
            if reason:
                interrupt_capture(args.journal, reason)
                break
            time.sleep(.2)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            try: child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.terminate()
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired: child.kill(); child.wait()
    return child.returncode


if __name__ == '__main__':
    raise SystemExit(main())
