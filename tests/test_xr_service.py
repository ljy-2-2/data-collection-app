"""Exercise foreground and background XR launchers without robot hardware."""

from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleop_control/src"))
from teleop_control import xr_service


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(xr_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(xr_service.time, "sleep", lambda dt: clock.__setitem__(0, clock[0] + dt))
    script = tmp_path / "runService.sh"
    script.write_text("./RoboticsServiceProcess &\n")
    process = Mock()
    factory = Mock(return_value=process)
    service = xr_service.XrServiceLauncher(
        str(script), process_factory=factory,
        port_checker=lambda *_: clock[0] >= 1.0,
        timeout_seconds=2, log_path=tmp_path / "xr.log",
    )
    return service, process, factory


@pytest.mark.parametrize("exit_code", [None, 0])
def test_waits_for_service_after_foreground_or_background_launch(launcher, exit_code):
    service, process, factory = launcher
    process.poll.return_value = exit_code
    assert service.ensure_started() is True
    assert service.ensure_started() is False
    factory.assert_called_once()


def test_failed_launcher_reports_exit_code(launcher):
    service, process, _ = launcher
    process.poll.return_value = 127
    with pytest.raises(xr_service.XrServiceStartError, match="exited with code 127"):
        service.ensure_started()


def test_successful_script_without_listening_service_times_out(launcher):
    service, process, _ = launcher
    process.poll.return_value = 0
    service.port_checker = lambda *_: False
    with pytest.raises(xr_service.XrServiceStartError, match="did not open port 60061"):
        service.ensure_started()


def test_existing_service_needs_no_launcher(launcher):
    service, _, factory = launcher
    service.port_checker = lambda *_: True
    service.script.unlink()
    assert service.ensure_started() is False
    factory.assert_not_called()
