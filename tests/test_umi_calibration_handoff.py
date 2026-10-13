"""Keep confirmed raw-radian minima intact through setup and collector launch."""
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "teleop_control/src"))

from teleop_control import umi
from teleop_control.models import UmiDeviceConfigureRequest, UmiStartRequest, UmiStatus


@pytest.fixture
def limits(tmp_path, monkeypatch):
    path = tmp_path / "encoder_limits.yaml"
    monkeypatch.setattr(umi, "ENCODER_LIMITS_PATH", path)
    return path


def save_limits(path, minima):
    path.write_text(yaml.safe_dump({"encoder_node": {"ros__parameters": {
        f"{side}_min_raw": value for side, value in minima.items()
    }}}))


@pytest.mark.parametrize("profile,sides", [
    ("dual", ("left", "right")), ("single-left", ("left",)),
    ("single-right", ("right",)),
])
def test_load_confirmed_minima_without_converting_degrees(limits, profile, sides):
    minima = {"left": math.radians(278.46), "right": math.radians(-11.27)}
    save_limits(limits, minima)
    assert umi._read_encoder_minima(profile) == {side: minima[side] for side in sides}


@pytest.mark.parametrize("content", [
    None, "", "[1, 2]", "encoder_node: [", "encoder_node: {}",
    "encoder_node:\n  ros__parameters:\n    left_min_raw: 1.0\n",
    "encoder_node:\n  ros__parameters:\n    left_min_raw: .nan\n    right_min_raw: 0\n",
    "encoder_node:\n  ros__parameters:\n    left_min_raw: 1\n    right_min_raw: .inf\n",
    "encoder_node:\n  ros__parameters:\n    left_min_raw: true\n    right_min_raw: 0\n",
    'encoder_node:\n  ros__parameters:\n    left_min_raw: "1.2"\n    right_min_raw: 0\n',
])
def test_missing_or_invalid_calibration_never_defaults_to_absolute_zero(limits, content):
    if content is not None:
        limits.write_text(content)
    with pytest.raises(RuntimeError, match="零点标定缺失或无效"):
        umi._read_encoder_minima("dual")


def test_actual_zero_is_valid_and_single_arm_needs_only_its_minimum(limits):
    save_limits(limits, {"right": 0})
    assert umi._read_encoder_minima("single-right") == {"right": 0.0}
    with pytest.raises(RuntimeError, match="零点标定缺失"):
        umi._read_encoder_minima("dual")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from teleop_control.camera_resolution import parse_modes
    modes = parse_modes("[0]: 'MJPG'\nSize: Discrete 640x480\nInterval: Discrete 0.033s (30.000 fps)")
    monkeypatch.setattr(umi, 'read_modes', lambda _: modes)
    instance = umi.UmiManager(tmp_path, tmp_path / "data", python=sys.executable,
                              runtime_dir=tmp_path / "runtime")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "galaxis_collection.py").touch()
    (tools / "web_device_config.py").touch()
    monkeypatch.setattr(instance, "check", lambda *_: UmiStatus(monitor={"device_check": {
        "ready": True, "hardware_ready": True,
        "camera_candidates": ["/dev/video0", "/dev/video2", "/dev/video4"],
        "camera_candidate_details": [dict(device=f"/dev/video{i}", vendor="test", product="camera", serial=str(i), usb_path=str(i)) for i in (0,2,4)],
        "encoder_candidates": ["/dev/ttyUSB0", "/dev/ttyUSB1"],
    }}))
    monkeypatch.setattr(instance, "_udev_properties", lambda _: {})
    return instance


@pytest.mark.parametrize("changed", [False, True])
def test_setup_preserves_confirmation_instead_of_recapturing_open_gripper(
    limits, manager, monkeypatch, changed
):
    confirmed = {"left": math.radians(278.46), "right": math.radians(-11.27)}
    # The user may have opened the gripper after confirming MIN. The helper
    # must persist the submitted raw reading, not the newer preview reading.
    manager._encoder_values = {side: {"value": value + 0.3}
                               for side, value in confirmed.items()}

    def configure(command, **kwargs):
        payload = json.loads((manager.runtime_dir / "device-selection.json").read_text())
        assert payload["encoder_minima"] == confirmed
        persisted = dict(confirmed)
        if changed:
            persisted["left"] += 0.3
        save_limits(limits, persisted)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(umi.subprocess, "run", configure)
    request = UmiDeviceConfigureRequest(camera_roles={
        "left": "/dev/video0", "right": "/dev/video2", "center": "/dev/video4"
    }, encoder_minima=confirmed)
    if changed:
        with pytest.raises(RuntimeError, match="零点与确认的最小值不一致"):
            manager.configure(request)
    else:
        manager.configure(request)
        assert manager._configured_encoder_minima == ("dual", confirmed)


def test_start_rejects_missing_minimum_before_launch(limits, manager, monkeypatch):
    monkeypatch.setattr(umi.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(RuntimeError, match="零点标定缺失"):
        manager.start(UmiStartRequest())
    assert manager._process is None


def test_start_rejects_zero_changed_after_confirmation(limits, manager, monkeypatch):
    manager._configured_encoder_minima = ("dual", {"left": 4.8, "right": -0.2})
    save_limits(limits, {"left": 4.9, "right": -0.2})
    monkeypatch.setattr(umi.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(RuntimeError, match="零点或采集模式已改变"):
        manager.start(UmiStartRequest())


def test_start_accepts_confirmed_nonzero_minima(limits, manager, monkeypatch):
    minima = {"left": 4.8, "right": -0.2}
    manager._configured_encoder_minima = ("dual", minima)
    save_limits(limits, minima)
    monkeypatch.setattr(manager, "_start_with_encoder_handoff", lambda *a: "started")
    assert manager.start(UmiStartRequest()) == "started"


def test_cannot_recalibrate_while_collector_uses_previous_zero(manager):
    manager._process = SimpleNamespace(poll=lambda: None)
    with pytest.raises(RuntimeError, match="先结束 UMI 采集服务"):
        manager.configure(UmiDeviceConfigureRequest(camera_roles={},
                          encoder_minima={"left": 4.8, "right": -0.2}))
