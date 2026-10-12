"""Hardware brightness is validated, confirmed and retained across UMI setup."""

import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import httpx
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "teleop_control/src"))

from teleop_control import app as app_module
from teleop_control import camera_brightness as hardware
from teleop_control import umi
from teleop_control.main import MockController
from teleop_control.models import UmiDeviceConfigureRequest, UmiStatus


def control_line(minimum=-63, maximum=255, step=3, default=0, value=0):
    return (
        "                     brightness 0x00980900 (int)    : "
        f"min={minimum} max={maximum} step={step} default={default} value={value}\n"
    )


@pytest.fixture
def driver(monkeypatch):
    state = SimpleNamespace(listing=control_line(), readback="brightness: 0\n", calls=[])

    def run(command, **kwargs):
        assert command[:2] == ["v4l2-ctl", "--device"]
        assert kwargs == {"capture_output": True, "text": True, "check": False, "timeout": 2.0}
        state.calls.append(command)
        operation = command[3]
        if operation == "--list-ctrls":
            output = state.listing
        elif operation == "--set-ctrl":
            output = ""
        elif operation == "--get-ctrl=brightness":
            output = state.readback
        else:
            pytest.fail(f"unexpected V4L2 operation: {command}")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(hardware.subprocess, "run", run)
    return state


@pytest.mark.parametrize("minimum,maximum,step,default,value", [
    (-63, 255, 3, 0, 129), (0, 1023, 7, 0, 14),
])
def test_reads_actual_driver_range_without_assuming_a_fixed_slider(
    driver, minimum, maximum, step, default, value
):
    driver.listing = "contrast 0x00980901 (int) : min=0 max=10 value=4\n" + control_line(
        minimum, maximum, step, default, value
    )

    assert hardware.read_brightness("/dev/video8") == {
        "supported": True, "min": minimum, "max": maximum,
        "step": step, "default": default, "value": value,
    }
    assert driver.calls == [["v4l2-ctl", "--device", "/dev/video8", "--list-ctrls"]]


@pytest.mark.parametrize("listing", [
    "", "contrast 0x00980901 (int) : min=0 max=10 step=1 default=0 value=4\n",
    control_line(step=0), control_line(step=-1), control_line(minimum=255),
    control_line().replace("step=3 ", ""),
])
def test_reports_unsupported_or_malformed_controls_without_writing(driver, listing):
    driver.listing = listing
    result = hardware.read_brightness("/dev/video0")
    assert result["supported"] is False
    assert result["message"]
    assert len(driver.calls) == 1


@pytest.mark.parametrize("flag", ["read-only", "inactive", "disabled"])
def test_unwritable_control_cannot_be_changed(driver, flag):
    driver.listing = control_line().rstrip() + f" flags={flag}\n"
    with pytest.raises(RuntimeError, match="不支持亮度调节"):
        hardware.set_brightness("/dev/video0", 0)
    assert all("--set-ctrl" not in call for call in driver.calls)


@pytest.mark.parametrize("value", [-63, 0, 129, 255])
def test_set_checks_step_from_driver_minimum_and_verifies_actual_readback(driver, value):
    driver.readback = f"brightness: {value}\n"

    result = hardware.set_brightness("/dev/video2", value)

    assert result["supported"] is True
    assert result["value"] == value
    assert driver.calls == [
        ["v4l2-ctl", "--device", "/dev/video2", "--list-ctrls"],
        ["v4l2-ctl", "--device", "/dev/video2", "--set-ctrl", f"brightness={value}"],
        ["v4l2-ctl", "--device", "/dev/video2", "--get-ctrl=brightness"],
    ]


@pytest.mark.parametrize("value", [-64, 256, 1, True, False, 3.0, 3.5, "3"])
def test_invalid_range_step_or_type_never_writes_the_camera(driver, value):
    with pytest.raises(ValueError, match="步长为 3 的整数"):
        hardware.set_brightness("/dev/video0", value)
    assert driver.calls == [["v4l2-ctl", "--device", "/dev/video0", "--list-ctrls"]]


def test_step_is_relative_to_driver_minimum_instead_of_zero(driver):
    driver.listing = control_line(minimum=-64, maximum=62, step=3, default=-1, value=-1)
    driver.readback = "brightness: -61\n"
    assert hardware.set_brightness("/dev/video0", -61)["value"] == -61

    driver.calls.clear()
    with pytest.raises(ValueError, match="步长为 3 的整数"):
        hardware.set_brightness("/dev/video0", 0)
    assert driver.calls == [["v4l2-ctl", "--device", "/dev/video0", "--list-ctrls"]]


