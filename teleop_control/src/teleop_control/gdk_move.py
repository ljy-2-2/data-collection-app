#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

LEFT_ARM = [f"idx2{i}_arm_l_joint{i}" for i in range(1, 8)]
BODY = [f"idx0{i}_body_joint{i}" for i in range(1, 6)]
RIGHT_ARM = [f"idx6{i}_arm_r_joint{i}" for i in range(1, 8)]
LEFT_GRIPPER = "idx31_gripper_l_inner_joint1"
RIGHT_GRIPPER = "idx71_gripper_r_inner_joint1"
SERVO_JOINTS = LEFT_ARM + BODY + RIGHT_ARM
EXPECTED_NAMES = LEFT_ARM + [LEFT_GRIPPER] + BODY + RIGHT_ARM + [RIGHT_GRIPPER]
GRIPPER_RANGE = (-0.785, 0.0)


def read_positions(robot) -> dict[str, float]:
    positions: dict[str, float] = {}
    for row in robot.get_joint_states().get("states", []):
        name = row.get("name", row.get("joint_name"))
        value = row.get("motor_position")
        if name is not None and value is not None and math.isfinite(float(value)):
            positions[str(name)] = float(value)
    return positions


def wait_for_joints(robot, timeout: float) -> dict[str, float]:
    deadline = time.monotonic() + timeout
    missing = list(SERVO_JOINTS)
    last_error: RuntimeError | None = None
    while time.monotonic() < deadline:
        try:
            positions = read_positions(robot)
            last_error = None
        except RuntimeError as exc:
            # A freshly initialised DDS reader commonly has no first sample.
            # Treat that as transient and honour the telemetry timeout instead
            # of aborting the motion on the first read.
            last_error = exc
            time.sleep(0.1)
            continue
        missing = [name for name in SERVO_JOINTS if name not in positions]
        if not missing:
            return {name: positions[name] for name in SERVO_JOINTS}
        time.sleep(0.1)
    if last_error is not None:
        raise RuntimeError(f"joint telemetry unavailable: {last_error}") from last_error
    raise RuntimeError(f"joint telemetry incomplete: {', '.join(missing)}")


def load_limits(app_root: str) -> dict[str, tuple[float, float]]:
    path = Path(app_root).expanduser() / "gdk" / "config" / "mc_impl_config.json"
    payload = json.loads(path.read_text())
    return {
        name: (float(row["min"]), float(row["max"]))
        for name, row in payload.items()
        if isinstance(row, dict) and "min" in row and "max" in row
    }


def gdk_success(gdk, result) -> bool:
    return result in (None, 0) or result == gdk.GDKRes.kSuccess


def validate_targets(
    payload: dict, limits: dict[str, tuple[float, float]]
) -> dict[str, float]:
    names = payload.get("joint_names")
    values = payload.get("joint_positions")
    if names != EXPECTED_NAMES or not isinstance(values, list) or len(values) != len(names):
        raise ValueError("request must contain the ordered G2 dual-arm, body and gripper joints")
    targets = {name: float(value) for name, value in zip(names, values, strict=True)}
    if not all(math.isfinite(value) for value in targets.values()):
        raise ValueError("joint targets must be finite")
    for name in SERVO_JOINTS:
        if name not in limits:
            raise ValueError(f"GDK limit table is missing {name}")
        low, high = limits[name]
        if not low <= targets[name] <= high:
            raise ValueError(f"{name} target {targets[name]:.6f} is outside [{low}, {high}]")
    for name in (LEFT_GRIPPER, RIGHT_GRIPPER):
        if not GRIPPER_RANGE[0] <= targets[name] <= GRIPPER_RANGE[1]:
            raise ValueError(f"gripper target is outside {GRIPPER_RANGE}")
    return targets


