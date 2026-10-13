from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

CONTROL_HEALTH_URL = "http://127.0.0.1:8010/healthz"
MAX_REQUEST_BYTES = 256
_start_lock = threading.Lock()
_control_process: Any | None = None


def service_is_healthy() -> bool:
    try:
        with urllib.request.urlopen(CONTROL_HEALTH_URL, timeout=0.5) as response:
            return response.status == 200 and json.load(response) == {"status": "ok"}
    except (OSError, ValueError, urllib.error.URLError):
        return False


def terminate_process_group(process: Any) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def start_control_service(
    script: Path,
    log_path: Path,
    *,
    spawn: Callable[..., Any] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = 20,
) -> dict[str, object]:
    global _control_process
    if service_is_healthy():
        return {"status": "running", "started": False}
    if not script.is_file() or not os.access(script, os.X_OK):
        return {"status": "error", "detail": f"startup script is not executable: {script}"}

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as log_file:
        process = spawn(
            [str(script)],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _control_process = process

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if service_is_healthy():
            return {"status": "running", "started": True}
        if process.poll() is not None:
            return {
                "status": "error",
                "detail": f"teleop-control exited with code {process.returncode}; see {log_path}",
            }
        sleep(0.2)

    terminate_process_group(process)
    return {
        "status": "error",
        "detail": (
            f"teleop-control did not become ready within {timeout_seconds:g}s; "
            f"see {log_path}"
        ),
    }


def restart_control_service(
    script: Path,
    log_path: Path,
    *,
    spawn: Callable[..., Any] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = 20,
) -> dict[str, object]:
    global _control_process
    process = _control_process
    if process is not None and process.poll() is None:
        terminate_process_group(process)
        _control_process = None
    elif service_is_healthy():
        return {
            "status": "error",
            "detail": "teleop-control is not managed by this launcher; restart the launcher once",
        }
    result = start_control_service(
        script,
        log_path,
        spawn=spawn,
        sleep=sleep,
        timeout_seconds=timeout_seconds,
    )
    if result.get("status") == "running":
        result["restarted"] = process is not None
    return result


def handle_request(payload: bytes, project_root: Path) -> bytes:
    try:
        command = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        command = None
    if command not in {"connect", "restart"}:
        result: dict[str, object] = {"status": "error", "detail": "unsupported request"}
    else:
        with _start_lock:
            script = project_root / "teleop_control" / "run_host_service.sh"
            log_path = project_root / "teleop_control" / "runtime" / "teleop-control.log"
            if command == "restart":
                result = restart_control_service(script, log_path)
            else:
                result = start_control_service(script, log_path)
    return json.dumps(result, ensure_ascii=False).encode() + b"\n"


class LauncherRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        payload = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(payload) > MAX_REQUEST_BYTES:
            self.wfile.write(b'{"status":"error","detail":"request too large"}\n')
            return
        self.wfile.write(handle_request(payload, self.server.project_root))  # type: ignore[attr-defined]


class LauncherServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    project_root: Path


def serve(socket_path: Path, project_root: Path) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.parent.chmod(0o755)
    if socket_path.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                probe.connect(str(socket_path))
        except OSError:
            socket_path.unlink()
        else:
            raise SystemExit(f"host launcher is already listening on {socket_path}")

    server = LauncherServer(str(socket_path), LauncherRequestHandler)
    server.project_root = project_root
    socket_path.chmod(0o660)
    auto_start = os.environ.get("TELEOP_LAUNCHER_AUTO_START", "true").strip().lower()
    if auto_start not in {"0", "false", "no", "off"}:
        with _start_lock:
            start_control_service(
                project_root / "teleop_control" / "run_host_service.sh",
                project_root / "teleop_control" / "runtime" / "teleop-control.log",
            )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)


def main() -> None:
    project_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description="Start teleop-control on host requests")
    parser.add_argument(
        "--socket",
        type=Path,
        default=project_root / "teleop_control" / "runtime" / "launcher.sock",
    )
    args = parser.parse_args()
    serve(args.socket.resolve(), project_root)


if __name__ == "__main__":
    main()