@pytest.mark.parametrize("readback,message", [
    ("brightness: 126\n", "相机实际亮度为 126"),
    ("", "未返回亮度确认值"),
    ("brightness: 129.5\n", "未返回亮度确认值"),
])
def test_unconfirmed_hardware_value_is_reported_as_failure(driver, readback, message):
    driver.readback = readback
    with pytest.raises(RuntimeError, match=message):
        hardware.set_brightness("/dev/video0", 129)


@pytest.mark.parametrize("error,message", [
    (FileNotFoundError("v4l2-ctl"), "需要 v4l2-ctl"),
    (OSError("device disconnected"), "请检查相机连接"),
    (subprocess.TimeoutExpired("v4l2-ctl", 2), "请检查相机连接"),
])
def test_command_startup_and_timeout_failures_are_actionable(monkeypatch, error, message):
    def run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(hardware.subprocess, "run", run)
    with pytest.raises(RuntimeError, match=message):
        hardware.read_brightness("/dev/video0")


@pytest.mark.parametrize("failed_operation", ["--list-ctrls", "--set-ctrl", "--get-ctrl=brightness"])
def test_nonzero_v4l2_exit_is_not_reported_as_success(monkeypatch, failed_operation):
    def run(command, **_kwargs):
        if command[3] == failed_operation:
            return SimpleNamespace(returncode=1, stdout="", stderr="driver rejected control")
        return SimpleNamespace(returncode=0, stdout=control_line(), stderr="")

    monkeypatch.setattr(hardware.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="driver rejected control"):
        hardware.set_brightness("/dev/video0", 0)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from teleop_control.camera_resolution import parse_modes
    modes = parse_modes("[0]: 'MJPG'\nSize: Discrete 640x480\nInterval: Discrete 0.033s (30.000 fps)")
    monkeypatch.setattr(umi, 'read_modes', lambda _: modes)
    instance = umi.UmiManager(tmp_path, tmp_path / "data", python=sys.executable,
                              runtime_dir=tmp_path / "runtime")
    instance._device_check = {"camera_candidates": ["/dev/video0", "/dev/video2", "/dev/video4"], "camera_candidate_details": [dict(device=f"/dev/video{i}", vendor="test", product="camera", serial=str(i), usb_path=str(i)) for i in (0,2,4)]}
    return instance


@pytest.fixture
def manager_controls(monkeypatch):
    calls = []

    def read(device):
        calls.append((device, None))
        return {"supported": True, "min": -64, "max": 255, "step": 1, "default": 0, "value": 12}

    def write(device, value):
        calls.append((device, value))
        return {"supported": True, "min": -64, "max": 255, "step": 1, "default": 0, "value": value}

    monkeypatch.setattr(umi, "read_brightness", read)
    monkeypatch.setattr(umi, "set_brightness", write)
    return calls


@pytest.mark.parametrize("camera_key", ["video1", "video2/../video0", "/dev/video0", "rear", "video0;echo"])
def test_unknown_or_unselected_device_never_reaches_hardware(manager, manager_controls, camera_key):
    with pytest.raises(ValueError, match="camera not found"):
        manager.camera_brightness(camera_key, 20)
    assert manager_controls == []


@pytest.mark.parametrize("collecting,stopping,profile,role", [
    (False, False, "dual", "left"), (True, True, "dual", "center"),
    (True, False, "single-left", "right"), (True, False, "single-right", "left"),
])
def test_inactive_collection_role_never_reaches_hardware(
    manager, manager_controls, collecting, stopping, profile, role
):
    manager._process = SimpleNamespace(poll=lambda: None) if collecting else None
    manager._stopping = stopping
    manager._collection_profile = profile
    with pytest.raises(ValueError, match="未在当前采集模式中启用"):
        manager.camera_brightness(role, 20)
    assert manager_controls == []


@pytest.mark.parametrize("blocker", ["running", "handoff"])
def test_candidate_brightness_is_blocked_during_collection_handoff(manager, manager_controls, blocker):
    if blocker == "running":
        manager._process = SimpleNamespace(poll=lambda: None)
    else:
        manager._encoder_preview_blockers.add("configure")
    with pytest.raises(RuntimeError, match="正在交接采集设备"):
        manager.camera_brightness("video0", 20)
    assert manager_controls == []


@pytest.mark.parametrize("profile,role", [("dual", "left"), ("single-left", "center"), ("single-right", "right")])
def test_active_collection_role_controls_the_corresponding_hardware(manager, manager_controls, profile, role):
    manager._process = SimpleNamespace(poll=lambda: None)
    manager._collection_profile = profile
    assert manager.camera_brightness(role, 42)["value"] == 42
    assert manager_controls == [(f"/dev/galaxis_{role}_camera", 42)]


