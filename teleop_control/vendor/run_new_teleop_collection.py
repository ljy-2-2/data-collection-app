#!/usr/bin/env python3
"""Run the vendored teleop-runtime control path with platform collection."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "teleop_runtime"
DUAL_ARM_APP = RUNTIME / "apps" / "pico_g2_dual_arm_live.py"


def stop_child(child: subprocess.Popen | None, *, timeout: float = 5.0) -> None:
    if child is None or child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2.0)


def stop_collector(socket_path: Path) -> None:
    import socket

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(socket_path))
            sock.sendall(b'{"type":"shutdown"}\n')
    except OSError:
        pass


def _collector_config(source: Path, extra_cameras: list[str], temp_dir: Path) -> Path:
    if not extra_cameras:
        return source
    config = yaml.safe_load(source.read_text()) or {}
    cameras = config.setdefault("cameras", [])
    existing = {item if isinstance(item, str) else item.get("name") for item in cameras}
    for camera in extra_cameras:
        if camera not in existing:
            cameras.append({"name": camera, "essential": False, "staleness_budget_ms": 50})
    destination = temp_dir / "collection.yaml"
    destination.write_text(yaml.safe_dump(config, sort_keys=False))
    return destination


def _wait_for_collector(socket_path: Path, collector: subprocess.Popen) -> None:
    deadline = time.monotonic() + 10.0
    while not socket_path.exists() and collector.poll() is None:
        if time.monotonic() >= deadline:
            raise RuntimeError("collector IPC did not start")
        time.sleep(0.1)
    if collector.poll() is not None:
        raise RuntimeError(f"collector exited early ({collector.returncode})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-folder", required=True)
    parser.add_argument("--out", default="~/agibot_episodes/dual_arm")
    parser.add_argument("--hz", type=float, default=100.0)
    parser.add_argument("--scale", type=float, default=0.5)
    parser.add_argument("--max-joint-vel", type=float, default=0.5)
    parser.add_argument(
        "--extra-camera",
        action="append",
        default=[],
        choices=("head_depth", "hand_left_depth", "hand_right_depth"),
    )
    parser.add_argument("--image-width", type=int, default=640)
    parser.add_argument("--image-height", type=int, default=480)
    parser.add_argument("--collection-project", default=str(HERE / "gdk_data_collection"))
    parser.add_argument("--config", default=str(HERE / "config" / "dual_arm.yaml"))
    parser.add_argument("--recording-status", type=Path)
    # Kept for command-line compatibility with the previous platform runner.
    parser.add_argument("--gdk-app-root")
    parser.add_argument("--yes", action="store_true")
    return parser


def _worker_command(args: argparse.Namespace, socket_path: Path) -> list[str]:
    """Build the live worker command, including both operator parameters."""
    return [
        sys.executable,
        str(DUAL_ARM_APP),
        "worker",
        "--settle-seconds",
        "2",
        "--control-hz",
        str(args.hz),
        "--max-joint-velocity-rad-s",
        str(args.max_joint_vel),
        "--retarget-scale",
        str(args.scale),
        "--collection-socket",
        str(socket_path),
    ]


def main() -> int:
    args = _parser().parse_args()
    if not 0.1 <= args.scale <= 1.0:
        raise SystemExit("--scale must be within [0.1, 1.0]")
    if not 0.1 <= args.max_joint_vel <= 1.0:
        raise SystemExit("--max-joint-vel must be within [0.1, 1.0]")
    if args.hz < 25.0:
        raise SystemExit("--hz must be at least 25 for the new dual-arm safety step")

    child_env = os.environ.copy()
    existing_pythonpath = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(RUNTIME), str(RUNTIME / "src"), existing_pythonpath) if item
    )

    with tempfile.TemporaryDirectory(prefix="data-platform-teleop-") as temp_name:
        temp_dir = Path(temp_name)
        socket_path = temp_dir / "collector.sock"
        collector_config = _collector_config(Path(args.config), args.extra_camera, temp_dir)
        collector_command = [
            sys.executable,
            str(HERE / "remote_collector.py"),
            "--socket",
            str(socket_path),
            "--task",
            args.task,
            "--session-id",
            args.session_id,
            "--session-folder",
            args.session_folder,
            "--out",
            args.out,
            "--config",
            str(collector_config),
            "--collection-project",
            str(Path(args.collection_project).expanduser()),
            "--image-width",
            str(args.image_width),
            "--image-height",
            str(args.image_height),
        ]
        if args.recording_status is not None:
            collector_command.extend(["--recording-status", str(args.recording_status)])

        collector = subprocess.Popen(collector_command, env=child_env)
        worker: subprocess.Popen | None = None
        emitter: subprocess.Popen | None = None
        try:
            _wait_for_collector(socket_path, collector)
            worker = subprocess.Popen(
                _worker_command(args, socket_path),
                stdin=subprocess.PIPE,
                env=child_env,
            )
            assert worker.stdin is not None
            emitter = subprocess.Popen(
                [
                    sys.executable,
                    str(DUAL_ARM_APP),
                    "emit",
                    "--duration",
                    "86400",
                    "--hz",
                    str(args.hz),
                ],
                stdout=worker.stdin,
                env=child_env,
            )
            worker.stdin.close()

            while True:
                worker_status = worker.poll()
                emitter_status = emitter.poll()
                if worker_status is not None:
                    stop_child(emitter)
                    return worker_status
                if emitter_status is not None:
                    stop_child(worker)
                    return emitter_status
                time.sleep(0.1)
        except KeyboardInterrupt:
            return 130
        finally:
            stop_child(emitter)
            stop_child(worker)
            if collector.poll() is None:
                stop_collector(socket_path)
            try:
                collector.wait(timeout=60.0)
            except subprocess.TimeoutExpired:
                collector.terminate()
                collector.wait(timeout=5.0)


if __name__ == "__main__":
    raise SystemExit(main())
