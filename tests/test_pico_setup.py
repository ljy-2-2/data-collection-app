"""Exercise ADB failures without a headset or any robot process."""
import subprocess
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleop_control/src"))

from teleop_control.galaxis import GalaxisHeadsetSetup
from teleop_control.main import prepare_umi_device_check


def headset_fixture(tmp_path, *, reverse_code=0, launch_code=0, mapping=True, running=True):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        code, output, error = 0, "", ""
        if command[-1] == "devices":
            output = "List of devices attached\nPICO123\tdevice\n"
        elif "pidof" in command:
            code, output = (0, "1234\n") if running else (1, "")
        elif command[-2:] == ["reverse", "--list"]:
            output = "usb tcp:63901 tcp:63901\n" if mapping else ""
        elif command[-3:] == ["reverse", "tcp:63901", "tcp:63901"]:
            code, error = reverse_code, "device disconnected" if reverse_code else ""
        elif "monkey" in command:
            code, error = launch_code, "No activities found" if launch_code else ""
        return subprocess.CompletedProcess(command, code, output, error)

    headset = GalaxisHeadsetSetup(run_command=run)
    headset.bundled_apk = tmp_path / "not-installed.apk"
    return headset, commands


def test_existing_usb_tunnel_is_never_removed_or_rebound(tmp_path):
    headset, commands = headset_fixture(tmp_path)
    assert headset.ensure_started(quick=True)
    assert not any("--remove" in cmd or "force-stop" in cmd or "monkey" in cmd for cmd in commands)
    assert [cmd[-2:] for cmd in commands if "reverse" in cmd] == [["reverse", "--list"]]


@pytest.mark.parametrize("reverse_code", [0, 1])
def test_failed_or_unverified_tunnel_cannot_be_reported_as_connected(tmp_path, reverse_code):
    headset, _commands = headset_fixture(tmp_path, mapping=False, reverse_code=reverse_code)
    assert not headset.ensure_started(quick=True)
    assert "USB 数据隧道" in headset.last_error


def test_launch_failure_is_propagated(tmp_path):
    headset, _commands = headset_fixture(tmp_path, running=False, launch_code=1)
    assert not headset.ensure_started(quick=True)
    assert "No activities found" in headset.last_error


@pytest.mark.parametrize("state,expected", [
    ("unauthorized", "重新授权"), ("offline", "离线"),
    ("no permissions (user in plugdev group; are your udev rules wrong?)", "访问权限"),
    ("", "未发现"),
])
def test_preflight_explains_connection_failure_without_false_ready(state, expected):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "List of devices attached\n" +
            (f"PICO123\t{state}\n" if state else ""), "")

    status = prepare_umi_device_check(GalaxisHeadsetSetup(run_command=run))
    assert not status["pico_ready"]
    assert expected in status["pico_detail"]
    assert all(command == ["adb", "devices"] for command in commands)