def test_confirmed_values_are_cached_per_physical_device(manager, manager_controls):
    assert manager.camera_brightness("video0")["value"] == 12
    manager.camera_brightness("video0", -32)
    manager.camera_brightness("video2", 17)
    assert manager._camera_brightness_values == {
        str(Path("/dev/video0").resolve()): -32,
        str(Path("/dev/video2").resolve()): 17,
    }


def test_failed_change_keeps_last_confirmed_value_and_unsupported_control_clears_it(
    manager, manager_controls, monkeypatch
):
    manager.camera_brightness("video0", 17)
    key = str(Path("/dev/video0").resolve())

    def fail(*_args):
        raise RuntimeError("readback differs")

    monkeypatch.setattr(umi, "set_brightness", fail)
    with pytest.raises(RuntimeError, match="readback differs"):
        manager.camera_brightness("video0", 19)
    assert manager._camera_brightness_values[key] == 17

    monkeypatch.setattr(umi, "read_brightness", lambda _: {"supported": False, "message": "unsupported"})
    assert manager.camera_brightness("video0")["supported"] is False
    assert key not in manager._camera_brightness_values


@pytest.mark.parametrize("adjustments,expected", [
    ((("video0", -32), ("video2", 17), ("video4", 82)), {"left": 17, "right": -32, "center": 82}),
    ((("video2", 17),), {"left": 17}),
])
def test_configure_carries_confirmed_device_values_to_selected_roles_after_preview_reset(
    manager, manager_controls, monkeypatch, adjustments, expected
):
    for key, value in adjustments:
        manager.camera_brightness(key, value)
    tools = manager.project_root / "tools"
    tools.mkdir()
    (tools / "web_device_config.py").touch()
    minima = {"left": 4.2, "right": -0.3}
    monkeypatch.setattr(manager, "check", lambda *_: UmiStatus(monitor={"device_check": {
        "ready": True, "hardware_ready": True,
        **manager._device_check, "encoder_candidates": ["/dev/ttyUSB0", "/dev/ttyUSB1"],
    }}))
    monkeypatch.setattr(manager, "_udev_properties", lambda _: {})
    monkeypatch.setattr(umi, "_read_encoder_minima", lambda _: minima)
    monkeypatch.setattr(umi, "read_brightness", lambda *_: pytest.fail("must preserve confirmed preview values"))
    captured = []

    def configure(command, **_kwargs):
        assert command[:2] == ["docker", "run"]
        captured.append(json.loads((manager.runtime_dir / "device-selection.json").read_text()))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(umi.subprocess, "run", configure)
    manager.configure(UmiDeviceConfigureRequest(camera_roles={
        "left": "/dev/video2", "right": "/dev/video0", "center": "/dev/video4",
    }, encoder_minima=minima))

    assert captured[0]["camera_brightness"] == expected
    assert captured[0]["encoder_minima"] == minima
    assert not manager._encoder_preview_blockers


