#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess

PR_SET_PDEATHSIG = 1


def install_parent_death_signal(parent_pid: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGINT, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    # Close the small race where the parent exits before prctl is installed.
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGINT)


def signal_process_group(process: subprocess.Popen, signal_number: signal.Signals) -> None:
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        pass


def supervise(command: list[str]) -> int:
    # Keep the robot runner and every descendant in their own group so this guard
    # survives long enough to escalate and reap the group.
    child = subprocess.Popen(command, start_new_session=True)
    try:
        return child.wait()
    except KeyboardInterrupt:
        # PDEATHSIG targets this guard. Forward it to every runner descendant in
        # the inherited process group, including send.py and the collector.
        signal_process_group(child, signal.SIGINT)
        try:
            # The collector may still be normalizing the just-finished Episode.
            # Its SIGINT handler requests a graceful drain and ready-marker write.
            return child.wait(timeout=75.0)
        except subprocess.TimeoutExpired:
            signal_process_group(child, signal.SIGTERM)
            try:
                return child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                signal_process_group(child, signal.SIGKILL)
                return child.wait(timeout=2.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Terminate a command when teleop-control exits")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a child command is required")
    install_parent_death_signal(args.parent_pid)
    return supervise(command)


if __name__ == "__main__":
    raise SystemExit(main())
