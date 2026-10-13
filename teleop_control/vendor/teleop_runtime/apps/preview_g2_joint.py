"""Build one G2 joint request preview from a real read-only state snapshot."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import importlib
import math
import sys
import time

from apps.read_g2_state import read_real_g2_state
from teleop.backends.direct_joint import DirectJointBackend
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.contracts.intent import JointJogIntent
from teleop.contracts.robot_state import RobotState
from teleop.robots.g2 import (
    G2Adapter,
    G2JointControlRequestPreview,
    make_g2_joint_schema,
)


DEFAULT_JOINT_NAME = "idx21_arm_l_joint1"
DEFAULT_DELTA_RAD = 0.01
MAX_PREVIEW_DELTA_RAD = 0.01


@dataclass(frozen=True)
class G2LiveJointPreview:
    joint_name: str
    current_position: float
    target_position: float
    requested_delta: float
    request: G2JointControlRequestPreview


def _validate_preview_request(joint_name: str, delta_rad: float) -> None:
    if joint_name not in make_g2_joint_schema().commandable_names:
        raise ValueError(f"unknown G2 joint: {joint_name!r}")
    if isinstance(delta_rad, bool) or not isinstance(delta_rad, (int, float)):
        raise ValueError("delta_rad must be a number")
    if not math.isfinite(delta_rad):
        raise ValueError("delta_rad must be finite")
    if delta_rad == 0.0:
        raise ValueError("delta_rad must be non-zero")
    if abs(delta_rad) > MAX_PREVIEW_DELTA_RAD:
        raise ValueError(
            f"absolute delta_rad must be at most {MAX_PREVIEW_DELTA_RAD}"
        )


def build_preview_from_state(
    state: RobotState,
    *,
    joint_name: str,
    delta_rad: float,
    now_ns: int | None = None,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> G2LiveJointPreview:
    if not isinstance(state, RobotState):
        raise TypeError("state must be RobotState")
    _validate_preview_request(joint_name, delta_rad)
    command_now_ns = clock_ns() if now_ns is None else now_ns

    adapter = G2Adapter(dry_run=True, initial_positions=state.positions)
    router = ControlRouter(DirectJointBackend(default_ttl_ms=100))
    safety = SafetyGateway(max_state_age_ms=200)
    intent = JointJogIntent(
        deltas={joint_name: float(delta_rad)},
        source="g2_live_joint_preview",
    )
    command = router.route(intent, state, now_ns=command_now_ns)
    validation_now_ns = clock_ns() if now_ns is None else now_ns
    validated = safety.validate(
        command,
        state,
        adapter.get_joint_schema(),
        now_ns=validation_now_ns,
    )
    request_now_ns = clock_ns() if now_ns is None else now_ns
    request = adapter.build_request_preview(validated, now_ns=request_now_ns)
    current_position = state.positions[joint_name]
    return G2LiveJointPreview(
        joint_name=joint_name,
        current_position=current_position,
        target_position=request.joint_positions[0],
        requested_delta=float(delta_rad),
        request=request,
    )


def read_and_build_preview(
    *,
    gdk: object,
    settle_seconds: float,
    joint_name: str,
    delta_rad: float,
) -> G2LiveJointPreview:
    _validate_preview_request(joint_name, delta_rad)
    state = read_real_g2_state(gdk=gdk, settle_seconds=settle_seconds)
    return build_preview_from_state(
        state,
        joint_name=joint_name,
        delta_rad=delta_rad,
    )


def format_preview(preview: G2LiveJointPreview) -> str:
    request = preview.request
    return (
        "G2_LIVE_JOINT_PREVIEW "
        f"selected_joint={preview.joint_name} "
        f"current_position={preview.current_position:+.6f} "
        f"requested_delta={preview.requested_delta:+.6f} "
        f"target_position={preview.target_position:+.6f} "
        f"life_time={request.life_time_s:.6f} "
        f"joint_names={list(request.joint_names)!r} "
        f"joint_positions={list(request.joint_positions)!r} "
        f"joint_velocities={list(request.joint_velocities)!r} "
        "physical_robot_command_sent=False"
    )


def _print_precheck() -> None:
    print(
        "G2_LIVE_JOINT_PREVIEW_PRECHECK mode=read-and-preview "
        "physical_robot_command_sent=False",
        flush=True,
    )


def run(
    *,
    settle_seconds: float,
    joint_name: str,
    delta_rad: float,
    emit_precheck: bool = True,
) -> int:
    if emit_precheck:
        _print_precheck()
    try:
        _validate_preview_request(joint_name, delta_rad)
        gdk = importlib.import_module("agibot_gdk")
        preview = read_and_build_preview(
            gdk=gdk,
            settle_seconds=settle_seconds,
            joint_name=joint_name,
            delta_rad=delta_rad,
        )
    except Exception as error:
        print(
            f"G2_LIVE_JOINT_PREVIEW_ERROR: {error} "
            "physical_robot_command_sent=False",
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(format_preview(preview), flush=True)
    return 0


def _settle_seconds(value: str) -> float:
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(converted) or converted < 0.0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return converted


def _delta_rad(value: str) -> float:
    try:
        converted = float(value)
        _validate_preview_request(DEFAULT_JOINT_NAME, converted)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return converted


def main() -> int:
    _print_precheck()
    parser = argparse.ArgumentParser(
        description=(
            "Read G2 state and build one safe joint request preview without sending it."
        )
    )
    parser.add_argument("--joint", default=DEFAULT_JOINT_NAME)
    parser.add_argument(
        "--delta-rad",
        type=_delta_rad,
        default=DEFAULT_DELTA_RAD,
        help="non-zero preview delta with absolute value at most 0.01",
    )
    parser.add_argument(
        "--settle-seconds",
        type=_settle_seconds,
        default=2.0,
        help="wait after GDK initialization before reading (default: 2)",
    )
    args = parser.parse_args()
    return run(
        settle_seconds=args.settle_seconds,
        joint_name=args.joint,
        delta_rad=args.delta_rad,
        emit_precheck=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