def test_recording_camera_startup_restores_each_confirmed_preview_brightness(
    manager, monkeypatch, tmp_path
):
    """Execute the preview -> saved settings -> recording startup control path."""
    vendor = ROOT / "umi_collection/vendor/galaxis"

    def load_source(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    setup = load_source("_brightness_setup", vendor / "tools/web_device_config.py")
    registry = load_source("_brightness_launch", vendor / "galaxis_bringup/galaxis_bringup/launch_registry.py")
    settings_path = tmp_path / "center_camera.yaml"
    roles = {"left": "/dev/video2", "right": "/dev/video0", "center": "/dev/video4"}
    aliases = {f"/dev/galaxis_{side}_camera": device for side, device in roles.items()}
    confirmed = {"/dev/video0": 23, "/dev/video2": -17, "/dev/video4": 64}
    current_hardware = dict.fromkeys(confirmed, 0)
    writes = []

    def command(command, **_kwargs):
        if command[:2] == ["docker", "run"]:
            payload = json.loads((manager.runtime_dir / "device-selection.json").read_text())
            settings = setup.build_camera_settings(
                tuple(payload["camera_roles"]), payload["camera_brightness"]
            )
            setup.write(settings_path, yaml.safe_dump(settings, sort_keys=False))
            # Reopening the cameras for collection may reset driver controls.
            current_hardware.update(dict.fromkeys(current_hardware, 0))
            return SimpleNamespace(returncode=0)
        assert command[:2] == ["v4l2-ctl", "--device"]
        device = aliases.get(command[2], command[2])
        if command[3] == "--list-ctrls":
            output = control_line(minimum=-64, maximum=255, step=1, value=current_hardware[device])
        elif command[3] == "--set-ctrl":
            current_hardware[device] = int(command[4].split("=", 1)[1])
            writes.append((command[2], current_hardware[device]))
            output = ""
        else:
            assert command[3] == "--get-ctrl=brightness"
            output = f"brightness: {current_hardware[device]}\n"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(hardware.subprocess, "run", command)
    for device, value in confirmed.items():
        assert manager.camera_brightness(Path(device).name, value)["value"] == value
    assert current_hardware == confirmed

    tools = manager.project_root / "tools"
    tools.mkdir()
    (tools / "web_device_config.py").touch()
    minima = {"left": 4.2, "right": -0.3}
    monkeypatch.setattr(manager, "check", lambda *_: UmiStatus(monitor={"device_check": {
        "ready": True, "hardware_ready": True, **manager._device_check,
        "encoder_candidates": ["/dev/ttyUSB0", "/dev/ttyUSB1"],
    }}))
    monkeypatch.setattr(manager, "_udev_properties", lambda _: {})
    monkeypatch.setattr(umi, "_read_encoder_minima", lambda _: minima)
    manager.configure(UmiDeviceConfigureRequest(camera_roles=roles, encoder_minima=minima))
    assert set(current_hardware.values()) == {0}

    effective_parameters = {}

    class Timer:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    class RosNode:
        def __init__(self, _name):
            self.parameters = {}

        def declare_parameter(self, key, default):
            self.parameters[key] = effective_parameters.get(key, default)

        def get_parameter(self, key):
            return SimpleNamespace(value=self.parameters[key])

        def create_timer(self, _interval, _callback):
            return Timer()

        def get_logger(self):
            return SimpleNamespace(info=lambda *_: None,
                                   warning=lambda message: pytest.fail(message),
                                   error=lambda message: pytest.fail(message))

    monkeypatch.setitem(sys.modules, "rclpy", ModuleType("rclpy"))
    monkeypatch.setitem(sys.modules, "rclpy.node", SimpleNamespace(Node=RosNode))
    monkeypatch.syspath_prepend(str(vendor / "galaxis_bringup"))
    controller_module = load_source(
        "galaxis_bringup._brightness_recording_controller",
        vendor / "galaxis_bringup/galaxis_bringup/camera_control_node.py",
    )
    monkeypatch.setattr(controller_module, "Path", lambda _: SimpleNamespace(exists=lambda: True))
    writes.clear()
    for role, physical_device in roles.items():
        device = f"/dev/galaxis_{role}_camera"
        # Follow ROS launch precedence: saved node parameters override defaults.
        sources = registry.with_center_camera_settings(
            [{"video_device": device, "brightness": 0}], settings_path
        )
        effective_parameters.clear()
        for source in sources:
            effective_parameters.update(source if isinstance(source, dict) else
                yaml.safe_load(Path(source).read_text())[
                    f"{role}_camera_brightness_controller"]["ros__parameters"])

        node = controller_module.CameraControlNode()
        node._apply_brightness()
        assert current_hardware[physical_device] == confirmed[physical_device]
        assert not node._timer.cancelled
        node._apply_brightness()
        assert node._timer.cancelled
        assert writes[-2:] == [(device, confirmed[physical_device])] * 2

    assert current_hardware == confirmed


@pytest.fixture
def request_api(monkeypatch):
    # Exercise HTTP validation and routing, without depending on sandbox thread scheduling.
    async def inline_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", inline_thread)

    def request(provider, method, camera_key, body=None):
        app = app_module.create_app(MockController(), cors_origins=[], umi=provider)

        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.request(method, f"/umi/camera-controls/{camera_key}/brightness", json=body)

        return asyncio.run(send())

    return request


def test_api_reads_and_changes_actual_candidate_camera(manager, manager_controls, request_api):
    response = request_api(manager, "GET", "video2")
    assert response.status_code == 200
    assert response.json()["value"] == 12
    response = request_api(manager, "POST", "video2", {"value": -17})
    assert response.status_code == 200
    assert response.json()["value"] == -17
    assert manager_controls == [("/dev/video2", None), ("/dev/video2", -17)]


@pytest.mark.parametrize("body", [{"value": True}, {"value": False}, {"value": 1.5},
                                   {"value": 2.0}, {"value": "2"}, {"value": None}, {}])
def test_api_rejects_noninteger_body_before_hardware(manager, manager_controls, request_api, body):
    response = request_api(manager, "POST", "video0", body)
    assert response.status_code == 422
    assert manager_controls == []


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("error,status", [(ValueError("out of range"), 400), (RuntimeError("device offline"), 503)])
def test_api_returns_hardware_validation_errors(request_api, method, error, status):
    def fail(*_args):
        raise error

    response = request_api(SimpleNamespace(camera_brightness=fail), method, "video0", {"value": 1})
    assert response.status_code == status
    assert response.json()["detail"] == str(error)
