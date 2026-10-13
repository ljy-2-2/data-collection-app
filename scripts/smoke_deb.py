#!/usr/bin/env python3
"""Unpack and exercise the real bundled runtimes without installing or touching hardware."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("deb", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="xiaokai-deb-smoke-") as temp:
        root = Path(temp)
        subprocess.run(
            ["dpkg-deb", "-x", str(args.deb.resolve()), str(root / "package")],
            check=True,
        )
        package = root / "package/opt/xiaokai-lab-data-collection"
        python = package / "runtime/python/bin/python3.12"
        subprocess.run(
            [
                str(python),
                "-c",
                "import fastapi, uvicorn, httpx, cv2, serial, yaml, h5py, psutil, PySide6.QtQuick, PySide6.QtQuick3D; print('Bundled Python and Qt imports OK')",
            ],
            check=True,
        )
        assert not (package / "runtime/node").exists()
        assert not (package / "node_modules").exists()
        assert not list((package / "runtime").rglob("*WebEngine*"))
        # An absent executable differs from a Docker command returning 1.
        # Use a minimal PATH with no Docker/ADB at all: a false shim masked
        # the fresh-install FileNotFoundError regression.
        shims = root / "no-hardware"
        shims.mkdir()
        for command in ("bash", "dirname", "install"):
            target = shutil.which(command)
            assert target, command
            (shims / command).symlink_to(target)
        assert shutil.which("docker", path=str(shims)) is None
        app_port, control_port, monitor_port = (
            unused_port(),
            unused_port(),
            unused_port(),
        )
        config = root / "config/xiaokai-lab-data-collection"
        config.mkdir(parents=True)
        (config / ".env").write_text(
            f"APP_HOST=127.0.0.1\nAPP_PORT={app_port}\nLOCAL_CONTROL_HOST=127.0.0.1\n"
            f"LOCAL_CONTROL_PORT={control_port}\nLOCAL_CONTROL_URL=http://127.0.0.1:{control_port}\n"
            f"PLATFORM_API_URL=http://127.0.0.1:{monitor_port}/api\nPLATFORM_WEB_URL=http://127.0.0.1:{monitor_port}\n"
            f"TELEOP_GDK_PYTHON={python}\nUMI_DATA_ROOT={root}/recordings\n"
            f"TELEOP_OUTPUT={root}/teleop\nGDK_RIGHT_ARM_IMPORT_ROOT={root}/right\n"
            f"UMI_MONITOR_URL=http://127.0.0.1:{monitor_port}\n"
            f"TELEOP_GDK_APP_ROOT={root}/no-sdk\n"
        )
        env = os.environ.copy()
        env.update(
            {
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "PATH": str(shims),
                "PYTHONNOUSERSITE": "1",
            }
        )
        log = root / "launch.log"
        with log.open("w") as output:
            process = subprocess.Popen(
                [
                    "/usr/bin/python3",
                    str(package / "scripts/package_launcher.py"),
                    "--headless",
                ],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            deadline = time.monotonic() + 45
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Package exited before becoming ready")
                try:
                    with urlopen(
                        f"http://127.0.0.1:{control_port}/healthz", timeout=1
                    ) as response:
                        assert response.status == 200
                        response.read()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Package backend did not become ready") from None
                    time.sleep(0.3)
            with urlopen(
                f"http://127.0.0.1:{control_port}/healthz", timeout=2
            ) as response:
                assert response.status == 200
            with urlopen(
                f"http://127.0.0.1:{control_port}/platform/auth", timeout=2
            ) as response:
                status = json.load(response)
                assert not status.get("authenticated")
            # An unreachable platform must never turn a login attempt into
            # offline access. Exercise the actual packaged HTTP endpoint.
            login_request = Request(
                f"http://127.0.0.1:{control_port}/platform/auth/login",
                data=json.dumps({"username": "smoke-test", "password": "invalid-test-password"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                with urlopen(login_request, timeout=15):
                    raise AssertionError("Unavailable platform allowed login")
            except HTTPError as exc:
                assert exc.code == 502, exc.code
            with urlopen(f"http://127.0.0.1:{control_port}/umi/status", timeout=5) as response:
                assert json.load(response)["record_format"] == "rosbag"
            startup = (package / "app/scripts/qt_ui/qml/Startup.qml").read_text()
            assert "欢迎使用小凯数据采集平台" in startup and "正在进入登录界面" in startup
            assert "NumberAnimation on x" in startup
            print(
                "PASS: extracted Qt runtime + backend + strict login + rosbag default + startup loading bar (no hardware)"
            )
        except Exception:
            print(log.read_text())
            raise
        finally:
            if process.poll() is None:
                # desktop_app.stop() requests the same graceful shutdown signal.
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            for port in (app_port, control_port):
                with socket.socket() as sock:
                    assert sock.connect_ex(("127.0.0.1", port)) != 0, (
                        "Package left a child service running"
                    )
        print("PASS: exit releases both local ports")
        works=list((root / "data/xiaokai-lab-data-collection/workspaces").iterdir())
        work=next(p for p in works if not p.name.startswith('.'))
        ui_env={**env, "QT_QPA_PLATFORM":"offscreen", "QSG_RHI_BACKEND":"opengl", "QSG_RENDER_LOOP":"basic", "QT_SCALE_FACTOR":"1", "LIBGL_ALWAYS_SOFTWARE":"1", "XDG_STATE_HOME":str(root/"state")}
        with log.open("w") as output:
            gui=subprocess.Popen(["/usr/bin/python3",str(package/"scripts/package_launcher.py")],env=ui_env,stdout=output,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+45
            while "NATIVE_SERVICE_READY" not in log.read_text():
                if gui.poll() is not None: raise RuntimeError(log.read_text())
                if time.monotonic()>deadline: raise RuntimeError("Qt desktop did not start: "+log.read_text())
                time.sleep(.2)
            time.sleep(1)
            assert gui.poll() is None,log.read_text()
            assert "QQmlApplicationEngine failed" not in log.read_text(),log.read_text()
            gui.send_signal(signal.SIGTERM)
            assert gui.wait(timeout=25)==0,log.read_text()
            with socket.socket() as sock: assert sock.connect_ex(("127.0.0.1",control_port))!=0
            print("PASS: actual Qt desktop opens and closes with no Docker/Node/SDK; supervisor releases service")
        finally:
            if gui.poll() is None: gui.kill();gui.wait()



if __name__ == "__main__":
    main()
