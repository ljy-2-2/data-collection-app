"""Offline geometry and fake SDK tests; never connect to a robot or CAN."""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "teleop_control/vendor/tianji/teleop"))
from marvin_teleop import gripper_collision as collision
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models, prepare_box, prepared_clearance
from marvin_teleop.robot import Robot, CollisionBlocked


@pytest.fixture(scope="module")
def models():
    return Models(configuration(ROOT / "teleop_control/vendor/tianji/teleop/marvin_teleop/config.json"))


def pose(x=0):
    return {"left": np.array([x, 0., 0., 0., 0., 0., 0.]), "right": np.zeros(7)}


def synthetic_guard(gaps):
    guard = collision.GripperCollisionGuard.__new__(collision.GripperCollisionGuard)
    guard.clearances = lambda q, openings=None: np.asarray(gaps(q["left"][0]), dtype=float)
    guard.reach = {"left": 100., "right": 100.}
    guard.names = ["moving", "obstacle"]
    guard.box_sides = ["left", "right"]
    guard.articulation = {"left": {}, "right": {}}
    guard.pairs = [(0, 1)] * len(gaps(0))
    guard.labels = [str(i) for i in range(len(gaps(0)))]
    return guard


def test_inside_reserve_only_hold_or_retreat_and_no_reentry():
    guard = synthetic_guard(lambda x: [x - .035])
    assert guard.reason(pose(0), pose(0)) is None
    assert guard.reason(pose(0), pose(.02)) is None
    assert guard.reason(pose(.02), pose(.01))
    assert guard.reason(pose(.02), pose(.04)) is None
    assert guard.reason(pose(.04), pose(0))


def test_each_pair_has_its_own_floor():
    guard = synthetic_guard(lambda x: [-.01 + x, -.01 - x])
    assert guard.reason(pose(0), pose(.001))
    assert guard.reason(pose(0), pose(-.001))


def test_clear_endpoints_cannot_tunnel_through_a_thin_obstacle():
    guard = synthetic_guard(lambda x: [abs(x - .037) - .00001])
    assert guard.clearances(pose(0))[0] > 0
    assert guard.clearances(pose(.1))[0] > 0
    assert guard.reason(pose(0), pose(.1))


def test_urdf_padding_is_on_each_moving_link_and_camera(models):
    for side, arm in models.arms.items():
        letter = "L" if side == "left" else "R"
        tree = ET.parse(arm.path)
        boxes = collision.protection_bounds(arm.path, letter)
        assert len(boxes) == 5 and f"camera_{letter}" in boxes
        for name, (center, half) in boxes.items():
            parent = f"gripper_{letter}" if name.startswith("camera") else name
            node = tree.find(f"link[@name='{parent}']/collision[@name='{collision.PREFIX + name}']")
            assert node is not None
            np.testing.assert_allclose(np.fromstring(node.find("geometry/box").get("size"), sep=" "), half * 2)
            assert np.all(half > .035)
        # A reduced/missing reserve fails before any hardware connection.
        assert len(collision.load_protection(arm.path, letter)[1]) == 5


def test_jaws_follow_opening_and_camera_follows_arm(models, monkeypatch):
    seen = []
    original = collision.pair_clearances
    def capture(boxes, pairs):
        seen.append(boxes)
        return original(boxes, pairs)
    monkeypatch.setattr(collision, "pair_clearances", capture)
    guard = models.gripper_guard
    guard.clearances(pose(), {"left": 0, "right": 0})
    guard.clearances(pose(), {"left": 1, "right": 0})
    guard.clearances(pose(20), {"left": 1, "right": 0})
    jaw = guard.names.index("gripper_L_1")
    camera = guard.names.index("camera_L")
    assert not np.allclose(seen[0][jaw][0], seen[1][jaw][0])
    np.testing.assert_allclose(seen[0][camera][0], seen[1][camera][0])
    assert not np.allclose(seen[1][camera][0], seen[2][camera][0])
    assert all("left/Link7" not in label for label in guard.labels if "gripper_L" in label)
    assert any("left/Link6" in label for label in guard.labels if "gripper_L" in label)


