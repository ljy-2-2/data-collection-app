"""Observe validated VideoHub frames on the PC without saving or displaying them."""

from __future__ import annotations

import argparse
import socket
import sys
import time

from teleop.vision import read_frame


def observe(*, host: str, port: int, frame_limit: int, timeout_s: float) -> int:
    frames = 0
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as connection:
            connection.settimeout(timeout_s)
            while frames < frame_limit:
                frame = read_frame(connection)
                if frame is None:
                    raise RuntimeError("VideoHub closed before the requested frames arrived")
                age_ms = (time.monotonic_ns() - frame.pc_received_monotonic_ns) / 1e6
                print(
                    f"VIDEO_FRAME sequence={frame.sequence_id} camera={frame.camera_name} "
                    f"size={frame.width}x{frame.height} bytes={len(frame.payload)} "
                    f"age_ms={age_ms:.1f} physical_robot_command_sent=False",
                    flush=True,
                )
                frames += 1
    except Exception as error:
        print(
            f"VIDEO_OBSERVER_ERROR error={error!r} frames={frames} "
            "physical_robot_command_sent=False",
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(
        f"VIDEO_OBSERVER_SUMMARY frames={frames} physical_robot_command_sent=False",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read and validate frames from the host VideoHub."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=63902)
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")
    if args.timeout <= 0.0:
        parser.error("--timeout must be positive")
    return observe(
        host=args.host,
        port=args.port,
        frame_limit=args.frames,
        timeout_s=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
