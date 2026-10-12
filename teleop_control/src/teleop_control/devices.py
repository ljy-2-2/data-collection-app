from __future__ import annotations

import re
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

from .models import DeviceStatus
from .gdk_env import detect_robot_interface

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
PortChecker = Callable[[str, int, float], bool]

_ADB_SERVER_FAULT_MARKERS = (
    "failed to check server version",
    "protocol fault",
    "cannot connect to daemon",
    "server didn't ack",
    "connection reset",
    "could not install",
)


def port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class DeviceDetector:
    def __init__(
        self,
        *,
        robot_interface: str = "auto",
        robot_subnet_prefix: str = "10.42.1.",
        sysfs_root: Path = Path("/sys/class/net"),
        adb_path: str = "adb",
        run_command: RunCommand = subprocess.run,
        port_checker: PortChecker = port_open,
        robot_unavailable_reason: str | None = None,
    ) -> None:
        self.robot_interface = robot_interface
        self.robot_subnet_prefix = robot_subnet_prefix
        self.sysfs_root = sysfs_root
        self.adb_path = adb_path
        self.run_command = run_command
        self.port_checker = port_checker
        self.robot_unavailable_reason = robot_unavailable_reason

    def check_robot(self) -> DeviceStatus:
        if self.robot_unavailable_reason:
            return DeviceStatus(
                id="robot", name="智元 G2", status="offline",
                detail=self.robot_unavailable_reason,
            )
        interface = detect_robot_interface(
            self.robot_interface, self.robot_subnet_prefix, run_command=self.run_command
        )
        if not interface:
            return DeviceStatus(id="robot", name="智元 G2", status="offline",
                                detail="未找到唯一的机器人网卡，请连接机器人网络；多网卡时配置 TELEOP_ROBOT_INTERFACE")
        interface_root = self.sysfs_root / interface
        try:
            carrier = (interface_root / "carrier").read_text().strip()
            operstate = (interface_root / "operstate").read_text().strip()
        except OSError as exc:
            return DeviceStatus(
                id="robot",
                name="智元 G2",
                status="offline",
                detail=f"Robot interface {interface} unavailable: {exc}",
            )
        if carrier != "1" or operstate != "up":
            return DeviceStatus(
                id="robot",
                name="智元 G2",
                status="offline",
                detail=f"Robot Ethernet cable is unplugged ({interface})",
            )

        command = ["ip", "-o", "-4", "addr", "show", "dev", interface]
        try:
            result = self.run_command(
                command,
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return DeviceStatus(
                id="robot",
                name="智元 G2",
                status="warning",
                detail=f"Robot cable is connected; cannot read interface address: {exc}",
            )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Cannot read robot interface"
            return DeviceStatus(id="robot", name="智元 G2", status="warning", detail=detail)
        addresses = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)/", result.stdout)
        address = next(
            (item for item in addresses if item.startswith(self.robot_subnet_prefix)),
            None,
        )
        if address is None:
            return DeviceStatus(
                id="robot",
                name="智元 G2",
                status="warning",
                detail=(
                    f"{interface} cable connected but no "
                    f"{self.robot_subnet_prefix}x address"
                ),
            )
        try:
            speed = (interface_root / "speed").read_text().strip()
        except OSError:
            speed = "unknown"
        speed_detail = f"{speed} Mbps" if speed.isdigit() else "speed unknown"
        return DeviceStatus(
            id="robot",
            name="智元 G2",
            status="connected",
            detail=f"{interface} cable connected · {address} · {speed_detail}",
        )

    def check_xr(self) -> DeviceStatus:
        connected = self.port_checker("127.0.0.1", 60061, 0.3)
        return DeviceStatus(
            id="xr",
            name="Galaxis VR",
            status="connected" if connected else "offline",
            detail="127.0.0.1:60061 listening" if connected else "Port 60061 is not listening",
        )

    def check_headset(self) -> DeviceStatus:
        result = self._run_adb("devices", timeout=8.0)
        if result is None:
            result = self._run_adb("devices", timeout=10.0)
            if result is None:
                return DeviceStatus(
                    id="headset", name="Pico", status="offline", detail="adb unavailable"
                )
        if self._is_adb_server_fault(result):
            self._restart_adb_server()
            result = self._run_adb("devices", timeout=10.0)
            if result is None:
                return DeviceStatus(
                    id="headset",
                    name="Pico",
                    status="offline",
                    detail="adb unavailable after restart",
                )

        rows = self._adb_rows(result.stdout)
        online = [serial for serial, state in rows if state == "device"]
        if online:
            return DeviceStatus(
                id="headset", name="Pico", status="connected", detail=", ".join(online)
            )
        if rows:
            if any(state == "offline" for _, state in rows):
                self._run_adb("reconnect", "offline", timeout=5.0)
                retry = self._run_adb("devices", timeout=10.0)
                if retry is not None:
                    rows = self._adb_rows(retry.stdout)
                    online = [serial for serial, state in rows if state == "device"]
                    if online:
                        return DeviceStatus(
                            id="headset",
                            name="Pico",
                            status="connected",
                            detail=", ".join(online),
                        )
            detail = ", ".join(f"{serial} {state}" for serial, state in rows)
            return DeviceStatus(id="headset", name="Pico", status="warning", detail=detail)
        detail = result.stderr.strip() or "No ADB device online"
        return DeviceStatus(id="headset", name="Pico", status="offline", detail=detail)

    def _run_adb(
        self, *args: str, timeout: float = 5.0
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

    @staticmethod
    def _adb_rows(stdout: str) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("* ") or stripped.startswith("List of devices"):
                continue
            parts = stripped.split(None, 1)
            if len(parts) == 2:
                rows.append((parts[0], parts[1]))
        return rows

    def _restart_adb_server(self) -> None:
        self._run_adb("kill-server", timeout=5.0)
        self._run_adb("start-server", timeout=10.0)

    @staticmethod
    def _is_adb_server_fault(result: subprocess.CompletedProcess[str]) -> bool:
        if result.returncode != 0:
            return True
        text = f"{result.stdout} {result.stderr}".lower()
        return any(marker in text for marker in _ADB_SERVER_FAULT_MARKERS)

    def check_all(self) -> list[DeviceStatus]:
        return [self.check_robot(), self.check_xr(), self.check_headset()]