def test_vectorized_sat_agrees_with_scalar(models, monkeypatch):
    original = collision.pair_clearances
    def check(boxes, pairs):
        result = original(boxes, pairs)
        expected = [prepared_clearance(prepare_box(boxes[a]), prepare_box(boxes[b])) for a, b in pairs]
        np.testing.assert_allclose(result, expected, atol=1e-12)
        return result
    monkeypatch.setattr(collision, "pair_clearances", check)
    models.gripper_guard.clearances(pose(45), {"left": .43, "right": .76})


def test_stale_or_missing_opening_uses_full_sweep(models, monkeypatch):
    seen = []
    monkeypatch.setattr(collision, "pair_clearances", lambda boxes, pairs: seen.append(boxes) or np.ones(len(pairs)))
    guard = models.gripper_guard
    guard.clearances(pose(), {})
    fallback = seen[-1]
    for opening in np.linspace(0, 1, 11):
        guard.clearances(pose(), {"left": opening, "right": opening})
        for i, name in enumerate(guard.names):
            if name not in ("gripper_L_1", "gripper_L_17", "gripper_R_1", "gripper_R_17"):
                continue
            t, center, half = seen[-1][i]
            ft, fc, fh = fallback[i]
            signs = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)])
            points = (center + signs * half) @ t[:3, :3].T + t[:3, 3]
            local = (points - ft[:3, 3]) @ ft[:3, :3]
            assert np.all(np.abs(local - fc) <= fh + 1e-10)
    robot = Robot.__new__(Robot)
    robot.gripper_feedback = {"left": (.8, time.monotonic() - 1, .15),
                              "right": (.2, time.monotonic(), .15)}
    assert robot.collision_openings()["left"] is None
    assert robot.collision_openings()["right"] == .2


def test_sdk_boundary_replaces_blocked_target_with_measured_hold():
    robot = Robot.__new__(Robot)
    robot.safety_lock = threading.RLock()
    robot.gripper_feedback, robot.gripper_targets = {}, {}
    robot.joint_targets = {}
    robot.gripper_guard = synthetic_guard(lambda x: [x - .035])
    robot.enabled_sides = {"left", "right"}
    robot.config = {"arm_channels": {"left": "A", "right": "B"}}
    robot.q = pose(.01)
    robot.read = Mock(return_value=robot.q)
    robot._clear_buffer = Mock()
    robot.api = Mock()
    with pytest.raises(CollisionBlocked, match="只能远离"):
        robot.command({"left": pose(0)["left"]})
    assert robot.api.send_cmd.call_count == 1
    holds = dict(call.args for call in robot.api.set_joint_cmd_pose.call_args_list)
    assert holds == {"A": robot.q["left"].tolist(), "B": robot.q["right"].tolist()}
    robot.api.reset_mock()
    robot.command({"left": pose(.02)["left"]})
    assert robot.api.set_joint_cmd_pose.call_args.args == ("A", pose(.02)["left"].tolist())
    robot.read.side_effect = ValueError("stale feedback")
    robot.api.reset_mock()
    with pytest.raises(ValueError, match="stale"):
        robot.command({"left": pose(.03)["left"]})
    robot.api.send_cmd.assert_not_called()


def test_motor_rejection_holds_without_fault_and_reverse_remains_possible():
    from gripper.runtime import Control
    motor = dict(open_rad=0., closed_rad=1., travel_margin_rad=.01, speed_rad_s=1.,
                 accel_rad_s2=10., feedback_timeout_s=.15, max_temperature_c=60)
    feedback = SimpleNamespace(received=10., state=1, mos_c=20, rotor_c=20, position=.5)
    driver = Mock(feedback={"left": feedback})
    control = Control({"left": motor}, driver, .2)
    control.states["left"].update(state="ACTIVE", target_rad=.5)
    control.collision_check = lambda side, current, target: "只能远离" if target > current else None
    control._motor_step("left", motor, {"triggers": {"left": 1.}}, 10, 10, .01)
    assert driver.send_position.call_args.args == ("left", .5)
    assert not control.fault and control.states["left"]["state"] == "ACTIVE"
    control._motor_step("left", motor, {"triggers": {"left": 0.}}, 10, 10, .01)
    assert driver.send_position.call_args.args[1] < .5


@pytest.mark.parametrize("opened,closed", [(-.7, .82), (1.7, .18)])
def test_opening_uses_calibrated_motor_endpoints(opened, closed):
    motor = dict(open_rad=opened, closed_rad=closed)
    assert collision.normalized_opening(motor, opened) == 1
    assert collision.normalized_opening(motor, closed) == 0
    assert collision.normalized_opening(motor, (opened + closed) / 2) == pytest.approx(.5)


