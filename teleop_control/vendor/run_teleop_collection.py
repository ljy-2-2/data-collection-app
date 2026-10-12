#!/usr/bin/env python3
"""One-command dual-arm XR teleoperation and A/B episode collection."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def stop_child(child: subprocess.Popen, *, timeout: float = 5.0) -> None:
    if child.poll() is not None:
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


def run_teleop_child(command: list[str], env: dict[str, str]) -> int:
    child = subprocess.Popen(command, env=env)
    try:
        return child.wait()
    except KeyboardInterrupt:
        stop_child(child)
        return 130
    finally:
        stop_child(child)


def prepend_env_path(env: dict[str, str], name: str, paths) -> None:
    """Prepend existing paths to a colon-separated subprocess environment."""
    old = [item for item in env.get(name, "").split(os.pathsep) if item]
    new = [str(Path(item)) for item in paths if Path(item).exists()]
    env[name] = os.pathsep.join(dict.fromkeys(new + old))


def build_gdk_env(app_root: str) -> dict[str, str]:
    """Make collector/teleop independent of whether env.sh was sourced."""
    env = os.environ.copy()
    root = Path(app_root).expanduser().resolve()
    python_dir = root / "gdk" / "lib"
    native_root = root / "lib"
    if not (python_dir / "agibot_gdk").is_dir():
        raise SystemExit(
            f"agibot_gdk not found under {python_dir}; "
            "set --gdk-app-root to the installed AgiBot app directory"
        )
    if not native_root.is_dir():
        raise SystemExit(f"AgiBot native library directory not found: {native_root}")

    # env.sh adds every directory below app/lib that contains a shared object.
    native_dirs = sorted({
        so.parent for so in native_root.rglob("*.so*") if so.is_file()
    })
    prepend_env_path(env, "PYTHONPATH", [python_dir])
    prepend_env_path(env, "LD_LIBRARY_PATH", native_dirs)
    vendor_yaml = root / "gdk" / "config" / "app_conf.yml"
    vendor_json = root / "gdk" / "config" / "app_conf.json"
    if vendor_yaml.is_file():
        env.setdefault("APP_CONF_PATH", str(vendor_yaml))
    elif vendor_json.is_file():
        env.setdefault("APP_CONF_PATH", str(vendor_json))

    # Use the same unique-NIC selection as the App, including direct CLI use.
    source = str(HERE.parent / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from teleop_control.gdk_env import refresh_discovery_env

    refresh_discovery_env(env, env.get("TELEOP_ROBOT_INTERFACE", "auto"))
    if not env.get("LOCATOR_IP"):
        raise SystemExit(
            "No unique G2 interface address; connect the robot network or set TELEOP_ROBOT_INTERFACE"
        )
    return env


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def start_xr_service(script: str):
    if port_open(60061):
        print("[launch] Galaxis VR already listening on 60061")
        return
    path = Path(script).expanduser()
    if not path.exists():
        raise RuntimeError(f"Galaxis VR launcher not found: {path}")
    log = open("/tmp/test-teleop-xrservice.log", "a")
    # Vendor runService.sh has no shebang, so executing it directly raises
    # ENOEXEC ("Exec format error").  It also uses BASH_SOURCE, therefore run
    # it explicitly with bash rather than relying on /bin/sh fallback.
    subprocess.Popen(["bash", str(path)], stdout=log, stderr=subprocess.STDOUT,
                     start_new_session=True)
    print("[launch] starting Galaxis VR (log: /tmp/test-teleop-xrservice.log)")
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if port_open(60061):
            print("[launch] Galaxis VR ready")
            return
        time.sleep(0.5)
    raise RuntimeError("Galaxis VR did not open port 60061 within 20 seconds")


def setup_headset(serial: str | None, package: str) -> str:
    if shutil.which("adb") is None:
        raise RuntimeError("adb not found")
    subprocess.run(["adb", "start-server"], check=True)
    rows = subprocess.check_output(["adb", "devices"], text=True).splitlines()[1:]
    devices = [r.split()[0] for r in rows if r.strip().endswith("\tdevice")]
    if serial:
        if serial not in devices:
            raise RuntimeError(f"ADB device {serial} not online; online={devices}")
    elif len(devices) == 1:
        serial = devices[0]
    else:
        raise RuntimeError(f"need --adb-serial because online devices={devices}")
    subprocess.run(["adb", "-s", serial, "reverse", "--remove", "tcp:63901"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["adb", "-s", serial, "reverse", "tcp:63901", "tcp:63901"],
                   check=True)
    subprocess.run(["adb", "-s", serial, "shell", "monkey", "-p", package,
                    "-c", "android.intent.category.LAUNCHER", "1"], check=True,
                   stdout=subprocess.DEVNULL)
    print(f"[launch] headset {serial}: reverse 63901 and launched {package}")
    return serial


def stop_collector(socket_path: Path):
    """Best-effort shutdown when teleop exits before/without its IPC client."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(socket_path))
            sock.sendall(b'{"type":"shutdown"}\n')
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", required=True, help="dataset task label")
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--session-folder", required=True)
    ap.add_argument("--out", default="~/agibot_episodes/dual_arm")
    ap.add_argument("--hz", type=float, default=100.0)
    ap.add_argument("--scale", type=float, default=0.5)
    ap.add_argument("--max-joint-vel", type=float, default=0.5)
    ap.add_argument(
        "--extra-camera", action="append", default=[],
        choices=("head_depth", "hand_left_depth", "hand_right_depth"),
        help="additional head stream to save (repeat for multiple streams)",
    )
    ap.add_argument("--image-width", type=int, default=640)
    ap.add_argument("--image-height", type=int, default=480)
    ap.add_argument("--adb-serial", default=None)
    ap.add_argument("--no-device-setup", action="store_true",
                    help="do not run adb reverse or launch Galaxis")
    ap.add_argument("--no-start-xr-service", action="store_true")
    ap.add_argument("--plot", action="store_true",
                    help="live 3-D right-EE trajectory in the head camera frame")
    ap.add_argument("--yes", action="store_true",
                    help="confirmation already completed by the web control service")
    ap.add_argument("--xr-service", default="/opt/apps/roboticsservice/runService.sh")
    ap.add_argument("--galaxis-package", default="com.galaxis.vrteleop")
    ap.add_argument("--gdk-app-root", default="~/.cache/agibot/app",
                    help="AgiBot app root containing gdk/lib and lib")
    ap.add_argument("--collection-project", default=str(HERE / "gdk_data_collection"))
    ap.add_argument("--config", default=str(HERE / "config" / "dual_arm.yaml"))
    ap.add_argument("--recording-status", type=Path, default=None)
    args = ap.parse_args()

    for module in ("cv2", "yaml"):
        try:
            __import__(module)
        except ImportError:
            raise SystemExit(f"missing {module}; run: python -m pip install "
                             "opencv-python-headless pyyaml") from None

    if not args.no_start_xr_service:
        start_xr_service(args.xr_service)
    elif not port_open(60061):
        raise SystemExit("Galaxis VR is not listening on port 60061")
    if not args.no_device_setup:
        setup_headset(args.adb_serial, args.galaxis_package)

    child_env = build_gdk_env(args.gdk_app_root)
    runtime_dir = HERE.parents[1] / "teleop_control" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # GDK's glog writes next to the process CWD by default. The host service
    # starts from $HOME, which would litter the home directory with
    # python3.10.log.* files; pin glog output to the platform runtime dir.
    child_env["GLOG_log_dir"] = str(runtime_dir)
    print("[launch] AgiBot GDK Python/native library paths ready")

    ipc_dir = Path(tempfile.mkdtemp(prefix="test-teleop-"))
    socket_path = ipc_dir / "collector.sock"
    collector_config = Path(args.config).expanduser()
    if args.extra_camera:
        config = yaml.safe_load(collector_config.read_text()) or {}
        cameras = config.setdefault("cameras", [])
        existing = {
            item if isinstance(item, str) else item.get("name")
            for item in cameras
        }
        for camera in args.extra_camera:
            if camera not in existing:
                cameras.append({"name": camera, "essential": False, "staleness_budget_ms": 50})
                existing.add(camera)
        collector_config = ipc_dir / "collection.yaml"
        collector_config.write_text(yaml.safe_dump(config, sort_keys=False))
    print(
        "[launch] collector extra cameras: "
        + (", ".join(args.extra_camera) or "none"),
        flush=True,
    )
    collector_cmd = [
        sys.executable, str(HERE / "remote_collector.py"),
        "--socket", str(socket_path), "--task", args.task,
        "--session-id", args.session_id,
        "--session-folder", args.session_folder,
        "--out", args.out, "--config", str(collector_config),
        "--collection-project", str(Path(args.collection_project).expanduser()),
        "--image-width", str(args.image_width),
        "--image-height", str(args.image_height),
    ]
    if args.recording_status is not None:
        collector_cmd.extend(["--recording-status", str(args.recording_status)])
    collector = subprocess.Popen(collector_cmd, env=child_env)
    deadline = time.monotonic() + 10.0
    while not socket_path.exists() and collector.poll() is None:
        if time.monotonic() >= deadline:
            collector.terminate()
            raise SystemExit("collector IPC did not start")
        time.sleep(0.1)
    if collector.poll() is not None:
        raise SystemExit(f"collector exited early ({collector.returncode})")

    teleop_cmd = [
        sys.executable, str(HERE / "send.py"),
        "--hz", str(args.hz), "--scale", str(args.scale),
        "--max-joint-vel", str(args.max_joint_vel),
        "--collect-socket", str(socket_path),
        "--control-side", "both",
    ]
    if args.plot:
        teleop_cmd.append("--plot")
    if args.yes:
        teleop_cmd.append("--yes")
    print("[launch] DUAL-ARM components started; confirm teleop with YES")
    print("[launch] A=start episode, B=stop+save, next A=next episode")
    try:
        rc = run_teleop_child(teleop_cmd, child_env)
    finally:
        if collector.poll() is None:
            stop_collector(socket_path)
        try:
            # A B-key stop can leave image normalization running for several
            # seconds. Do not interrupt it before the upload-ready marker exists.
            collector.wait(timeout=60.0)
        except subprocess.TimeoutExpired:
            collector.terminate()
            collector.wait(timeout=5.0)
        try:
            socket_path.unlink()
            ipc_dir.rmdir()
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
