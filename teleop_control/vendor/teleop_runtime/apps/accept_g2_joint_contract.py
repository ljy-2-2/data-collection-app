"""Read and validate the 22-joint G2 adapter contract without motion."""

import argparse
import importlib
import sys

from apps.read_g2_state import _settle_seconds, read_real_g2_state
from teleop.contracts.capabilities import AdapterOperatingMode, RobotCapabilities
from teleop.robots.g2 import make_g2_joint_schema
from teleop.validation.adapter_acceptance import (
    AdapterAcceptanceReport,
    inspect_adapter_snapshot,
)


def format_report(report: AdapterAcceptanceReport) -> str:
    lines: list[str] = []
    for joint in report.joints:
        current = (
            "missing"
            if joint.current_position is None
            else f"{joint.current_position:+.6f}"
        )
        velocity = (
            "unspecified"
            if joint.max_velocity is None
            else f"{joint.max_velocity:.6f}"
        )
        lines.append(
            "G2_JOINT_ACCEPTANCE "
            f"joint={joint.name} group={joint.group} current={current} "
            f"position_min={joint.position_min:+.6f} "
            f"position_max={joint.position_max:+.6f} "
            f"max_step={joint.max_step:.6f} max_velocity={velocity} "
            f"status={'PASS' if joint.passed else 'FAIL'} "
            f"reason={joint.reason.replace(' ', '_')} "
            f"physical_robot_command_sent={report.physical_robot_command_sent}"
        )
    lines.append(
        "G2_JOINT_ACCEPTANCE_SUMMARY "
        f"passed={report.passed} joints={len(report.joints)} "
        f"physical_robot_command_sent={report.physical_robot_command_sent}"
    )
    return "\n".join(lines)


def _print_precheck() -> None:
    print(
        "G2_JOINT_ACCEPTANCE_PRECHECK mode=live-read-only expected_joints=22 "
        "physical_robot_command_sent=False",
        flush=True,
    )


def run(*, settle_seconds: float, emit_precheck: bool = True) -> int:
    if emit_precheck:
        _print_precheck()
    try:
        gdk = importlib.import_module("agibot_gdk")
        state = read_real_g2_state(gdk=gdk, settle_seconds=settle_seconds)
        report = inspect_adapter_snapshot(
            capabilities=RobotCapabilities(
                operating_mode=AdapterOperatingMode.READ_ONLY,
                joint_state_read=True,
            ),
            schema=make_g2_joint_schema(),
            state=state,
            expected_joint_count=22,
        )
    except Exception as error:
        print(
            f"G2_JOINT_ACCEPTANCE_ERROR: {error} "
            "physical_robot_command_sent=False",
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(format_report(report), flush=True)
    return 0 if report.passed else 1


def main() -> int:
    _print_precheck()
    parser = argparse.ArgumentParser(
        description=(
            "Read and validate the 22-joint G2 adapter contract without sending motion."
        )
    )
    parser.add_argument(
        "--settle-seconds",
        type=_settle_seconds,
        default=2.0,
        help="wait after GDK initialization before the read (default: 2)",
    )
    args = parser.parse_args()
    return run(settle_seconds=args.settle_seconds, emit_precheck=False)


if __name__ == "__main__":
    raise SystemExit(main())
