"""Offline tests only: no SDK robot connection, enabling, camera or XR starts."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleop_control/src"))
from teleop_control.main import MockController
from teleop_control.tianji import TianjiManager, tianji_router
from teleop_control.tianji_models import TianjiMove, TianjiParameters, TianjiStart
from teleop_control.tianji_worker import (
    ROOT,
    Controller,
    Models,
    configuration,
    manual_plan,
)


# Vendor imports follow the worker's explicit local SDK bootstrap.
from marvin_teleop.protocol import Frame  # noqa: E402
from marvin_teleop.robot import SimRobot  # noqa: E402
from gripper.damiao import Simulation  # noqa: E402
from gripper.runtime import Control as GripperControl  # noqa: E402
from gripper.runtime import refresh_disabled_feedback  # noqa: E402


@pytest.fixture(scope="module")
def models():
    return Models(configuration(ROOT / "teleop/marvin_teleop/config.json"))


@pytest.mark.parametrize("age_ms", [140, 160])
def test_gripper_timeout_keeps_150ms_limit_and_reports_age(monkeypatch, age_ms):
    motors = json.loads((ROOT / "teleop/gripper/config.json").read_text())["motors"]
    motors = {"left": motors["left"]}
    driver = Simulation(motors)
    driver.poll = Mock()  # No fresh reply arrives during this cycle.
    driver.feedback["left"].received = 500 - age_ms / 1000
    control = GripperControl(motors, driver, 0.2)
    control.context = (1, 0)
    sample = dict(submitted=500, received=500, context=(1, 0),
                  valid=True, emergency=False, triggers={"left": 0})
    monkeypatch.setattr("gripper.runtime.time.monotonic", lambda: 500)
    if age_ms < 150:
        control.step(sample, 500)
        assert control.states["left"]["state"] == "READY"
    else:
        with pytest.raises(RuntimeError) as error:
            control.step(sample, 500)
        assert "反馈年龄=160.0 ms，上限=150 ms，阶段=WAIT_RELEASE" in str(error.value)
        assert control.states["left"]["state"] == "WAIT_RELEASE"


@pytest.mark.parametrize("missing_side", [None, "left", "right"])
def test_dual_gripper_start_refreshes_stale_preflight_without_enabling(monkeypatch, missing_side):
    motors = json.loads((ROOT / "teleop/gripper/config.json").read_text())["motors"]
    clock = [500.0]
    monkeypatch.setattr("gripper.runtime.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("gripper.runtime.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    driver = Simulation(motors)
    # Finishing the right-side register reads outlasted the left watchdog.
    driver.feedback["left"].received = clock[0] - 0.196
    driver.feedback["right"].received = clock[0] - 0.01
    pending = {}
    def disable(side):
        pending[side] = clock[0] + 0.01
    def poll():
        for side, arrival in list(pending.items()):
            if side != missing_side and clock[0] >= arrival:
                driver.feedback[side].state = 0
                driver.feedback[side].received = clock[0]
                del pending[side]
    driver.disable = Mock(side_effect=disable)
    driver.poll = poll
    driver.enable = Mock(side_effect=AssertionError("Startup must not enable a motor"))
    driver.send_position = Mock(side_effect=AssertionError("Startup must not command a target"))
    sample = dict(submitted=500, received=500, context=(1, 0),
                  valid=True, emergency=False, triggers={"left": 0, "right": 0})
    control = GripperControl(motors, driver, 0.2)
    control.context = (1, 0)
    with pytest.raises(RuntimeError, match="left: motor feedback timeout"):
        control.step(sample, clock[0])
    if missing_side:
        with pytest.raises(RuntimeError, match="startup disabled feedback timeout: " + missing_side):
            refresh_disabled_feedback(driver, motors)
        assert clock[0] <= 500.151
    else:
        refresh_disabled_feedback(driver, motors)
        control.step(sample, clock[0])
        assert {state["state"] for state in control.states.values()} == {"READY"}
    driver.enable.assert_not_called()
    driver.send_position.assert_not_called()


def test_gripper_start_rejects_enabled_acknowledgement(monkeypatch):
    motors = json.loads((ROOT / "teleop/gripper/config.json").read_text())["motors"]
    motors = {"left": motors["left"]}
    clock = [500.0]
    monkeypatch.setattr("gripper.runtime.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("gripper.runtime.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    driver = Simulation(motors)
    driver.disable = Mock()  # The motor fails to acknowledge disable.
    driver.feedback["left"].state = 1
    with pytest.raises(RuntimeError, match="startup disabled feedback timeout: left"):
        refresh_disabled_feedback(driver, motors)


@pytest.mark.parametrize("mode", ["left", "right", "dual"])
def test_only_selected_arms_are_enabled_and_commanded(models, mode):
    cfg = configuration(ROOT / "teleop/marvin_teleop/config.json")
    selected = ("left", "right") if mode == "dual" else (mode,)
    cfg["controlled_sides"] = selected
    robot = SimRobot()
    robot.enable = Mock(wraps=robot.enable)
    robot.command = Mock(wraps=robot.command)
    ctrl = Controller(models, robot, cfg)
    ctrl.start_session()
    assert robot.enabled_sides == set(selected)
    ctrl._dispatch(robot.read(), time.monotonic())
    assert set(robot.command.call_args.args[0]) == set(selected)
    now = time.monotonic()
    frame = Frame(
        now,
        1,
        1,
        now,
        True,
        True,
        {s: np.eye(4) for s in ("left", "right")},
        np.eye(4),
        {"left": 0, "right": 0},
        False,
    )
    ctrl.tick(frame, True, False, now)
    assert robot.enabled_sides == set(selected)
    assert not ctrl.fault


def test_single_arm_zero_keeps_other_arm_in_path(models):
    cfg = configuration(ROOT / "teleop/marvin_teleop/config.json")
    measured = {"left": np.array([2.0, 0, 0, 0, 0, 0, 0]), "right": np.zeros(7)}
    plan = manual_plan(models, measured, TianjiMove(kind="zero", mode="left"), cfg)
    assert np.array_equal(plan["goal"]["left"], np.zeros(7))
    assert np.array_equal(plan["goal"]["right"], measured["right"])
    assert plan["sides"] == ("left",)
    assert plan["duration"] >= 1


@pytest.mark.parametrize("speed,accel", [(180, 450), (1.5, 2)])
@pytest.mark.parametrize("kind,distance,accel_cap,previous_speed", [
    ("zero", 10, 5, 5), ("pose", 10, 4, 4),
    ("zero", 60, 5, 5), ("pose", 60, 4, 4),
    ("jog", 1, 4, 4), ("jog", 10, 4, 4),
])
def test_manual_move_bounds_ten_degree_speed_and_existing_acceleration(models, speed, accel, kind, distance, accel_cap, previous_speed):
    cfg = configuration(ROOT / "teleop/marvin_teleop/config.json")
    cfg.update(max_joint_speed_deg_s=speed, max_joint_accel_deg_s2=accel)
    measured = {"left": np.array([float(distance), 0, 0, 0, 0, 0, 0]), "right": np.zeros(7)}
    request = TianjiMove(kind=kind, mode="left", side="left", joint=1, delta=-distance if kind == "jog" else 0,
                         targets={"left": [0.] * 7} if kind == "pose" else None)
    plan = manual_plan(models, measured, request, cfg)
    t = plan["duration"]
    u = np.linspace(0, 1, 1001)
    peak_v = np.max(np.abs(distance * (30*u**2 - 60*u**3 + 30*u**4) / t))
    peak_a = np.max(np.abs(distance * (60*u - 180*u**2 + 120*u**3) / t**2))
    assert peak_v <= min(10, speed)
    assert peak_a <= min(accel_cap, accel)
    if speed == 180:
        previous_duration = 1.05 * max(1, 1.875 * distance / previous_speed,
                                       np.sqrt(10 / np.sqrt(3) * distance / accel_cap))
        assert t <= previous_duration
        if distance == 60:
            assert t < previous_duration
            assert peak_v > previous_speed


@pytest.mark.parametrize("delta", [-10, 10])
def test_ten_degree_jog_checks_manufacturer_limit(models, delta):
    cfg = configuration(ROOT / "teleop/marvin_teleop/config.json")
    q = {"left": np.zeros(7), "right": np.zeros(7)}
    request = TianjiMove(kind="jog", mode="left", side="left", joint=1, delta=delta)
    plan = manual_plan(models, q, request, cfg)
    assert plan["goal"]["left"][0] == delta
    assert np.array_equal(plan["goal"]["right"], q["right"])
    q["left"][0] = 169.5 if delta > 0 else -169.5
    with pytest.raises(ValueError, match="限位"):
        manual_plan(models, q, request, cfg)


@pytest.mark.parametrize("delta", [-10.01, 10.01, float("nan"), float("inf")])
def test_manual_jog_rejects_steps_beyond_ten_degrees(delta):
    with pytest.raises(ValidationError):
        TianjiMove(kind="jog", mode="left", side="left", joint=1, delta=delta)


def test_reject_joint_limits_unselected_arm_and_bad_pose(models):
    cfg = configuration(ROOT / "teleop/marvin_teleop/config.json")
    q = {"left": np.zeros(7), "right": np.zeros(7)}
    with pytest.raises(ValueError, match="未在当前模式"):
        manual_plan(
            models, q, TianjiMove(kind="jog", mode="left", side="right", delta=0.5), cfg
        )
    with pytest.raises(ValueError, match="限位"):
        manual_plan(
            models,
            q,
            TianjiMove(
                kind="pose", mode="left", targets={"left": [180, 0, 0, 0, 0, 0, 0]}
            ),
            cfg,
        )
    with pytest.raises(ValueError, match="7 个"):
        manual_plan(
            models, q, TianjiMove(kind="pose", mode="left", targets={"left": [0]}), cfg
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("kp", float("nan")),
        ("kd", 0),
        ("scale", 0),
        ("max_joint_speed_deg_s", 181),
        ("max_joint_accel_deg_s2", 451),
        ("left_gripper_speed", 101),
    ],
)
def test_invalid_parameters_rejected(field, value):
    with pytest.raises(ValidationError):
        TianjiParameters(**{field: value})


def test_defaults_match_updated_sdk():
    p = TianjiParameters()
    c = json.loads((ROOT / "teleop/marvin_teleop/config.json").read_text())
    g = json.loads((ROOT / "teleop/gripper/config.json").read_text())
    assert p.right_gripper_speed == g["motors"]["right"]["speed_rad_s"]
    assert p.scale == c["scale"]
    assert p.left_gripper_speed == g["motors"]["left"]["speed_rad_s"]
    # Calibrated endpoint coordinates can be positive or negative. Validate
    # actual travel limits instead of assuming a particular motor zero offset.
    from gripper.config import load as load_gripper_config
    validated = load_gripper_config(ROOT / "teleop/gripper/config.json", live=True)
    assert validated["motors"]["right"]["open_rad"] == g["motors"]["right"]["open_rad"]
    assert p.kp == g["motors"]["left"]["kp"] == g["motors"]["right"]["kp"]
    assert p.kd == g["motors"]["left"]["kd"] == g["motors"]["right"]["kd"]


@pytest.mark.parametrize("upload_mode", ["left", "right", "dual"])
def test_upload_selection_preserves_dual_arm_control(models, upload_mode):
    request = TianjiStart(task="test", mode="dual", upload_mode=upload_mode)
    cfg = configuration(ROOT / "teleop/marvin_teleop/config.json")
    cfg["controlled_sides"] = ("left", "right") if request.mode == "dual" else (request.mode,)
    robot = SimRobot()
    ctrl = Controller(models, robot, cfg)
    ctrl.start_session()
    assert robot.enabled_sides == {"left", "right"}
    assert set(ctrl.arms) == {"left", "right"}


def test_upload_selection_cannot_include_uncontrolled_arm():
    with pytest.raises(ValidationError, match="上传曲线"):
        TianjiStart(task="test", mode="left", upload_mode="right")


def test_status_renews_operator_lease_and_stale_feedback_goes_offline(monkeypatch):
    manager = TianjiManager(MockController())
    manager.value = {
        "state": "running",
        "joints": {"left": [0] * 7},
        "robot_ready": True,
    }
    manager.received = time.monotonic() - 10
    monkeypatch.setattr(manager, "active", lambda: True)
    monkeypatch.setattr(manager, "_write", Mock())
    value = manager.snapshot()
    assert value["state"] == "unknown"
    assert value["joints"] == {}
    assert all(d["status"] == "offline" for d in value["devices"])
    assert value["recording"] is False
    manager._write.assert_called_once_with({"action": "heartbeat"})
    monkeypatch.setattr(manager, "active", lambda: False)


@pytest.mark.parametrize("previous_state,expected", [
    (None, "checking"), ("idle", "checking"),
    ("running", "unknown"), ("moving", "unknown"), ("error", "unknown"),
])
def test_device_check_distinguishes_initial_connection_from_lost_motion_feedback(
    monkeypatch, previous_state, expected,
):
    manager = TianjiManager(MockController())
    connected = [previous_state is not None]
    manager.value = {} if previous_state is None else {"state": previous_state}
    manager.received = time.monotonic() - 10
    monkeypatch.setattr(manager, "active", lambda: connected[0])
    monkeypatch.setattr(manager, "_launch", lambda: connected.__setitem__(0, True))

    def waiting_for_devices(action, payload):
        assert action == "check"
        status = manager.snapshot(touch=False)
        assert status["state"] == expected
        assert all(d["status"] == "offline" for d in status["devices"])
        raise RuntimeError("simulated check timeout")

    monkeypatch.setattr(manager, "_command", waiting_for_devices)
    try:
        with pytest.raises(RuntimeError, match="simulated check timeout"):
            manager.command("check")
        # Failed initialization must not mask uncertain state indefinitely.
        assert manager.snapshot(touch=False)["state"] == "unknown"
    finally:
        connected[0] = False


def test_completed_device_check_returns_idle(monkeypatch):
    manager = TianjiManager(MockController())
    connected = [False]
    monkeypatch.setattr(manager, "active", lambda: connected[0])
    monkeypatch.setattr(manager, "_launch", lambda: connected.__setitem__(0, True))

    def checked(action, payload):
        assert manager.snapshot(touch=False)["state"] == "checking"
        manager.value = {"state": "idle"}
        manager.received = time.monotonic()
        return {"ok": True}

    monkeypatch.setattr(manager, "_command", checked)
    try:
        assert manager.command("check") == {"ok": True}
        assert manager.snapshot(touch=False)["state"] == "idle"
        manager.received -= 10
        assert manager.snapshot(touch=False)["state"] == "unknown"
    finally:
        connected[0] = False


def test_diagnostics_never_renews_operator_lease_or_starts_hardware(monkeypatch):
    manager = TianjiManager(MockController())
    manager.last_access = 123.0
    monkeypatch.setattr(manager, "active", lambda: True)
    monkeypatch.setattr(manager, "_write", Mock())
    monkeypatch.setattr(manager, "start_cameras", Mock())
    manager._diagnostic_event("preview_error", error="Camera missing or busy: usb-2")
    app = FastAPI()
    app.include_router(tianji_router(manager))
    with TestClient(app) as client:
        for _ in range(3):
            value = client.get("/tianji/diagnostics").json()
            assert value["last_operator_access"] == 123.0
            assert value["events"][-1]["error"] == "Camera missing or busy: usb-2"
    assert manager.last_access == 123.0
    manager._write.assert_not_called()
    manager.start_cameras.assert_not_called()
    monkeypatch.setattr(manager, "active", lambda: False)


def test_camera_diagnostics_preserve_failed_end_before_frames_clear(monkeypatch):
    from types import SimpleNamespace
    manager = TianjiManager(MockController())
    end = dict(type="end", ok=False, native_exit=1,
               native_failure_reason="Camera has no new frame for 2 seconds; stop")
    def stream(**kwargs):
        yield SimpleNamespace(metadata=dict(type="image", camera_id="usb-1", format="mjpeg"), data=b"jpeg")
        yield SimpleNamespace(metadata=end)
        raise RuntimeError(end["native_failure_reason"])
    from unittest.mock import MagicMock
    client = MagicMock()
    client.__enter__.return_value = client
    client.status.return_value = {"cooldown_seconds": 2, "idle": True, "max_stream_seconds": 600}
    client.list_cameras.return_value = [{"id": c, "available": True} for c in ("usb-1", "usb-2", "usb-4")]
    client.stream.side_effect = stream
    monkeypatch.setattr(manager, "new_camera_client", lambda: client)
    manager._cameras()
    assert manager.frames == {}
    value = manager.diagnostics()
    assert value["camera_phase"] == "fault"
    assert next(e for e in value["events"] if e["kind"] == "preview_end")["metadata"] == end
    assert any(e["kind"] == "preview_first_frame" for e in value["events"])


def preview_client():
    from unittest.mock import MagicMock
    client = MagicMock()
    client.__enter__.return_value = client
    client.status.return_value = dict(idle=True, fault=None, max_stream_seconds=600, cooldown_seconds=2)
    client.clock_sync.return_value = dict(boot_id='test-boot', pc_midpoint_ns=1, offset_ns=2, uncertainty_ns=3)
    client.list_cameras.return_value = [{"id": c, "available": True} for c in ("usb-1", "usb-2", "usb-4")]
    return client


def test_missing_camera_never_opens_or_latches_remote_stream(monkeypatch):
    manager = TianjiManager(MockController())
    client = preview_client()
    client.list_cameras.return_value[1]["available"] = False
    monkeypatch.setattr(manager, "new_camera_client", lambda: client)
    manager._cameras()
    client.stream.assert_not_called()
    client.recover_camera.assert_not_called()
    assert "右腕相机（usb-2）" in manager.camera_error
    assert "未启动三路取流" in manager.camera_error
    # A subsequent explicit preview request can proceed after enumeration
    # returns; the previous preflight did not latch a remote stream fault.
    client.list_cameras.return_value[1]["available"] = True
    assert manager._prepare_preview(client) == 600


@pytest.mark.parametrize("recoverable", [False, True])
def test_preview_never_clears_remote_fault(recoverable):
    manager = TianjiManager(MockController())
    client = preview_client()
    client.status.return_value.update(fault="camera gap", recoverable_camera_fault=recoverable)
    with pytest.raises(RuntimeError, match="本地重连无法解除"):
        manager._prepare_preview(client)
    client.recover_camera.assert_not_called()
    client.stream.assert_not_called()


def test_preview_respects_service_duration_limit():
    manager = TianjiManager(MockController())
    client = preview_client()
    client.status.return_value['max_stream_seconds'] = 180
    assert manager._prepare_preview(client) == 180


def test_longer_preview_keeps_operator_lease_stop(monkeypatch):
    from types import SimpleNamespace
    manager = TianjiManager(MockController())
    client = preview_client()
    monkeypatch.setattr("teleop_control.tianji.time.monotonic", lambda: 500.0)
    manager.last_access = 499.0
    def stream(**kwargs):
        manager.last_access = 490.0  # UI disappears during the stream.
        yield SimpleNamespace(metadata=dict(type="image", camera_id="usb-1", format="mjpeg"), data=b"jpeg")
        yield SimpleNamespace(metadata=dict(type="end", ok=True))
    client.stream.side_effect = stream
    client.request_stop.side_effect = manager.camera_stop.set
    monkeypatch.setattr(manager, "new_camera_client", lambda: client)
    manager._cameras()
    client.stream.assert_called_once_with(seconds=600, mode="teleop_observation")
    assert client.request_stop.called
    assert manager.frames == {}
    assert manager.camera_phase == "idle"


@pytest.mark.parametrize('armed', [False, True])
def test_manager_adopts_running_preview_and_waits_for_its_real_end(monkeypatch, armed):
    from fusion_camera import StreamEvent
    manager = TianjiManager(MockController())
    manager.collection.armed = armed
    client = preview_client()
    holder = {}
    monkeypatch.setattr(manager, "new_camera_client", lambda: client)

    def stream(**kwargs):
        yield StreamEvent(dict(type="start",boot_id="test-boot",observation_mode="teleop_observation",graceful_stop=True))
        for camera in ("usb-1", "usb-2", "usb-4"):
            yield StreamEvent(dict(type="image",camera_id=camera,format="mjpeg"),b"before A")
        yield StreamEvent(dict(type="joints"))
        holder['adopted'] = manager.adopt_camera_stream()
        holder['adopted'].validate_stream()
        client.request_stop.assert_not_called()
        yield StreamEvent(dict(type="image",camera_id="usb-1",format="mjpeg"),b"after A")
        holder['adopted'].request_stop()  # B still stops the actual native stream.
        yield StreamEvent(dict(type="end",ok=True,native_exit=0,stop_reason="client_stop"))

    client.stream.side_effect = stream
    manager._cameras()
    events = list(holder['adopted'].stream())
    assert [e.metadata['type'] for e in events] == ['start','image','end']
    assert events[1].data == b'after A'
    assert events[-1].metadata['native_exit'] == 0
    client.stream.assert_called_once()
    client.request_stop.assert_called_once()
    assert manager.camera_session.finished.is_set()
    assert manager.camera_phase == ('resuming' if armed else 'idle')
    assert bool(manager.frames) == armed
    if armed:
        assert manager.frames['usb_1'][0] == b'after A'
    holder['adopted'].close()


@pytest.mark.parametrize("recoverable", [True, False])
def test_interrupted_preview_does_not_recover_remote_service(monkeypatch, recoverable):
    from types import SimpleNamespace
    from fusion_camera import CameraError
    manager = TianjiManager(MockController())
    client = preview_client()
    monkeypatch.setattr(manager, "new_camera_client", lambda: client)
    end = dict(type="end", ok=False, error="Invalid MJPEG frame or dimensions; stop",
               recoverable_camera_fault=recoverable)
    def stream(**kwargs):
        yield SimpleNamespace(metadata=end)
        raise CameraError(end)
    client.stream.side_effect = stream
    manager._cameras()
    client.recover_camera.assert_not_called()
    client.stream.assert_called_once()
    assert manager.camera_phase == "fault"
    assert manager.frames == {}


def test_parameter_storage_and_api_validation_are_robot_specific(tmp_path, monkeypatch):
    from teleop_control import tianji

    monkeypatch.setattr(tianji, "RUNTIME", tmp_path)
    manager = TianjiManager(MockController())
    app = FastAPI()
    app.include_router(tianji_router(manager))
    with TestClient(app) as client:
        assert client.get("/tianji/status").json()["state"] == "idle"
        assert client.post("/tianji/parameters", json={"scale": 0.7}).status_code == 200
        assert client.get("/tianji/parameters").json()["scale"] == 0.7
        assert (
            client.post(
                "/tianji/start", json={"task": "test", "mode": "body"}
            ).status_code
            == 422
        )
        assert client.post("/tianji/parameters", json={"kp": -1}).status_code == 422
        assert client.get("/tianji/cameras/head_color/frame").status_code == 404
        assert client.get("/tianji/cameras/usb_1/frame").status_code == 503
    assert not manager.active()


def test_mock_mode_cannot_open_real_hardware():
    manager = TianjiManager(MockController())
    with pytest.raises(RuntimeError, match="模拟模式"):
        manager.command("check")
    assert manager.process is None


def test_camera_stream_uses_cached_jpeg_and_closes_without_device_commands():
    import asyncio
    from types import SimpleNamespace

    manager = TianjiManager(MockController())
    manager.frames["usb_1"] = (b"\xff\xd8frame\xff\xd9", time.monotonic())
    route = next(r for r in tianji_router(manager).routes if r.path.endswith("/stream"))

    async def disconnected():
        return False

    async def read_one():
        response = await route.endpoint(
            "usb_1", SimpleNamespace(is_disconnected=disconnected), True
        )
        chunk = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return response, chunk

    response, chunk = asyncio.run(read_one())
    assert response.media_type == "application/octet-stream"
    assert chunk.endswith(b"\xff\xd8frame\xff\xd9\r\n")
    assert not manager.active()


@pytest.mark.parametrize('has_first_frame', [False, True])
@pytest.mark.parametrize('resuming', [False, True])
def test_collection_preview_waits_between_frames_without_preview_thread(monkeypatch, has_first_frame, resuming):
    import asyncio
    from types import SimpleNamespace
    manager = TianjiManager(MockController())
    monkeypatch.setattr(manager.collection, 'active', lambda: not resuming)
    manager.camera_phase = 'resuming' if resuming else 'idle'
    now = time.monotonic()
    if has_first_frame:
        manager.frames['usb_1'] = (b'first', now)
    sleeps = []
    async def tick(_seconds):
        sleeps.append(1)
        if len(sleeps) == 2:
            manager.frames['usb_1'] = (b'next', now + 0.01)
    monkeypatch.setattr('teleop_control.tianji.asyncio.sleep', tick)
    async def disconnected(): return False
    route = next(r for r in tianji_router(manager).routes if r.path.endswith('/stream'))
    async def consume():
        response = await route.endpoint('usb_1', SimpleNamespace(is_disconnected=disconnected), True)
        stream = response.body_iterator
        if has_first_frame:
            assert (await anext(stream)).endswith(b'first\r\n')
        assert (await anext(stream)).endswith(b'next\r\n')
        await stream.aclose()
    asyncio.run(consume())
    assert len(sleeps) == 2
    monkeypatch.setattr(manager.collection, 'active', lambda: False)


@pytest.mark.parametrize('armed,expired,cancelled', [(True, False, False), (False, False, False), (True, True, False), (True, False, True)])
def test_preview_resume_once_after_cleanup_with_live_operator(monkeypatch, armed, expired, cancelled):
    from types import SimpleNamespace
    manager = TianjiManager(MockController())
    manager.camera_session = SimpleNamespace(attached=True)
    manager.camera_thread = Mock()
    manager.camera_thread.is_alive.return_value = False
    manager.camera_phase = 'resuming'
    manager.collection.armed = armed
    manager.last_access = time.monotonic() - (9 if expired else 0)
    manager.start_cameras = Mock()
    manager.camera_stop.wait = Mock(return_value=cancelled)
    # Synchronous scheduling tests the release order without real cameras/waits.
    monkeypatch.setattr('teleop_control.tianji.threading.Thread',
                        lambda **kw: SimpleNamespace(start=kw['target']))
    manager.resume_cameras_after_episode()
    manager.resume_cameras_after_episode()
    manager.camera_thread.join.assert_called_once_with(timeout=12)
    manager.camera_stop.wait.assert_called_once_with(manager.camera_cooldown + .1)
    assert manager.start_cameras.call_count == int(armed and not expired and not cancelled)
