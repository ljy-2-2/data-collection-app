"""Offline tests for relocating robot dependencies and selecting recipient NICs."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "teleop_control/src"))

from teleop_control import gdk_env, main, runtime_paths
from teleop_control.controller import MANUAL_JOINTS, TeleopController
from teleop_control.devices import DeviceDetector
from teleop_control.models import DeviceStatus, MoveRequest, StartSessionRequest


@pytest.fixture
def relocated_runtime(tmp_path, monkeypatch):
    app = tmp_path / "recipient application/app"
    app.mkdir(parents=True)
    runtime = app.parent / "runtime/robot"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python3").write_text("bundled interpreter")
    (runtime / "gdk").mkdir()
    (runtime / "xr").mkdir()
    (runtime / "xr/runService.sh").write_text("bundled XR launcher")
    monkeypatch.setattr(runtime_paths, "APP_ROOT", app)
    monkeypatch.delenv("TELEOP_XR_SERVICE", raising=False)
    monkeypatch.delenv("TELEOP_XR_LAUNCHER", raising=False)
    return runtime


def test_runtime_resolves_installed_and_per_user_workspace_layout(relocated_runtime, tmp_path, monkeypatch):
    runtime = relocated_runtime
    assert runtime_paths.resolve_robot_python() == str(runtime / "bin/python3")
    assert runtime_paths.resolve_gdk_root() == str(runtime / "gdk")
    assert runtime_paths.resolve_xr_service() == str(runtime / "xr/runService.sh")
    workspace = tmp_path / "another user's workspace"
    workspace.mkdir()
    (workspace / ".runtime").symlink_to(runtime.parent, target_is_directory=True)
    monkeypatch.setattr(runtime_paths, "APP_ROOT", workspace)
    assert runtime_paths.robot_runtime() == runtime


def test_explicit_custom_paths_take_priority_even_if_missing(relocated_runtime):
    assert runtime_paths.resolve_robot_python("~/custom/python") == str(Path.home() / "custom/python")
    assert runtime_paths.resolve_gdk_root("/operator/custom/gdk") == "/operator/custom/gdk"
    assert runtime_paths.resolve_xr_service("/operator/custom/xr.sh") == "/operator/custom/xr.sh"


@pytest.mark.parametrize("exists", [False, True])
def test_legacy_default_paths_migrate_only_if_missing(relocated_runtime, monkeypatch, exists):
    legacy_xr = runtime_paths.LEGACY_XR_SERVICE
    legacy_gdk = Path("~/.cache/agibot/app").expanduser()
    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: exists if path in (Path(legacy_xr), legacy_gdk) else original_exists(path))
    assert runtime_paths.resolve_gdk_root("~/.cache/agibot/app") == str(
        legacy_gdk if exists else relocated_runtime / "gdk"
    )
    assert runtime_paths.resolve_xr_service(legacy_xr) == (
        legacy_xr if exists else str(relocated_runtime / "xr/runService.sh")
    )


def test_xr_explicit_argument_then_service_then_legacy_env(relocated_runtime, monkeypatch):
    monkeypatch.setenv("TELEOP_XR_LAUNCHER", "/legacy/custom/runService.sh")
    assert runtime_paths.resolve_xr_service() == "/legacy/custom/runService.sh"
    monkeypatch.setenv("TELEOP_XR_SERVICE", "/preferred/custom/runService.sh")
    assert runtime_paths.resolve_xr_service() == "/preferred/custom/runService.sh"
    assert runtime_paths.resolve_xr_service("/cli/runService.sh") == "/cli/runService.sh"


def test_main_cli_paths_override_bundle_and_expand_home(relocated_runtime, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["teleop-control", "--gdk-python", "~/chosen/python",
                                    "--gdk-app-root", "~/chosen/gdk", "--xr-service", "~/chosen/xr.sh"])
    args = main.parse_args()
    assert args.gdk_python == str(Path.home() / "chosen/python")
    assert args.gdk_app_root == str(Path.home() / "chosen/gdk")
    assert args.xr_service == str(Path.home() / "chosen/xr.sh")


def rows(*interfaces):
    return "\n".join(f"{i + 2}: {name}    inet {ip}/24 brd 10.42.1.255 scope global {name}"
                     for i, (name, ip) in enumerate(interfaces))


@pytest.mark.parametrize("output,expected", [
    (rows(("enp5s0", "10.42.1.5"), ("wlan0", "192.168.1.6")), "enp5s0"),
    (rows(("usb0@if3", "10.42.1.6")), "usb0"),
    (rows(("enp5s0", "10.42.1.5"), ("enp5s0", "10.42.1.6")), "enp5s0"),
    (rows(("enp5s0", "10.42.1.5"), ("usb0", "10.42.1.6")), ""),
    (rows(("wlan0", "10.42.10.5")), ""),
    ("", ""),
])
def test_auto_network_requires_one_matching_interface(output, expected):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=output))
    assert gdk_env.detect_robot_interface("auto", run_command=run) == expected
    assert run.call_args.kwargs["timeout"] == 2


def test_explicit_interface_does_not_run_auto_detection():
    run = Mock(side_effect=AssertionError("must honor selected interface"))
    assert gdk_env.detect_robot_interface("selected0", run_command=run) == "selected0"
    run.assert_not_called()


@pytest.mark.parametrize("error", [FileNotFoundError("ip"), subprocess.TimeoutExpired("ip", 2)])
def test_auto_network_failures_do_not_choose_an_interface(error):
    assert gdk_env.detect_robot_interface("auto", run_command=Mock(side_effect=error)) == ""


@pytest.mark.parametrize("output", ["", rows(("enp5s0", "10.42.1.5"), ("usb0", "10.42.1.6"))])
def test_unresolved_network_clears_old_discovery_address_without_empty_device_query(monkeypatch, output):
    monkeypatch.setattr(gdk_env.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stdout=output)))
    query = Mock(side_effect=AssertionError("no unique NIC to query"))
    monkeypatch.setattr(gdk_env.subprocess, "check_output", query)
    env = {"LOCATOR_IP": "10.42.1.99", "TELEOP_AORTA_DISCOVERY_URI": "http://10.42.1.123:2379"}
    gdk_env.refresh_discovery_env(env)
    assert "LOCATOR_IP" not in env
    assert env["AORTA_DISCOVERY_URI"] == "http://10.42.1.123:2379"
    query.assert_not_called()


def test_discovery_refresh_reads_new_address_and_preserves_explicit_interface(monkeypatch):
    monkeypatch.setattr(gdk_env.subprocess, "run", Mock(side_effect=AssertionError("explicit NIC")))
    query = Mock(return_value=rows(("selected0", "10.42.1.21")))
    monkeypatch.setattr(gdk_env.subprocess, "check_output", query)
    env = {"LOCATOR_IP": "10.42.1.99"}
    gdk_env.refresh_discovery_env(env, "selected0")
    assert env["LOCATOR_IP"] == "10.42.1.21"
    assert env["TELEOP_ROBOT_INTERFACE"] == "selected0"
    assert query.call_args.args[0][-2:] == ["dev", "selected0"]


@pytest.mark.parametrize("output,ready", [
    (rows(("enp8s0", "10.42.1.25")), True),
    (rows(("enp8s0", "10.42.1.25"), ("usb0", "10.42.1.26")), False),
    ("", False),
])
def test_device_detector_uses_injected_recipient_network(tmp_path, output, ready):
    interface = tmp_path / "enp8s0"
    interface.mkdir()
    for key, value in {"carrier": "1", "operstate": "up", "speed": "1000"}.items():
        (interface / key).write_text(value)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=output, stderr=""))
    detector = DeviceDetector(sysfs_root=tmp_path, run_command=run)
    status = detector.check_robot()
    assert (status.status == "connected") is ready
    assert run.call_count == (2 if ready else 1)


@pytest.fixture
def sdk_tree(tmp_path):
    root = tmp_path / "fake-sdk"
    (root / "gdk/lib/agibot_gdk").mkdir(parents=True)
    (root / "lib").mkdir()
    return root


def test_explicit_empty_base_environment_does_not_import_host_settings(sdk_tree, monkeypatch):
    monkeypatch.setenv("BUILDER_ONLY_MARKER", "not-for-the-child")
    monkeypatch.setattr(gdk_env.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stdout="")))
    assert "BUILDER_ONLY_MARKER" not in gdk_env.build_gdk_env(str(sdk_tree), base_env={})


@pytest.mark.parametrize("mode", ["teleop", "move"])
@pytest.mark.parametrize("available", [True, False])
def test_actual_control_launch_refreshes_address_and_rejects_network_races(monkeypatch, mode, available):
    discovered = rows(("enp5s0", "10.42.1.55")) if available else rows(
        ("enp5s0", "10.42.1.55"), ("usb0", "10.42.1.56")
    )
    monkeypatch.setattr(gdk_env.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stdout=discovered)))
    monkeypatch.setattr(gdk_env.subprocess, "check_output", Mock(return_value=discovered))
    process = SimpleNamespace(pid=12345, poll=lambda: None)
    spawn = Mock(return_value=process)
    detector = SimpleNamespace(check_all=lambda: [
        DeviceStatus(id=key, name=key, status="connected") for key in ("robot", "xr", "headset")
    ])
    controller = TeleopController(detector=detector, teleop_command=["fake-teleop"],
                                 move_command=["fake-move"], process_factory=spawn,
                                 process_env={"LOCATOR_IP": "10.42.1.99"}, robot_interface="auto")
    def launch():
        if mode == "teleop":
            return controller.start_session(StartSessionRequest(robot_model="agibot-g2", mode="arm"))
        return controller.move(MoveRequest(command_id="isolated", robot_model="agibot-g2", mode="arm",
                                           joint_names=MANUAL_JOINTS, joint_positions=[0.] * len(MANUAL_JOINTS)))
    if available:
        launch()
        assert spawn.call_args.kwargs["env"]["LOCATOR_IP"] == "10.42.1.55"
    else:
        with pytest.raises(HTTPException) as error:
            launch()
        assert error.value.status_code == 409
        spawn.assert_not_called()


def test_legacy_runner_cannot_pick_first_interface_or_reuse_stale_locator(sdk_tree, monkeypatch):
    path = ROOT / "teleop_control/vendor/run_teleop_collection.py"
    spec = importlib.util.spec_from_file_location("isolated_legacy_teleop", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setenv("TELEOP_ROBOT_INTERFACE", "auto")
    monkeypatch.setenv("LOCATOR_IP", "10.42.1.99")
    monkeypatch.setattr(gdk_env.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=0, stdout=rows(("enp5s0", "10.42.1.55"), ("usb0", "10.42.1.56")))))
    with pytest.raises(SystemExit, match="unique G2 interface"):
        runner.build_gdk_env(str(sdk_tree))
