from __future__ import annotations

import subprocess
from pathlib import Path
from collections.abc import Callable

RunCommand = Callable[..., subprocess.CompletedProcess[str]]

_ADB_SERVER_FAULT_MARKERS = (
    "failed to check server version",
    "protocol fault",
    "cannot connect to daemon",
    "server didn't ack",
    "connection reset",
    "could not install",
)


class GalaxisHeadsetSetup:
    """Best-effort setup supported by the vendor Pico application.

    Galaxis VR 1.1.6 does not expose its in-app tracking toggles through an
    intent or config file. This setup only performs operations that have a
    stable ADB interface: USB reverse, hand-tracking permission, and launch.
    """

    def __init__(
        self,
        *,
        adb_path: str = "adb",
        package: str = "com.galaxis.vrteleop",
        run_command: RunCommand = subprocess.run,
    ) -> None:
        self.adb_path = adb_path
        self.package = package
        self.run_command = run_command
        self.bundled_apk = Path(__file__).resolve().parents[3] / "umi_collection/headset/galaxis-vr.apk"

    def _run(
        self, *args: str, timeout: float = 10.0
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return self.run_command(
                [self.adb_path, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def probe_ready(self) -> bool:
        """Quick, read-only UMI preflight; the collector prepares the tunnel."""
        result = self._run("devices", timeout=2.0)
        if result is None or result.returncode != 0:
            return False
        devices = [
            row.split()[0]
            for row in result.stdout.splitlines()[1:]
            if row.strip().endswith("\tdevice")
        ]
        if len(devices) != 1:
            return False
        running = self._run(
            "-s", devices[0], "shell", "pidof", self.package, timeout=2.0
        )
        return bool(
            running is not None
            and running.returncode == 0
            and running.stdout.strip()
        )

    def wake(self) -> bool:
        """Keep a running VR sender awake without resetting its in-app switches."""
        result = self._run(
            "shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=4.0
        )
        return result is not None and result.returncode == 0

    def ensure_started(self, *, force_relaunch: bool = False, quick: bool = False) -> bool:
        device_timeout = 4.0 if quick else 8.0
        command_timeout = 4.0 if quick else 10.0
        result = self._run("devices", timeout=device_timeout)
        if result is None:
            result = self._run("devices", timeout=command_timeout)
        if result is not None and (
            result.returncode != 0
            or any(
                marker in f"{result.stdout} {result.stderr}".lower()
                for marker in _ADB_SERVER_FAULT_MARKERS
            )
        ):
            self._run("kill-server", timeout=command_timeout)
            self._run("start-server", timeout=command_timeout)
            result = self._run("devices", timeout=command_timeout)
        if result is None:
            return False

        devices = [
            row.split()[0]
            for row in result.stdout.splitlines()[1:]
            if row.strip().endswith("\tdevice")
        ]
        if len(devices) != 1:
            return False

        serial = devices[0]
        prefix = [self.adb_path, "-s", serial]
        if self.bundled_apk.is_file():
            installed = self._run(*prefix[1:], "shell", "pm", "path", self.package, timeout=command_timeout)
            if installed is None:
                return False
            if not (installed.returncode == 0 and installed.stdout.strip().startswith("package:")):
                result = self._run(*prefix[1:], "install", str(self.bundled_apk), timeout=120.0)
                if result is None or result.returncode != 0:
                    return False
        # PICO stops controller 6DoF while the display is asleep. Wake it
        # before checking/launching the Unity sender so UMI does not receive a
        # healthy 90 Hz stream containing only zero controller poses.
        self._run(
            *prefix[1:], "shell", "input", "keyevent", "KEYCODE_WAKEUP",
            timeout=command_timeout,
        )
        # Relaunching the VR app restarts its headset session. When Galaxis is
        # already running (normal page-return case), only keep the ADB tunnel
        # and permission in place and leave the app untouched.
        running = self._run(
            *prefix[1:], "shell", "pidof", self.package, timeout=command_timeout
        )
        already_running = (
            running is not None
            and running.returncode == 0
            and bool(running.stdout.strip())
        )
        commands: tuple[list[str], ...] = ()
        if force_relaunch and already_running:
            commands += (
                [*prefix, "shell", "am", "force-stop", self.package],
            )
            already_running = False
        commands += (
            [*prefix, "reverse", "--remove", "tcp:63901"],
            [*prefix, "reverse", "tcp:63901", "tcp:63901"],
            [
                *prefix,
                "shell",
                "pm",
                "grant",
                self.package,
                "com.picovr.permission.HAND_TRACKING",
            ],
        )
        if not already_running:
            commands += (
                [
                    *prefix,
                    "shell",
                    "monkey",
                    "-p",
                    self.package,
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                ],
            )
        for command in commands:
            outcome = self._run(*command[1:], timeout=command_timeout)
            if outcome is None:
                return False
        return True
