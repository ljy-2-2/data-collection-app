#!/usr/bin/env python3
"""Read the G2 head color camera through GDK and stream it to the ROS bridge."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GDK_ROOT = PROJECT_DIR.parent / "app" / "gdk"
DEFAULT_GDK_LIB_ROOT = PROJECT_DIR.parent / "app" / "lib"
FRAME_STALL_TIMEOUT_SECONDS = 3.0
OUTPUT_FRAME_INTERVAL_SECONDS = 1.0 / 30.0


def local_robot_ip(robot_ip: str) -> str:
    """Resolve the source address Linux uses to reach the G2 without netlink."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((robot_ip, 2379))
        return str(probe.getsockname()[0])


def enum_name(value: Any) -> str:
    return str(getattr(value, "name", value)).split(".")[-1].upper()


def configure_gdk_environment(gdk_root: Path, native_lib_root: Path, robot_ip: str) -> None:
    python_lib = str(gdk_root / "lib")
    if python_lib not in sys.path:
        sys.path.insert(0, python_lib)
    previous = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = ":".join(
        part for part in (str(native_lib_root), previous) if part
    )
    os.environ.setdefault("LOCATOR_IP", local_robot_ip(robot_ip))
    os.environ.setdefault("AORTA_DISCOVERY_URI", f"http://{robot_ip}:2379")
    os.environ.setdefault("AORTA_DISPATCHER_THREAD_NUM", "6")


def frame_is_newer(timestamp_ns: int, previous_timestamp_ns: int) -> bool:
    """Use source-clock progression; the G2 wall clock may differ from the PC."""
    return timestamp_ns > previous_timestamp_ns


def wait_for_fresh_frame(camera, camera_type, timeout: float):
    deadline = time.monotonic() + timeout
    last_error = "no frame received"
    previous_timestamp = 0
    while time.monotonic() < deadline:
        try:
            frame = camera.get_latest_image(camera_type, 1000.0)
            if frame is not None:
                timestamp_ns = int(frame.timestamp_ns)
                if previous_timestamp and frame_is_newer(
                    timestamp_ns, previous_timestamp
                ):
                    return frame
                previous_timestamp = max(previous_timestamp, timestamp_ns)
                last_error = "latest frame timestamp is not advancing"
        except RuntimeError as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"G2 head color camera has no live frame: {last_error}")


def frame_metadata(frame, intrinsic) -> dict[str, Any]:
    return {
        "timestamp_ns": int(frame.timestamp_ns),
        "width": int(frame.width),
        "height": int(frame.height),
        "encoding": enum_name(frame.encoding),
        "color_format": enum_name(frame.color_format),
        "intrinsic": [float(value) for value in intrinsic.intrinsic],
        "distortion": [float(value) for value in intrinsic.distortion],
    }


def run(args: argparse.Namespace) -> int:
    configure_gdk_environment(args.gdk_root, args.native_lib_root, args.robot_ip)
    package_root = PROJECT_DIR / "galaxis_bringup"
    sys.path.insert(0, str(package_root))
    from galaxis_bringup.g2_camera_protocol import send_frame

    import agibot_gdk

    if agibot_gdk.gdk_init() != agibot_gdk.GDKRes.kSuccess:
        raise RuntimeError("GDK initialization failed")
    camera = agibot_gdk.Camera()
    camera_type = agibot_gdk.CameraType.kHeadColor
    stop_requested = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        time.sleep(2.0)
        first_frame = wait_for_fresh_frame(camera, camera_type, args.startup_timeout)
        intrinsic = camera.get_camera_intrinsic(camera_type)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)
        server.settimeout(1.0)
        print(
            f"G2 center camera ready: input {first_frame.width}x{first_frame.height} "
            f"{enum_name(first_frame.encoding)}, ROS output 640x480@30 on "
            f"{args.host}:{args.port}",
            flush=True,
        )
        pending_frame = first_frame
        last_sent_timestamp = 0
        last_sent_at = 0.0
        last_fresh_at = time.monotonic()
        while not stop_requested:
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(2.0)
                last_fresh_at = time.monotonic()
                while not stop_requested:
                    try:
                        frame = pending_frame or camera.get_latest_image(camera_type, 1000.0)
                        pending_frame = None
                        timestamp_ns = int(frame.timestamp_ns)
                        if (
                            not frame_is_newer(timestamp_ns, last_sent_timestamp)
                        ):
                            if (
                                time.monotonic() - last_fresh_at
                                > FRAME_STALL_TIMEOUT_SECONDS
                            ):
                                raise RuntimeError(
                                    "G2 head color camera stopped publishing fresh frames"
                                )
                            time.sleep(0.02)
                            continue
                        now = time.monotonic()
                        remaining = OUTPUT_FRAME_INTERVAL_SECONDS - (now - last_sent_at)
                        if remaining > 0:
                            time.sleep(remaining)
                        send_frame(
                            connection,
                            frame_metadata(frame, intrinsic),
                            bytes(frame.data),
                        )
                        last_sent_timestamp = timestamp_ns
                        last_sent_at = time.monotonic()
                        last_fresh_at = last_sent_at
                    except (BrokenPipeError, ConnectionError, socket.timeout):
                        break
                    except RuntimeError as exc:
                        if (
                            time.monotonic() - last_fresh_at
                            > FRAME_STALL_TIMEOUT_SECONDS
                        ):
                            raise RuntimeError(
                                "G2 head color camera stopped publishing fresh frames"
                            ) from exc
                        print(f"G2 camera frame unavailable: {exc}", file=sys.stderr, flush=True)
                        time.sleep(0.1)
        server.close()
    finally:
        camera.close_camera()
        if hasattr(agibot_gdk, "gdk_release"):
            agibot_gdk.gdk_release()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=64002)
    parser.add_argument("--robot-ip", default="10.42.1.101")
    parser.add_argument("--startup-timeout", type=float, default=15.0)
    parser.add_argument("--gdk-root", type=Path, default=DEFAULT_GDK_ROOT)
    parser.add_argument("--native-lib-root", type=Path, default=DEFAULT_GDK_LIB_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