def test_teleop_collision_hold_keeps_clutch_and_reanchors_for_reverse(models):
    from marvin_teleop.control import Controller, ArmControl
    ctrl = Controller.__new__(Controller)
    ctrl.models = models
    ctrl.arms = {s: ArmControl(mode="ACTIVE") for s in models.arms}
    frame = SimpleNamespace(hands={s: np.eye(4) for s in models.arms}, sequence=7)
    ctrl._collision_hold(pose(), frame, "只能远离")
    for side, arm in ctrl.arms.items():
        assert arm.mode == "ACTIVE" and arm.target_clamped
        np.testing.assert_array_equal(arm.commanded, pose()[side])
        np.testing.assert_array_equal(arm.velocity, np.zeros(7))
        np.testing.assert_allclose(arm.anchor[1], models.arms[side].fk(pose()[side]))


def test_worker_rejected_manual_move_keeps_connection_and_accepts_next_move(monkeypatch, tmp_path):
    import io
    import json
    import queue
    from teleop_control import tianji_worker as worker
    commands = queue.Queue()
    robots = []
    phase = ["check"]
    plans = []

    def enqueue(action, **extra):
        commands.put(json.dumps(dict(action=action, id=action, **extra)) + "\n")

    class Input:
        def __iter__(self):
            while (line := commands.get(timeout=5)) is not None:
                yield line

    class FakeRobot:
        def __init__(self, config):
            self.q = pose()
            self.velocities = pose()
            self.enabled = False
            self.pending_sides = {}
            self.stop_calls = 0
            self.send_calls = 0
            self.last_command = None
            robots.append(self)
        def wait_ready(self): pass
        def read(self): return self.q
        def enable(self, sides): self.enabled = True
        def stop(self): self.stop_calls += 1; self.enabled = False
        def close(self): self.stop()
        def command(self, q, deadline=None): self.send_calls += 1

    def plan(models, measured, request, config):
        plans.append(request)
        if len(plans) == 1:
            raise CollisionBlocked("只能远离")
        return dict(start=pose(), goal=pose(), delta=pose(), sides=("left",),
                    duration=10., elapsed=0., enabled_at=time.monotonic())

    class Publisher:
        def __init__(self, **kwargs): pass
        def close(self): pass
        def submit(self, row, *args):
            if not robots or not row["joints_deg"]:
                return
            request = dict(kind="jog", mode="left", side="left", joint=1, delta=1)
            if phase[0] == "check":
                phase[0] = "blocked"
                enqueue("move", payload=request)
            elif phase[0] == "blocked" and plans:
                assert row["state"] == "idle" and robots[0].stop_calls == 0
                phase[0] = "retry"
                enqueue("move", payload={**request, "delta": -1})
            elif phase[0] == "retry" and robots[0].send_calls:
                assert len(robots) == 1 and robots[0].stop_calls == 0
                phase[0] = "done"
                enqueue("close")

    monkeypatch.setattr(worker, "__file__", str(tmp_path / "src/teleop_control/tianji_worker.py"))
    monkeypatch.setattr(worker.sys, "stdin", Input())
    monkeypatch.setattr(worker.sys, "stdout", io.StringIO())
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "load_configuration", lambda: {"collision_margin_m": .015, "arm_geometry": "nominal"})
    monkeypatch.setattr(worker, "Models", lambda config: SimpleNamespace(
        validate=lambda: None, mapping_report={}, collision_reason=lambda *a: None))
    monkeypatch.setattr(worker, "validate_preview", lambda *args: None)
    monkeypatch.setattr(worker, "RecordingRobot", FakeRobot)
    monkeypatch.setattr(worker, "Publisher", Publisher)
    monkeypatch.setattr(worker, "manual_plan", plan)
    monkeypatch.setattr(worker, "PicoLauncher", lambda config: SimpleNamespace(
        start=lambda: None, close=lambda: None, app_started=True, status="fixture"))
    monkeypatch.setattr(worker, "LocalVRConnection", lambda *a, **kw: SimpleNamespace(
        check=lambda: None, close=lambda: None, snapshot=lambda: (None, False, "", True)))
    enqueue("check")
    try:
        worker.main()
    finally:
        commands.put(None)
    assert phase[0] == "done" and len(plans) == 2
