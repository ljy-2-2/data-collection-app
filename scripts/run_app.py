#!/usr/bin/env python3
"""Start the self-contained collection service and local operator interface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from generate_runtime_config import ROOT, platform_urls, read_env

sys.path.insert(0, str(ROOT / "teleop_control" / "src"))
from teleop_control.platform_auth import PlatformTokenStore  # noqa: E402


def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def require_process_started(process: subprocess.Popen[bytes], label: str) -> None:
    """Catch immediate launch errors without depending on sandboxed loopback probes."""
    for _ in range(12):
        if process.poll() is not None:
            raise RuntimeError(f"{label}启动失败，请查看上方日志")
        time.sleep(0.25)


def wait_for_control_service(process: subprocess.Popen[bytes], host: str, port: int) -> None:
    """Wait for the shutdown API, not just the service process, to start."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("本机采集服务启动失败，请查看上方日志")
        try:
            with urlopen(f"http://{host}:{port}/healthz", timeout=1.0) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.25)
    raise RuntimeError("本机采集服务未能就绪，无法安全初始化 UMI")


def clear_previous_app_login(values: dict[str, str]) -> None:
    """Clear session tokens; optional keyring credentials survive App restarts.

    The login screen reauthenticates automatically only when explicitly enabled.
    Explicit logout, unlike closing the App, also deletes saved credentials.
    """
    configured = values.get("PLATFORM_TOKEN_FILE", "").strip()
    PlatformTokenStore(configured or None).clear()


def stop_previous_umi(control_host: str, control_port: int) -> None:
    """Save and stop this App's UMI session before a fresh launch or exit."""
    request = Request(
        f"http://{control_host}:{control_port}/umi/app-shutdown",
        method="POST",
    )
    try:
        with urlopen(request, timeout=245) as response:
            response.read()
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = str(payload.get("detail") or exc.reason)
        except (OSError, ValueError, AttributeError):
            detail = str(exc.reason)
        raise RuntimeError(f"UMI 安全停止失败：{detail}") from exc
    except (OSError, URLError) as exc:
        raise RuntimeError(f"无法连接本机采集服务以停止 UMI：{exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Robot data collection console")
    parser.add_argument("--mock", action="store_true", help="start without robot hardware")
    parser.add_argument("--no-browser", action="store_true", help="do not open the UI automatically")
    parser.add_argument("--backend-only", action="store_true", help="run service for native Qt interface")
    args = parser.parse_args()

    values = read_env(ROOT / ".env")
    values.update(platform_urls(values))
    env = os.environ.copy()
    env.update(values)
    # A native camera/serial crash bypasses Python exceptions. Keep the fatal
    # thread traceback in the desktop launch log instead of a silent API loss.
    env["PYTHONFAULTHANDLER"] = "1"
    env["DATA_PLATFORM_API_URL"] = values.get("PLATFORM_API_URL", "http://127.0.0.1:8000")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "teleop_control" / "src"), env.get("PYTHONPATH", "")) if part
    )
    npm_bin = env.get("APP_NPM_BIN") or shutil.which("npm", path=env.get("PATH"))
    node_bin = env.get("APP_NODE_BIN")
    if not args.backend_only and not node_bin and (not npm_bin or not Path(npm_bin).is_file()):
        print("ERROR: 找不到 Node.js/npm，请运行 install-app.sh。", file=sys.stderr)
        return 1

    if not args.backend_only:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_runtime_config.py")], check=True)

    control_host = values.get("LOCAL_CONTROL_HOST", "127.0.0.1")
    control_port = int(values.get("LOCAL_CONTROL_PORT", "8010"))
    app_host = values.get("APP_HOST", "127.0.0.1")
    app_port = int(values.get("APP_PORT", "4173"))
    app_url = f"http://{app_host}:{app_port}"

    if port_is_open(control_host, control_port):
        print(f"ERROR: {control_host}:{control_port} is already in use.", file=sys.stderr)
        print("Stop the old collection service or change LOCAL_CONTROL_PORT/URL in .env.", file=sys.stderr)
        return 2
    if not args.backend_only and port_is_open(app_host, app_port):
        print(f"ERROR: {app_host}:{app_port} is already in use; change APP_PORT in .env.", file=sys.stderr)
        return 2

    clear_previous_app_login(values)

    service_python = values.get("TELEOP_SERVICE_PYTHON") or sys.executable
    control_command = [
        service_python,
        "-m",
        "teleop_control.main",
        "--host",
        control_host,
        "--port",
        str(control_port),
    ]
    if args.mock:
        control_command.append("--mock")

    frontend_command = [
        npm_bin, "run", "start", "--", "--hostname", app_host, "--port", str(app_port)
    ]
    if node_bin:
        frontend_command = [node_bin, str(ROOT / "scripts/serve_app.mjs"), app_host, str(app_port)]
    processes: list[subprocess.Popen[bytes]] = []
    umi_initialized = False
    try:
        processes.append(subprocess.Popen(control_command, cwd=ROOT, env=env, start_new_session=True))
        require_process_started(processes[-1], "本机采集服务")
        wait_for_control_service(processes[-1], control_host, control_port)
        stop_previous_umi(control_host, control_port)
        umi_initialized = True

        commands = [("本机采集服务", control_command)]
        if not args.backend_only:
            processes.append(subprocess.Popen(frontend_command, cwd=ROOT, env=env, start_new_session=True))
            require_process_started(processes[-1], "App 界面")
            commands.append(("App 界面", frontend_command))
            print(f"Collection Console: {app_url}")
            if not args.no_browser:
                webbrowser.open(app_url)
        print("NATIVE_SERVICE_READY", flush=True)
        recent_restarts: list[list[float]] = [[], []]
        try:
            while True:
                time.sleep(0.5)
                for index, (label, command) in enumerate(commands):
                    exited = processes[index]
                    if exited.poll() is None:
                        continue
                    now = time.monotonic()
                    recent_restarts[index] = [
                        instant for instant in recent_restarts[index] if now - instant < 60
                    ]
                    if len(recent_restarts[index]) >= 3:
                        raise RuntimeError(
                            f"{label}在一分钟内连续退出，已停止自动恢复；退出码 {exited.returncode}"
                        )
                    recent_restarts[index].append(now)
                    print(
                        f"WARNING: {label}意外退出（退出码 {exited.returncode}），正在自动恢复"
                        f"（{len(recent_restarts[index])}/3）",
                        file=sys.stderr,
                        flush=True,
                    )
                    replacement = subprocess.Popen(
                        command, cwd=ROOT, env=env, start_new_session=True
                    )
                    processes[index] = replacement
                    require_process_started(replacement, label)
                    if index == 0:
                        # A crashed control process may leave a still-running
                        # collector. The new service restores its saved PID;
                        # never call app-shutdown during an automatic restart.
                        wait_for_control_service(replacement, control_host, control_port)
                    print(f"{label}已恢复", flush=True)
        except KeyboardInterrupt:
            return 0
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if umi_initialized and processes and processes[0].poll() is None:
            try:
                stop_previous_umi(control_host, control_port)
            except RuntimeError as exc:
                print(f"WARNING: {exc}", file=sys.stderr)
        for process in reversed(processes):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGINT)
                    process.wait(timeout=6)
                except (OSError, subprocess.TimeoutExpired):
                    process.terminate()
        clear_previous_app_login(values)


if __name__ == "__main__":
    raise SystemExit(main())
