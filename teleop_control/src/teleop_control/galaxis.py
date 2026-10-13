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
        self.last_error = ""
        self._serial: str | None = None

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
        except FileNotFoundError:
            self.last_error = "未安装或无法找到 ADB，请修复采集软件的 ADB 依赖"
            return None
        except subprocess.TimeoutExpired:
            self.last_error = "PICO USB 调试响应超时，请检查数据线和头显连接"
            return None
        except OSError as exc:
            self.last_error = f"无法执行 ADB：{exc}"
            return None

    def _device(self, result: subprocess.CompletedProcess[str] | None) -> str | None:
        if result is None:
            return None
        if result.returncode != 0:
            self.last_error = "ADB 连接失败：" + (result.stderr or result.stdout).strip()[:300]
            return None
        rows = [row.split() for row in result.stdout.splitlines()
                if row.strip() and not row.startswith(("List of devices", "*"))]
        # Orange Pi's own gadget adbd can appear as emulator-5554. It is not a
        # headset and must not make an attached USB PICO look ambiguous.
        devices = [row[0] for row in rows if len(row) > 1 and row[1] == "device"
                   and not row[0].startswith("emulator-")]
        if len(devices) == 1:
            self._serial = devices[0]
            return devices[0]
        self._serial = None
        text = result.stdout.lower()
        if len(devices) > 1:
            self.last_error = "发现多个 ADB 设备，请仅连接用于本次采集的 PICO"
        elif "unauthorized" in text:
            self.last_error = "PICO 未授权这台设备；请戴上头显并允许 USB 调试（换电脑或开发板后需重新授权）"
        elif "no permissions" in text:
            self.last_error = "当前账号无 PICO USB 访问权限；请安装 USB 设备规则并重新登录系统、插拔数据线"
        elif "offline" in text:
            self.last_error = "PICO USB 调试离线，请解锁头显并重新连接数据线"
        else:
            self.last_error = "未发现 PICO USB 调试设备，请使用数据线连接、开启开发者模式和 USB 调试"
        return None

    def _require_success(self, result: subprocess.CompletedProcess[str] | None, action: str) -> bool:
        if result is None:
            return False
        if result.returncode != 0:
            reason = (result.stderr or result.stdout).strip()[:300]
            self.last_error = f"{action}失败" + (f"：{reason}" if reason else "")
            return False
        return True

    def probe_ready(self) -> bool:
        """Quick, read-only UMI preflight; the collector prepares the tunnel."""
        self.last_error = ""
        result = self._run("devices", timeout=2.0)
        serial = self._device(result)
        if serial is None:
            return False
        running = self._run(
            "-s", serial, "shell", "pidof", self.package, timeout=2.0
        )
        ready = bool(
            running is not None
            and running.returncode == 0
            and running.stdout.strip()
        )
        if not ready and not self.last_error:
            self.last_error = "PICO 已授权，但 Galaxis VR 尚未启动"
        return ready

    def wake(self) -> bool:
        """Keep a running VR sender awake without resetting its in-app switches."""
        result = self._run(
            *(["-s", self._serial] if self._serial else []),
            "shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=4.0
        )
        return self._require_success(result, "唤醒 PICO")

    def ensure_started(self, *, force_relaunch: bool = False, quick: bool = False) -> bool:
        self.last_error = ""
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
        serial = self._device(result)
        if serial is None:
            return False
        prefix = [self.adb_path, "-s", serial]
        if self.bundled_apk.is_file():
            installed = self._run(*prefix[1:], "shell", "pm", "path", self.package, timeout=command_timeout)
            if installed is None:
                return False
            if not (installed.returncode == 0 and installed.stdout.strip().startswith("package:")):
                result = self._run(*prefix[1:], "install", str(self.bundled_apk), timeout=120.0)
                if not self._require_success(result, "安装 PICO Galaxis VR"):
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
        if force_relaunch and already_running:
            if not self._require_success(
                self._run(*prefix[1:], "shell", "am", "force-stop", self.package,
                          timeout=command_timeout), "重启 Galaxis VR"
            ):
                return False
            already_running = False

        def tunnel_ready(outcome):
            return (outcome is not None and outcome.returncode == 0 and any(
                row.split()[-2:] == ["tcp:63901", "tcp:63901"]
                for row in outcome.stdout.splitlines()
            ))

        # Re-checking a connected headset must not tear down its live stream.
        # A successful ADB command alone is insufficient: verify the mapping.
        mappings = self._run(*prefix[1:], "reverse", "--list", timeout=command_timeout)
        if not tunnel_ready(mappings):
            if not self._require_success(
                self._run(*prefix[1:], "reverse", "tcp:63901", "tcp:63901",
                          timeout=command_timeout), "建立 PICO USB 数据隧道"
            ):
                return False
            mappings = self._run(*prefix[1:], "reverse", "--list", timeout=command_timeout)
            if not tunnel_ready(mappings):
                self.last_error = "PICO USB 数据隧道校验失败，请重新连接数据线后重试"
                return False
        # Some PICO firmware versions do not expose this optional permission;
        # controller tracking and the USB tunnel do not depend on its grant.
        self._run(
            *prefix[1:], "shell", "pm", "grant", self.package,
            "com.picovr.permission.HAND_TRACKING", timeout=command_timeout,
        )
        if not already_running:
            outcome = self._run(
                *prefix[1:], "shell", "monkey", "-p", self.package,
                "-c", "android.intent.category.LAUNCHER", "1", timeout=command_timeout,
            )
            if not self._require_success(outcome, "启动 PICO Galaxis VR"):
                return False
        self.last_error = ""
        return True
