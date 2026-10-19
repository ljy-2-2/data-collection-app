"""Launch behavior and dependency paths of both official XR service variants."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleop_control/src"))

from teleop_control import gdk_env, runtime_paths, xr_service
from teleop_control.xr_service import XrServiceLauncher, XrServiceStartError


def test_backgrounding_vendor_launcher_waits_for_service_socket(tmp_path, monkeypatch):
    script = tmp_path / "vendor/runService.sh"
    script.parent.mkdir()
    script.touch()
    spawn = Mock(return_value=SimpleNamespace(poll=lambda: 0))
    ready = Mock(side_effect=[False, False, True])
    monkeypatch.setattr(xr_service.time, "sleep", lambda _seconds: None)
    service = XrServiceLauncher(str(script), process_factory=spawn, port_checker=ready,
                                log_path=tmp_path / "logs/xr.log")
    assert service.ensure_started() is True
    assert spawn.call_args.kwargs["cwd"] == str(script.parent)
    assert str(script.parent) in spawn.call_args.kwargs["env"]["LD_LIBRARY_PATH"].split(os.pathsep)


def test_existing_service_does_not_start_another_process(tmp_path):
    spawn = Mock()
    service = XrServiceLauncher(str(tmp_path / "unused.sh"), process_factory=spawn,
                                port_checker=lambda *_: True)
    assert service.ensure_started() is False
    spawn.assert_not_called()


def test_failed_launcher_reports_actual_exit_code(tmp_path):
    script = tmp_path / "runService.sh"
    script.touch()
    service = XrServiceLauncher(str(script),
        process_factory=Mock(return_value=SimpleNamespace(poll=lambda: 127)),
        port_checker=lambda *_: False, log_path=tmp_path / "xr.log")
    with pytest.raises(XrServiceStartError, match="code 127"):
        service.ensure_started()


def test_successful_shell_without_service_is_not_reported_ready(tmp_path, monkeypatch):
    script = tmp_path / "runService.sh"
    script.touch()
    monkeypatch.setattr(xr_service.time, "monotonic", Mock(side_effect=[0, 0, 3]))
    monkeypatch.setattr(xr_service.time, "sleep", lambda _seconds: None)
    service = XrServiceLauncher(str(script), timeout_seconds=2,
        process_factory=Mock(return_value=SimpleNamespace(poll=lambda: 0)),
        port_checker=lambda *_: False, log_path=tmp_path / "xr.log")
    with pytest.raises(XrServiceStartError, match="did not open port 60061"):
        service.ensure_started()


@pytest.mark.parametrize("arch,sdk", [("aarch64", "arm64"), ("x86_64", "x64")])
@pytest.mark.parametrize("workspace", [False, True])
def test_xr_env_finds_relocated_private_libraries_and_matching_sdk(tmp_path, monkeypatch, arch, sdk, workspace):
    app = tmp_path / "install/app"
    support = app / ".runtime/xr-support" if workspace else app.parent / "runtime/xr-support"
    support.mkdir(parents=True)
    vendor = tmp_path / "vendor"
    for path in (vendor / "lib", vendor / "SDK/arm64", vendor / "SDK/x64"):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runtime_paths, "APP_ROOT", app)
    monkeypatch.setattr(gdk_env.platform, "machine", lambda: arch)
    env = {"LD_LIBRARY_PATH": "/custom", "QT_PLUGIN_PATH": "/desktop/qt"}
    gdk_env.add_xr_runtime_env(env, str(vendor / "runService.sh"))
    libraries = env["LD_LIBRARY_PATH"].split(os.pathsep)
    assert libraries[0] == str(support)
    assert str(vendor / "SDK" / sdk) in libraries
    assert str(vendor / "SDK" / ("x64" if sdk == "arm64" else "arm64")) not in libraries
    assert libraries[-1] == "/custom"
    assert env["QT_PLUGIN_PATH"] == str(vendor / "plugins")
    assert env["QT_QPA_PLATFORM"] == "offscreen"
