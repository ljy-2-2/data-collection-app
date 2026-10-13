"""Offline tests: never launch hardware or contact the platform."""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleop_control" / "src"))

from teleop_control.controller import TeleopController
from teleop_control.models import DeviceStatus, StartSessionRequest


def request(**kwargs):
    return StartSessionRequest(robot_model="agibot-g2", mode="arm", **kwargs)


@pytest.mark.parametrize("task", ["", "   ", "x" * 121, "task\nname", "task\x00name"])
def test_task_rejects_empty_long_or_control_characters(task):
    with pytest.raises(ValidationError):
        request(task=task)


def test_task_trims_whitespace_and_legacy_request_is_compatible():
    assert request(task="  双臂抓取 杯子  ").task == "双臂抓取 杯子"
    assert request().task is None
    assert request().scale == request().max_joint_vel == 0.5


def test_unknown_robot_is_not_executed_as_g2():
    with pytest.raises(ValidationError):
        StartSessionRequest(robot_model="future-robot", mode="arm", task="测试")


@pytest.mark.parametrize("legacy_task", [True, False])
def test_task_reaches_runner_without_changing_other_arguments(legacy_task):
    commands = []
    process = SimpleNamespace(pid=12345, poll=lambda: None)
    def spawn(command, **kwargs):
        commands.append(command)
        assert not kwargs.get("shell")
        return process

    base = ["python", "runner.py"]
    if legacy_task:
        base += ["--task", "web-teleop"]
    base += ["--out", "/tmp/teleop-test-output"]
    controller = TeleopController(
        detector=SimpleNamespace(check_all=lambda: [
            DeviceStatus(id=id, name=id, status="connected")
            for id in ("robot", "xr", "headset")
        ]),
        teleop_command=base.copy(), move_command=["move.py"],
        process_factory=spawn,
        process_stopper=lambda child: None,
    )
    started = controller.start_session(request(task="  双臂抓取 杯子  ", extra_cameras=["head_depth"]))
    command = commands[0]
    assert command.count("--task") == 1
    assert command[command.index("--task") + 1] == "双臂抓取 杯子"
    assert command[command.index("--out") + 1] == "/tmp/teleop-test-output"
    assert command[command.index("--scale") + 1] == "0.5"
    assert command[command.index("--max-joint-vel") + 1] == "0.5"
    assert command[command.index("--extra-camera") + 1] == "head_depth"
    assert command[command.index("--session-id") + 1] == started.session_id
    assert controller.teleop_command == base
    controller.stop_session(started.session_id)
    assert controller.status()[1].state == "idle"
