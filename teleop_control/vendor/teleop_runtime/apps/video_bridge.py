"""Run the host-side Unix-to-TCP latest-frame video hub."""

from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys
import threading
import time

from teleop.vision import VideoHub


def run(args: argparse.Namespace) -> int:
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    bridge = VideoHub(
        unix_path=args.socket_path,
        tcp_host=args.host,
        tcp_port=args.port,
        on_event=lambda message: print(
            f"VIDEO_BRIDGE_EVENT {message} physical_robot_command_sent=False",
            flush=True,
        ),
    )
    try:
        bridge.start()
        host, port = bridge.tcp_address
        print(
            f"VIDEO_BRIDGE_READY unix_socket={args.socket_path} tcp={host}:{port} "
            "queue=latest-only viewers=multi physical_robot_command_sent=False",
            flush=True,
        )
        deadline = None if args.duration is None else time.monotonic() + args.duration
        while not stop.wait(0.25):
            if deadline is not None and time.monotonic() >= deadline:
                break
    except Exception as error:
        print(
            f"VIDEO_BRIDGE_ERROR error={error!r} physical_robot_command_sent=False",
            file=sys.stderr,
            flush=True,
        )
        return 2
    finally:
        bridge.close()
    print(
        f"VIDEO_BRIDGE_SUMMARY frames={bridge.mailbox.published_count} "
        f"overwritten={bridge.mailbox.overwritten_count} "
        "physical_robot_command_sent=False",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Expose newest robot JPEG frame on a dedicated TCP port."
    )
    parser.add_argument("--socket-path", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=63902)
    parser.add_argument("--duration", type=float, default=None)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
