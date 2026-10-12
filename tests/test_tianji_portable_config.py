"""Relocated Tianji settings: no robot, XR service or camera connection."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleop_control/src"))

from teleop_control import tianji, tianji_config
from teleop_control.tianji_worker import load_configuration


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for key in (
        "TIANJI_CONFIG", "TIANJI_ROBOT_IP", "TIANJI_CAMERA_HOST",
        "TIANJI_CAMERA_CREDENTIALS", "TIANJI_PYTHON", "TELEOP_GDK_PYTHON",
        "TELEOP_XR_SERVICE", "TELEOP_XR_LAUNCHER",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tianji_config, "resolve_xr_service", lambda path: path)


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    config = json.loads(tianji_config.DEFAULT_CONFIG.read_text())
    config["robot_ip"] = "192.168.80.25"
    path = tmp_path / "tianji.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("TIANJI_CONFIG", str(path))
    return path


@pytest.mark.parametrize("override", [None, "10.56.1.23"])
def test_worker_and_actual_camera_factory_share_robot_address(
    monkeypatch, settings_file, tmp_path, override
):
    if override:
        monkeypatch.setenv("TIANJI_ROBOT_IP", override)
    credentials = tmp_path / "camera-client.json"
    monkeypatch.setenv("TIANJI_CAMERA_CREDENTIALS", str(credentials))
    factory = Mock()
    monkeypatch.setitem(sys.modules, "fusion_camera", SimpleNamespace(FusionStreamClient=factory))
    config = load_configuration()
    manager = object.__new__(tianji.TianjiManager)
    assert manager.new_camera_client() is factory.return_value
    expected = override or "192.168.80.25"
    assert config["robot_ip"] == expected
    factory.assert_called_once_with(expected, credentials=str(credentials), timeout=5)


def test_separate_camera_host_does_not_change_robot_address(monkeypatch, settings_file):
    monkeypatch.setenv("TIANJI_CAMERA_HOST", "camera-box.local")
    assert tianji_config.camera_host() == "camera-box.local"
    assert load_configuration()["robot_ip"] == "192.168.80.25"


def test_default_address_still_comes_from_bundled_configuration():
    expected = json.loads(tianji_config.DEFAULT_CONFIG.read_text())["robot_ip"]
    assert load_configuration()["robot_ip"] == expected
    assert tianji_config.camera_host() == expected


def test_per_user_credentials_path_expands_on_the_target_computer(monkeypatch, settings_file):
    monkeypatch.setenv("TIANJI_CAMERA_CREDENTIALS", "~/camera-client.json")
    factory = Mock()
    monkeypatch.setitem(sys.modules, "fusion_camera", SimpleNamespace(FusionStreamClient=factory))
    object.__new__(tianji.TianjiManager).new_camera_client()
    assert factory.call_args.kwargs["credentials"] == str(Path.home() / "camera-client.json")
    assert load_configuration()["robot_ip"] == "192.168.80.25"


@pytest.mark.parametrize("invalid", ["", "not-an-ip", "999.1.1.1", True, 123, None])
def test_invalid_configured_robot_address_fails_before_camera_open(
    monkeypatch, settings_file, invalid
):
    config = json.loads(settings_file.read_text())
    config["robot_ip"] = invalid
    settings_file.write_text(json.dumps(config))
    factory = Mock()
    monkeypatch.setitem(sys.modules, "fusion_camera", SimpleNamespace(FusionStreamClient=factory))
    with pytest.raises(ValueError, match="IPv4"):
        object.__new__(tianji.TianjiManager).new_camera_client()
    factory.assert_not_called()


def test_environment_address_is_validated_by_worker(monkeypatch, settings_file):
    monkeypatch.setenv("TIANJI_ROBOT_IP", "wrong-address")
    with pytest.raises(ValueError, match="IPv4"):
        load_configuration()


@pytest.mark.parametrize(
    "service,legacy,expected",
    [("/custom/xr/runService.sh", "/old/xr/runService.sh", "/custom/xr/runService.sh"),
     (None, "/old/xr/runService.sh", "/old/xr/runService.sh"),
     (None, None, "/opt/apps/roboticsservice/runService.sh")],
)
def test_xr_service_overrides_are_shared_with_worker(
    monkeypatch, settings_file, service, legacy, expected
):
    if service:
        monkeypatch.setenv("TELEOP_XR_SERVICE", service)
    if legacy:
        monkeypatch.setenv("TELEOP_XR_LAUNCHER", legacy)
    resolver = Mock(return_value="/relocated/runtime/robot/xr/runService.sh")
    monkeypatch.setattr(tianji_config, "resolve_xr_service", resolver)
    assert load_configuration()["service_dir"] == "/relocated/runtime/robot/xr"
    resolver.assert_called_once_with(expected)


@pytest.mark.parametrize(
    "explicit,shared,bundled,expected",
    [("/custom/tianji-python", "/custom/gdk-python", "/bundle/python", "/custom/tianji-python"),
     (None, "/custom/gdk-python", "/bundle/python", "/custom/gdk-python"),
     (None, None, "/bundle/python", "/bundle/python"),
     (None, None, None, sys.executable)],
)
def test_launch_uses_portable_python_and_same_service_root(
    monkeypatch, settings_file, explicit, shared, bundled, expected
):
    if explicit:
        monkeypatch.setenv("TIANJI_PYTHON", explicit)
    if shared:
        monkeypatch.setenv("TELEOP_GDK_PYTHON", shared)
    monkeypatch.setattr(tianji, "resolve_robot_python", Mock(return_value=bundled))
    monkeypatch.setattr(tianji_config, "resolve_xr_service",
                        Mock(return_value="/relocated/runtime/robot/xr/runService.sh"))
    runtime_env = Mock(side_effect=lambda env, launcher: env)
    monkeypatch.setattr(tianji, "add_xr_runtime_env", runtime_env)
    process = Mock()
    monkeypatch.setattr(tianji.subprocess, "Popen", process)
    monkeypatch.setattr(tianji.threading, "Thread", Mock())
    manager = object.__new__(tianji.TianjiManager)
    manager.controller = SimpleNamespace(hardware_enabled=True)
    manager.require_other_idle = Mock()
    manager._launch()
    assert process.call_args.args[0] == [expected, "-u", "-m", "teleop_control.tianji_worker"]
    assert runtime_env.call_args.args[1] == "/relocated/runtime/robot/xr/runService.sh"
