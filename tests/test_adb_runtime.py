"""ARM gadget/host ADB isolation, with no USB access in these tests."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "teleop_control/src"))
from teleop_control import adb_runtime
from teleop_control.devices import DeviceDetector
from teleop_control.galaxis import GalaxisHeadsetSetup


@pytest.mark.parametrize("machine", ["aarch64", "arm64"])
def test_arm_defaults_to_local_autostart_socket(machine, monkeypatch):
    monkeypatch.setattr(adb_runtime.platform, "machine", lambda: machine)
    monkeypatch.setattr(adb_runtime.sys, "platform", "linux")
    env = {"UNRELATED": "preserved"}
    adb_runtime.configure_adb_environment(env)
    assert env == {"UNRELATED": "preserved", "ADB_SERVER_SOCKET": "tcp:15037"}
    adb_runtime.configure_adb_environment(env)
    assert env["ADB_SERVER_SOCKET"] == "tcp:15037"


@pytest.mark.parametrize("explicit", [
    {"ADB_SERVER_SOCKET": "tcp:localhost:8765"}, {"ANDROID_ADB_SERVER_PORT": "7500"},
    {"ANDROID_ADB_SERVER_ADDRESS": "custom-host"},
])
def test_explicit_adb_endpoint_is_honored(explicit, monkeypatch):
    monkeypatch.setattr(adb_runtime.platform, "machine", lambda: "aarch64")
    before = dict(explicit)
    adb_runtime.configure_adb_environment(explicit)
    assert explicit == before


@pytest.mark.parametrize("machine,system", [("x86_64", "linux"), ("arm64", "darwin")])
def test_pc_and_nonlinux_defaults_remain_unchanged(machine, system, monkeypatch):
    monkeypatch.setattr(adb_runtime.platform, "machine", lambda: machine)
    monkeypatch.setattr(adb_runtime.sys, "platform", system)
    assert adb_runtime.configure_adb_environment({}) == {}


DEVICES = "List of devices attached\nPICO123\tdevice\nemulator-5554\tdevice\n"


def test_headset_probe_and_wake_select_usb_pico_with_board_emulator():
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, DEVICES if command[-1] == "devices" else "1234\n", "")

    headset = GalaxisHeadsetSetup(run_command=run)
    assert headset.probe_ready()
    assert headset.wake()
    assert commands[-1] == ["adb", "-s", "PICO123", "shell", "input", "keyevent", "KEYCODE_WAKEUP"]
    assert DeviceDetector._adb_rows(DEVICES) == [("PICO123", "device")]


def test_emulator_alone_is_not_a_connected_headset():
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "List of devices attached\nemulator-5554\tdevice\n", "")

    assert not GalaxisHeadsetSetup(run_command=run).probe_ready()
    assert DeviceDetector(run_command=run).check_headset().status == "offline"


@pytest.mark.parametrize("component", ["umi", "tianji"])
def test_both_collector_and_tianji_resolve_one_usb_pico_despite_emulator(component, monkeypatch):
    source = ROOT / ("umi_collection/vendor/galaxis/tools/galaxis_collection.py" if component == "umi"
                     else "teleop_control/vendor/tianji/teleop/marvin_teleop/launcher.py")
    spec = importlib.util.spec_from_file_location("pico_selection_" + component, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if component == "umi":
        monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs:
            subprocess.CompletedProcess(command, 0, DEVICES, ""))
        assert module.discover_pico_serial(None) == "PICO123"
    else:
        pico = module.PicoLauncher({"serial": ""})
        monkeypatch.setattr(pico, "adb", lambda *args, **kwargs: DEVICES)
        assert pico.devices() == "PICO123"


def test_umi_worker_inherits_selected_endpoint(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from teleop_control import umi
    from teleop_control.models import UmiStartRequest

    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:15037")
    manager = umi.UmiManager(tmp_path, tmp_path / "data", python=sys.executable,
                             runtime_dir=tmp_path / "runtime")
    launch = []

    def popen(command, **kwargs):
        launch.append(kwargs["env"])
        return SimpleNamespace(pid=123456, poll=lambda: None)

    monkeypatch.setattr(umi.subprocess, "Popen", popen)
    monkeypatch.setattr(manager, "_save_session_state", lambda _: None)
    manager._start_with_encoder_handoff(UmiStartRequest(task="fixture"), tmp_path / "collector.py")
    assert launch[0]["ADB_SERVER_SOCKET"] == "tcp:15037"
