"""Connect and launch the installed, unchanged Galaxis application. No APK writes."""

import os
import platform
from pathlib import Path
import socket
import subprocess
import threading
import time


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def ensure_service(config):
    if listening(63901) and listening(60061):
        return "existing RoboticsService reused"
    if listening(63901) or listening(60061):
        raise RuntimeError(
            "63901/60061 partially occupied; check RoboticsService before starting"
        )
    directory = Path(config["service_dir"])
    executable = directory / "RoboticsServiceProcess"
    if not executable.exists():
        raise RuntimeError(f"RoboticsService not found: {executable}")
    env = os.environ.copy()
    sdk_arch = "arm64" if platform.machine().lower() in {"aarch64", "arm64"} else "x64"
    env["LD_LIBRARY_PATH"] = ":".join(
        [
            str(directory),
            str(directory / "lib"),
            str(directory / "SDK" / sdk_arch),
            env.get("LD_LIBRARY_PATH", ""),
        ]
    )
    env["QT_PLUGIN_PATH"] = str(directory / "plugins")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(exist_ok=True)
    with (log_dir / "roboticsservice.log").open("a") as log:
        process = subprocess.Popen(
            [str(executable)],
            cwd=directory,
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if listening(63901) and listening(60061):
            return "RoboticsService started"
        if process.poll() is not None:
            raise RuntimeError("RoboticsService exited; see logs/roboticsservice.log")
        time.sleep(0.1)
    process.terminate()
    raise RuntimeError("RoboticsService startup timeout")


class PicoLauncher:
    def __init__(self, config):
        self.cfg = config
        self.status = "waiting for authorized PICO USB"
        self.done = threading.Event()
        self.thread = None
        self.serial = None
        self.app_started = False
        self.saved_reverse = None

    def adb(self, *args, selected=True, timeout=8):
        cmd = ["adb"] + (["-s", self.serial] if selected else []) + list(args)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        return result.stdout.strip()

    def devices(self):
        result = self.adb("devices", "-l", selected=False)
        lines = [line.split() for line in result.splitlines()[1:] if line.strip()]
        authorized = [
            line[0] for line in lines if len(line) > 1 and line[1] == "device"
            and not line[0].startswith("emulator-")
        ]
        requested = self.cfg["serial"]
        if requested:
            if requested not in authorized:
                raise RuntimeError(f"PICO {requested} unavailable/unauthorized")
            return requested
        if len(authorized) != 1:
            raise RuntimeError(
                "connect exactly one authorized PICO or set serial in config"
            )
        return authorized[0]

    def require_pico(self):
        maker = self.adb("shell", "getprop", "ro.product.manufacturer")
        model = self.adb("shell", "getprop", "ro.product.model")
        if "pico" not in (maker + " " + model).lower() and model != "A9210":
            raise RuntimeError(f"device is not a PICO: {maker} {model}")

    def ensure(self):
        serial = self.devices()
        if serial != self.serial:
            self.app_started = False
            self.saved_reverse = None
        self.serial = serial
        self.require_pico()
        package = self.cfg["package"]
        installed = self.adb("shell", "pm", "list", "packages", package)
        if "package:" + package not in installed.splitlines():
            raise RuntimeError(
                "original Galaxis VR application is not installed; install your existing APK"
            )
        target = 63901 if self.cfg["input"] == "sdk" else self.cfg["port"]
        mappings = self.adb("reverse", "--list")
        if self.saved_reverse is None:
            self.saved_reverse = next(
                (
                    line.split()[-1]
                    for line in mappings.splitlines()
                    if len(line.split()) >= 3 and line.split()[-2] == "tcp:63901"
                ),
                "",
            )
        if not any(
            line.split()[-2:] == ["tcp:63901", f"tcp:{target}"]
            for line in mappings.splitlines()
        ):
            self.adb("reverse", "tcp:63901", f"tcp:{target}")
        if not self.app_started:
            # No custom intents, no force-stop, no reinstall, no preference changes.
            self.adb(
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            )
            self.app_started = True
        pid = self.adb("shell", "pidof", package)
        if not pid:
            raise RuntimeError("Galaxis process not running")
        self.status = f"PICO={serial} APP_PID={pid} USB=63901->{target}; ADB 和应用就绪，数据状态见 VR 连接"

    def run(self):
        while not self.done.is_set():
            try:
                self.ensure()
            except (
                OSError,
                RuntimeError,
                subprocess.TimeoutExpired,
                IndexError,
            ) as error:
                self.status = str(error)
                self.app_started = False
            self.done.wait(3)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def close(self):
        self.done.set()
        if self.thread:
            self.thread.join(timeout=9)
        # Direct mode temporarily routes the original headset port to Python.
        if (
            self.cfg["input"] == "tcp"
            and self.serial
            and self.saved_reverse is not None
        ):
            try:
                if self.saved_reverse:
                    self.adb("reverse", "tcp:63901", self.saved_reverse)
                else:
                    self.adb("reverse", "--remove", "tcp:63901")
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass  # USB may already be unplugged; no application state was changed.
