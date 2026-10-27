"""Single owner of the Tianji SDK; nonblocking telemetry feeds App collection."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .tianji_models import TianjiMove, TianjiParameters, TianjiStart
from .tianji_config import robot_configuration
from .tianji_vr import LocalVRConnection

ROOT = Path(__file__).resolve().parents[2] / "vendor" / "tianji"
sys.path.insert(0, str(ROOT / "teleop"))
# The vendor root is explicit, independent of the launch directory.
os.environ["MARVIN_SDK_ROOT"] = str(ROOT)
from gripper.config import load as load_grippers, travel_bounds  # noqa: E402
from gripper.runtime import Grippers  # noqa: E402
from marvin_teleop.configuration import configuration  # noqa: E402
from marvin_teleop.control import Controller  # noqa: E402
from marvin_teleop.launcher import PicoLauncher, ensure_service  # noqa: E402
from marvin_teleop.model import Models  # noqa: E402
from marvin_teleop.preview import validate_preview  # noqa: E402
from marvin_teleop.robot import Robot  # noqa: E402
from marvin_teleop.recovery import ZeroRecovery  # noqa: E402
from marvin_teleop.xr_input import SDKInput  # noqa: E402

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[3] / "fusion_data_collection/teleop_patch/marvin_teleop"),
)
from collection_telemetry import Publisher  # noqa: E402


class RecordingRobot(Robot):
    """Observe accepted SDK commands without another control connection."""

    last_command = None
    command_sequence = 0

    def command(self, q, deadline=None):
        super().command(q, deadline=deadline)
        if q:
            self.command_sequence += 1
            self.last_command = {
                "sequence": self.command_sequence,
                "pc_monotonic_ns": time.monotonic_ns(),
                "joints_deg": {s: values.tolist() for s, values in q.items()},
                "kind": "sdk_accepted_joint_target",
            }


def sides(mode):
    return ("left", "right") if mode == "dual" else (mode,)


def load_configuration():
    return configuration(robot_configuration())


def gripper_configuration(parameters, selected, *, path=None):
    """Build the actual driver settings, retaining calibrated travel/limits."""
    gc = load_grippers(path or ROOT / "teleop/gripper/config.json", live=True)
    params = TianjiParameters.model_validate(parameters)
    gc["motors"] = {side: motor for side, motor in gc["motors"].items() if side in selected}
    for side, motor in gc["motors"].items():
        motor.update(speed_rad_s=getattr(params, side + "_gripper_speed"),
                     accel_rad_s2=getattr(params, side + "_gripper_accel"),
                     open_rad=getattr(params, side + "_gripper_open_rad"),
                     closed_rad=getattr(params, side + "_gripper_closed_rad"))
        travel_bounds(motor)
    return gc


def manual_plan(models, measured, request, config):
    """Validate the entire path before enabling; keep the other arm in collision checks."""
    selected = sides(request.mode)
    goal = {s: q.copy() for s, q in measured.items()}
    if request.kind == "jog":
        if request.side not in selected:
            raise ValueError("该机械臂未在当前模式中启用")
        selected = (request.side,)
        goal[request.side][request.joint - 1] += request.delta
    elif request.kind == "zero":
        for s in selected:
            goal[s] = np.zeros(7)
    elif request.kind == "pose":
        if not request.targets or set(request.targets) != set(selected):
            raise ValueError("姿态必须包含当前模式的全部关节")
        for s in selected:
            values = np.asarray(request.targets[s], dtype=float)
            if values.shape != (7,) or not np.isfinite(values).all():
                raise ValueError("每臂必须为 7 个有效关节角度")
            goal[s] = values
    else:
        raise ValueError("无效的机械臂动作")
    from marvin_teleop.point_to_point import pose_plan
    # Saved poses use a faster smooth trajectory; zero recovery has its own
    # unchanged state machine, and joint jogging keeps the existing limits.
    return pose_plan(models, measured, goal, selected, config,
                     acceleration=20.0 if request.kind == "pose" else 5.0 if request.kind == "zero" else 4.0,
                     speed_limit=20.0 if request.kind == "pose" else 10.0)


def loop_fault(error, *, robot, ctrl, motion, recovery, grippers, halt):
    """Idle observation failures must not become a fictitious control session."""
    detail = str(error)
    had_control = bool(ctrl or motion or recovery or grippers or (robot and robot.enabled))
    if had_control:
        try:
            halt()
        except Exception as stop_error:
            detail += "; 停止失败：" + str(stop_error)
    return ('error' if had_control else 'idle'), detail


def main():
    lock_path = Path(__file__).resolve().parents[2] / "runtime/tianji/sdk.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lease_file = lock_path.open("a")
    try:
        fcntl.flock(lease_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lease_file.close()
        raise RuntimeError("已有天机 Marvin M6控制进程占用设备，请先停止原连接") from exc
    commands = queue.Queue()
    quitting = threading.Event()

    def reader():
        for line in sys.stdin:
            try:
                commands.put(json.loads(line))
            except ValueError:
                continue
        quitting.set()

    threading.Thread(target=reader, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: quitting.set())
    signal.signal(signal.SIGINT, lambda *_: quitting.set())

    outgoing, replies = queue.Queue(maxsize=64), queue.SimpleQueue()
    output_done = threading.Event()

    def emit(value):
        serial = getattr(launcher, "verified_serial", None)
        if serial:
            value["headset"] = {"serial": serial, "package": config["package"]}
        if "reply" in value:
            replies.put(value)
            return True
        try:
            outgoing.put_nowait(value)
            return True
        except queue.Full:
            return False

    def writer():
        while not output_done.is_set():
            try:
                value = replies.get_nowait() if not replies.empty() else outgoing.get(timeout=0.05)
                print(
                    "TIANJI:" + json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True
                )
            except queue.Empty:
                continue
            except (BrokenPipeError, OSError):
                quitting.set()
                return

    output_thread = threading.Thread(target=writer, name="tianji-ipc", daemon=True)
    output_thread.start()
    publisher = Publisher(sink=lambda row: emit({"telemetry": row}))

    config = load_configuration()
    models = Models(config)
    validate_preview(models, Path(__file__).resolve().parents[3] / "public/tianji-model.json")
    models.validate()
    robot = None
    vr = launcher = ctrl = grippers = motion = None
    recovery = None
    recovery_report = None
    state, error, mode = "idle", "", "dual"
    upload_mode = "dual"
    last_lease = time.monotonic()
    last_publish = 0.0
    last_tick = time.monotonic()
    robot_ready = False
    quit_pico = False

    def halt():
        nonlocal ctrl, grippers, motion, state, recovery, recovery_report
        # Always try both stop paths; a failed stop is surfaced, never reported as idle.
        errors = []
        if grippers:
            try:
                grippers.close()
            except Exception as exc:
                errors.append(str(exc))
            else:
                grippers = None
        if robot:
            try:
                robot.stop()
            except Exception as exc:
                errors.append(str(exc))
        if recovery:
            recovery_report = recovery.status()
            if not recovery.done:
                recovery_report['detail'] = '回零已停止：' + recovery.detail
        ctrl = motion = recovery = None
        if robot:
            robot.last_command = None
        state = "error" if errors else "idle"
        if errors:
            raise RuntimeError("; ".join(errors))

    def disconnect_vr():
        nonlocal vr
        try:
            if launcher:
                launcher.close(quit_app=quit_pico)
        finally:
            if vr:
                vr.close()
                vr = None

    def setup_grippers(selected):
        gc = gripper_configuration(config.get("app_parameters", {}), selected)
        return Grippers(gc, True, config["vr_timeout_s"], robot=robot, rate_hz=config["rate_hz"])

    try:
        while not quitting.is_set():
            now = time.monotonic()
            frame, emergency, vr_error, connected = (
                vr.snapshot() if vr else (None, False, "", False)
            )
            try:
                while not commands.empty():
                    cmd = commands.get_nowait()
                    action = cmd.get("action")
                    if action == "heartbeat":
                        last_lease = now
                        continue
                    try:
                        if action == "check":
                            if state != "idle":
                                raise ValueError("请先停止当前控制")
                            if robot is None:
                                robot = RecordingRobot(config)
                            robot.wait_ready()
                            robot_ready = True
                            if launcher is None:
                                launcher = PicoLauncher(config)
                                launcher.start()
                            if vr is None:
                                vr = LocalVRConnection(config, input_factory=SDKInput,
                                                       ensure_service=ensure_service,
                                                       cancelled=quitting)
                            vr.check()
                            frame, emergency, vr_error, connected = vr.snapshot()
                            error = ""
                        elif action == "start":
                            req = TianjiStart.model_validate(cmd["payload"])
                            if state != "idle" or robot is None or not robot_ready or not connected:
                                raise ValueError("请先检查设备，确认机械臂和 VR 数据在线")
                            config.update(req.parameters.model_dump())
                            config["app_parameters"] = req.parameters.model_dump()
                            mode = req.mode
                            upload_mode = req.upload_mode or req.mode
                            config["controlled_sides"] = sides(mode)
                            grippers = setup_grippers(sides(mode))
                            ctrl = Controller(models, robot, config)
                            robot.last_command = None
                            ctrl.start_session()
                            state, error = "running", ""
                        elif action == "move":
                            req = TianjiMove.model_validate(cmd["payload"])
                            if req.kind == "zero":
                                if state not in ("idle", "error") or ctrl or motion or recovery or grippers:
                                    raise ValueError("请先停止当前控制，再执行回零恢复")
                                config.update(req.parameters.model_dump())
                                mode = req.mode
                                if robot is None:
                                    robot = RecordingRobot(config)
                                # Recovery owns its fresh/error-aware read and
                                # clear/idle sequence; no VR/check prerequisite.
                                recovery = ZeroRecovery(robot, models, config, sides(mode), now)
                                recovery_report = recovery.status()
                                state, error, robot_ready = "moving", "", False
                                emit({"reply": cmd.get("id"), "ok": True, "state": state})
                                continue
                            if state != "idle" or robot is None or not robot_ready:
                                raise ValueError("请先停止遥操作，并确认机械臂反馈在线")
                            config.update(req.parameters.model_dump())
                            config["app_parameters"] = req.parameters.model_dump()
                            measured = robot.read()
                            if any(np.max(np.abs(v)) > 0.5 for v in robot.velocities.values()):
                                raise ValueError("机械臂尚未静止")
                            mode = req.mode
                            if req.kind == "gripper":
                                if req.side not in sides(mode):
                                    raise ValueError("该夹爪未启用")
                                grippers = setup_grippers((req.side,))
                                motion = dict(
                                    gripper_side=req.side, closure=req.closure, started=now
                                )
                            else:
                                motion = manual_plan(models, measured, req, config)
                                robot.enable(motion["sides"])
                            state, error = "moving", ""
                        elif action == "stop":
                            reconnect_required = not robot_ready or state == "error"
                            halt()
                            # A faulted SDK connection must not be read again
                            # immediately after acknowledging stop: stale UDP
                            # feedback would put the UI straight back in error
                            # and prevent the operator from checking devices.
                            # Release only after halt has confirmed shutdown.
                            if reconnect_required and robot:
                                robot_ready = False
                                robot.close()
                                robot = None
                            error = ""
                        elif action == "close":
                            quit_pico = bool((cmd.get("payload") or {}).get("quit_pico", True))
                            try:
                                halt()
                            finally:
                                disconnect_vr()
                            quitting.set()
                        else:
                            raise ValueError("不支持的天机 Marvin M6控制指令")
                        emit({"reply": cmd.get("id"), "ok": True, "state": state})
                    except Exception as exc:
                        if action in ("stop", "close"):
                            state = "error"
                        if action in ("start", "move"):
                            with contextlib.suppress(Exception):
                                halt()
                        error = str(exc)
                        emit({"reply": cmd.get("id"), "ok": False, "error": error, "state": state})
                if now - last_lease > 8 and state in ("running", "moving"):
                    halt()
                    error = "页面连接中断，控制已停止"
                if now - last_lease > 60:
                    quitting.set()
                if recovery:
                    recovery.tick(now)
                    recovery_report = recovery.status()
                    robot_ready = recovery.phase not in ("feedback", "idle_ack")
                    if recovery.done:
                        halt()
                        error = ""
                elif robot and robot_ready:
                    measured = robot.read()
                    robot_ready = True
                if ctrl:
                    ctrl.tick(frame, connected, emergency, now)
                    if grippers:
                        grippers.submit(frame, connected, emergency or ctrl.fault)
                    if ctrl.fault:
                        raise RuntimeError(ctrl.reason)
                if motion and "gripper_side" in motion:
                    from types import SimpleNamespace

                    elapsed = now - motion["started"]
                    sample = SimpleNamespace(
                        received=now,
                        session=1,
                        safety_epoch=0,
                        focused=True,
                        tracked=True,
                        stop=False,
                        triggers={
                            motion["gripper_side"]: 0
                            if elapsed < 0.15
                            else max(0.051, motion["closure"])
                        },
                    )
                    grippers.submit(sample, True, False)
                    if elapsed > 0.15 and motion["closure"] == 0:
                        # Establish takeover, then open fully without a discontinuity.
                        sample.triggers[motion["gripper_side"]] = 0.051 if elapsed < 0.3 else 0
                        grippers.submit(sample, True, False)
                    gs = grippers.status()
                    if gs["fault"]:
                        raise RuntimeError(gs["fault"])
                    motor = gs["motors"][motion["gripper_side"]]
                    feedback = motor.get("feedback") or {}
                    points = motor["display_endpoints_rad"]
                    target = points["open_rad"] if motion["closure"] == 0 else points["closed_rad"]
                    if (
                        elapsed > 0.3
                        and motor["state"] == "ACTIVE"
                        and abs(feedback.get("position", float("inf")) - target) < 0.04
                    ):
                        halt()
                    elif elapsed > 12:
                        raise RuntimeError("夹爪未按时到达目标，请检查夹爪反馈")
                elif motion:
                    if robot.pending_sides:
                        if now - motion["enabled_at"] > 3:
                            raise RuntimeError("等待使能超时")
                    else:
                        motion["elapsed"] += min(0.02, max(0.0, now - last_tick))
                        u = min(1.0, motion["elapsed"] / motion["duration"])
                        from marvin_teleop.point_to_point import pose_at
                        planned = pose_at(motion, motion["elapsed"])
                        target = {
                            s: planned[s]
                            if s in motion["sides"]
                            else measured[s]
                            for s in measured
                        }
                        middle = {s: (measured[s] + target[s]) / 2 for s in measured}
                        for candidate in (measured, middle, target):
                            reason = models.collision_reason(
                                candidate, config["collision_margin_m"]
                            )
                            if reason:
                                raise RuntimeError(reason)
                        robot.command({s: target[s] for s in motion["sides"]}, deadline=now + 0.05)
                        if u == 1:
                            if all(
                                np.max(np.abs(measured[s] - target[s])) < 0.15
                                for s in motion["sides"]
                            ):
                                halt()
                            elif motion["elapsed"] > motion["duration"] + 3:
                                raise RuntimeError("未按时到达目标姿态")
            except Exception as exc:
                robot_ready = False
                # An idle feedback/check failure is a connection fault, not a
                # running control session. Wait for the operator's next check.
                state,error = loop_fault(exc,robot=robot,ctrl=ctrl,motion=motion,
                    recovery=recovery,grippers=grippers,halt=halt)
            last_tick = now
            # Snapshot real feedback on the existing control loop. Encoding and
            # IPC run on separate bounded queues, never on the motion thread.
            publisher.submit(
                {
                    "mode": "live",
                    "state": state,
                    "joints_deg": {s: q.tolist() for s, q in robot.q.items()}
                    if robot and robot_ready
                    else {},
                    "robot_feedback_details": getattr(robot, "feedback_status", {}),
                    "grippers": grippers.status() if grippers else None,
                    "gripper_feedback": grippers.drain_feedback() if grippers else {},
                    "joint_command": getattr(robot, "last_command", None) if ctrl else None,
                    "motion_debug": {
                        "hold_policy": "joint_latch_v2",
                        "source_time": getattr(frame, 'source_time', None),
                        "input_age_s": now-frame.received if frame else None,
                        "hands": {s: p.tolist() for s,p in frame.hands.items()} if frame else {},
                        "feedback_velocity_deg_s": {s: v.tolist() for s,v in getattr(robot, 'velocities', {}).items()},
                        "arms": {s: {
                            "mode": a.mode, "stationary_joint_hold": a.stationary_hold,
                            "hand_position_held": bool(a.hand_stability and a.hand_stability.position_held),
                            "hand_rotation_held": bool(a.hand_stability and a.hand_stability.rotation_held),
                            "accepted_velocity_deg_s": a.velocity.tolist(),
                            "target_pose": a.target_pose.tolist() if a.target_pose is not None else None,
                        } for s,a in ctrl.arms.items()},
                    } if ctrl else None,
                    "teleop_mapping": {
                        "coordinate_revision": models.mapping_report.get("coordinate_revision"),
                        "ik_policy": models.mapping_report.get("ik_policy"),
                        "arm_geometry": config["arm_geometry"],
                        "input_mapping": "same_facing",
                        "model_world_from_tracking": ctrl.session_basis.tolist()
                        if ctrl is not None and ctrl.session_basis is not None else None,
                        "vr_session": ctrl.session if ctrl else None,
                    },
                },
                frame,
                connected,
            )
            if now - last_publish >= 0.05:
                last_publish = now
                emit(
                    {
                        "status": {
                            "state": state,
                            "mode": mode,
                            "upload_mode": upload_mode,
                            "error": error,
                            "robot_ready": robot_ready,
                            "vr_ready": connected,
                            "pico_ready": bool(launcher and launcher.app_started),
                            "pico_detail": launcher.status if launcher else "",
                            "vr_detail": vr_error,
                            "joints": {s: q.tolist() for s, q in robot.q.items()}
                            if robot_ready and robot and robot.q
                            else {},
                            "grippers": grippers.status() if grippers else None,
                            "reason": ctrl.reason if ctrl else "",
                            "arm_control": ctrl.status() if ctrl else {},
                            "coordinate_mapping": models.mapping_report,
                            "recovery": recovery_report,
                        }
                    }
                )
            quitting.wait(max(0, 0.01 - (time.monotonic() - now)))
    finally:
        try:
            halt()
        finally:
            try:
                disconnect_vr()
            finally:
                try:
                    if robot:
                        robot.close()
                finally:
                    lease_file.close()
                    publisher.close()
                    output_done.set()
                    output_thread.join(timeout=1)


if __name__ == "__main__":
    main()