def execute_motion(gdk, robot, targets: dict[str, float], args: argparse.Namespace) -> None:
    start = wait_for_joints(robot, args.telemetry_timeout)
    changed_joint = getattr(args, "changed_joint", None)
    jog_servo = changed_joint in SERVO_JOINTS
    jog_gripper = changed_joint in (LEFT_GRIPPER, RIGHT_GRIPPER)
    if changed_joint is not None:
        # A jog click must move only its selected joint. Replacing every other
        # requested value with fresh telemetry prevents stale browser targets
        # from pulling the rest of the robot toward an old pose.
        targets.update({name: start[name] for name in SERVO_JOINTS if name != changed_joint})
    deltas = {name: targets[name] - start[name] for name in SERVO_JOINTS}
    max_delta = max(abs(delta) for delta in deltas.values())
    if max_delta > args.max_start_delta:
        raise RuntimeError(
            f"maximum start delta {max_delta:.3f} exceeds {args.max_start_delta:.3f} rad"
        )
    if not jog_gripper:
        request = gdk.JointServoControlReq()
        request.control_period = 1.2 / args.hz
        request.joint_names = list(SERVO_JOINTS)
        steps = max(1, math.ceil(max_delta / args.max_joint_vel * args.hz))
        period = 1.0 / args.hz
        previous = dict(start)
        for step in range(1, steps + 1):
            cycle_start = time.monotonic()
            live = wait_for_joints(robot, min(args.telemetry_timeout, 1.0))
            error = max(abs(live[name] - previous[name]) for name in SERVO_JOINTS)
            if step > 1 and error > args.following_error:
                raise RuntimeError(
                    f"following error {error:.3f} exceeds {args.following_error:.3f} rad"
                )
            fraction = step / steps
            command = {name: start[name] + fraction * deltas[name] for name in SERVO_JOINTS}
            request.joint_positions = [command[name] for name in SERVO_JOINTS]
            if not gdk_success(gdk, robot.joint_servo_control(request)):
                raise RuntimeError("GDK rejected joint command")
            previous = command
            time.sleep(max(0.0, period - (time.monotonic() - cycle_start)))

        request.joint_positions = [targets[name] for name in SERVO_JOINTS]
        hold_seconds = 0.10 if jog_servo else 0.5
        for _ in range(max(1, round(hold_seconds * args.hz))):
            cycle_start = time.monotonic()
            if not gdk_success(gdk, robot.joint_servo_control(request)):
                raise RuntimeError("GDK rejected final joint hold command")
            time.sleep(max(0.0, period - (time.monotonic() - cycle_start)))

    for side, name in (("left", LEFT_GRIPPER), ("right", RIGHT_GRIPPER)):
        if changed_joint is not None and name != changed_joint:
            continue
        row = gdk.JointState()
        row.position = targets[name]
        gripper = gdk.JointStates()
        gripper.group = f"{side}_tool"
        gripper.target_type = "omnipicker"
        gripper.states = [row]
        gripper.nums = 1
        for _ in range(2 if jog_gripper else 5):
            if not gdk_success(gdk, robot.move_ee_pos(gripper)):
                raise RuntimeError(f"GDK rejected {side}-gripper command")
            time.sleep(0.2)
    if not jog_gripper:
        final = wait_for_joints(robot, args.telemetry_timeout)
        final_error = max(abs(final[name] - targets[name]) for name in SERVO_JOINTS)
        if final_error > args.following_error:
            raise RuntimeError(
                f"final arm error {final_error:.3f} exceeds {args.following_error:.3f} rad"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely move the G2 dual arms, body and grippers")
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--gdk-app-root", required=True)
    parser.add_argument("--hz", type=float, default=20.0)
    parser.add_argument("--max-joint-vel", type=float, default=0.10)
    parser.add_argument("--max-start-delta", type=float, default=4.60)
    parser.add_argument("--following-error", type=float, default=0.20)
    parser.add_argument("--telemetry-timeout", type=float, default=8.0)
    args = parser.parse_args()
    try:
        payload = json.loads(args.request_json)
    except json.JSONDecodeError as exc:
        parser.error(str(exc))
    args.changed_joint = payload.get("changed_joint")
    if min(
        args.hz,
        args.max_joint_vel,
        args.max_start_delta,
        args.following_error,
        args.telemetry_timeout,
    ) <= 0:
        parser.error("motion safety thresholds must be positive")

    limits = load_limits(args.gdk_app_root)
    try:
        targets = validate_targets(payload, limits)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    try:
        import agibot_gdk as gdk
    except ImportError as exc:
        print(f"GDK import failed: {exc}", file=sys.stderr)
        return 1
    if gdk.gdk_init() != gdk.GDKRes.kSuccess:
        print("GDK initialization failed", file=sys.stderr)
        return 1
    robot = gdk.Robot()
    try:
        execute_motion(gdk, robot, targets, args)
        return 0
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        print(f"safe motion aborted: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            gdk.gdk_release()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
