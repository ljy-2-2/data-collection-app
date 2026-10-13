import argparse
import math
import sys
import time

from teleop.retargeting.joint_jog import (
    JointJogMapper,
    JointJogStatus,
    TeleopInputMode,
)
from teleop.robots.g2 import make_g2_joint_schema
from teleop.xr import XRInputError, XRoboToolkitInputAdapter


def positive_finite_float(value: str) -> float:
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(converted) or converted <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return converted


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Observe PICO-to-JointJogIntent mapping without robot commands."
    )
    parser.add_argument(
        "--mode",
        choices=(TeleopInputMode.OBSERVE.value, TeleopInputMode.JOINT_JOG.value),
        default=TeleopInputMode.OBSERVE.value,
        help="joint-jog explicitly gives X/A to joint selection; default is read-only",
    )
    parser.add_argument("--duration", type=positive_finite_float, default=10.0)
    parser.add_argument("--hz", type=positive_finite_float, default=50.0)
    parser.add_argument("--print-hz", type=positive_finite_float, default=5.0)
    return parser


def format_status(status: JointJogStatus) -> str:
    delta = "none"
    if status.intent is not None:
        _, value = next(iter(status.intent.deltas.items()))
        delta = f"{value:+.3f}"
    return (
        f"mode={status.mode.value} armed={status.armed} "
        f"selected={status.selected_joint} delta_rad={delta} "
        f"reason={status.reason} current=unavailable target=unavailable "
        f"hardware_command_sent=False"
    )


def run_observer(args: argparse.Namespace, *, adapter: XRoboToolkitInputAdapter) -> int:
    mode = TeleopInputMode(args.mode)
    mapper = JointJogMapper(make_g2_joint_schema(), step_rad=0.01)
    frame_count = 0
    intent_count = 0
    last_status: JointJogStatus | None = None
    last_frame_stale = False
    next_print = 0.0
    exit_code = 0

    if mode is TeleopInputMode.JOINT_JOG:
        print(
            "JOINT_JOG_PRECHECK: X/A belong to joint selection; stop the old "
            "button recorder and turn off the headset's 'Switch with A' toggle.",
            flush=True,
        )
    else:
        print(
            "OBSERVE_MODE: X/A are not consumed as joint controls. "
            "Use --mode joint-jog only after resolving button ownership.",
            flush=True,
        )

    try:
        adapter.start()
        deadline = time.monotonic() + args.duration
        sample_period = 1.0 / args.hz
        while time.monotonic() < deadline:
            frame = adapter.read_frame()
            if frame is not None:
                frame_count += 1
                last_frame_stale = frame.stale
                status = mapper.update(frame, mode)
                last_status = status
                if status.intent is not None:
                    intent_count += 1
                now = time.monotonic()
                if status.intent is not None or now >= next_print:
                    print(format_status(status), flush=True)
                    next_print = now + 1.0 / args.print_hz
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(sample_period, remaining))
    except XRInputError as error:
        print(
            f"JOINT_JOG_OBSERVE_ERROR: {error} hardware_command_sent=False",
            file=sys.stderr,
            flush=True,
        )
        exit_code = 2
    finally:
        try:
            adapter.close()
        except XRInputError as error:
            print(
                f"XR_CLOSE_ERROR: {error} hardware_command_sent=False",
                file=sys.stderr,
                flush=True,
            )
            exit_code = 2

    selected = mapper.selected_joint if last_status is None else last_status.selected_joint
    print(
        f"JOINT_JOG_OBSERVE_SUMMARY frames={frame_count} intents={intent_count} "
        f"selected={selected} mode={mode.value} hardware_command_sent=False",
        flush=True,
    )
    if exit_code != 0:
        return exit_code
    if frame_count == 0:
        print(
            "JOINT_JOG_OBSERVE_FAILED: no valid XR frame was received "
            "hardware_command_sent=False",
            flush=True,
        )
        return 1
    if last_frame_stale:
        print(
            "JOINT_JOG_OBSERVE_FAILED: the final XR frame was stale "
            "hardware_command_sent=False",
            flush=True,
        )
        return 1
    if mode is TeleopInputMode.JOINT_JOG and intent_count == 0:
        print(
            "JOINT_JOG_OBSERVE_WARNING: no joint-jog intent was seen; wake the "
            "controllers, hold right Grip, return the right stick to neutral, then "
            "deflect it up or down. hardware_command_sent=False",
            flush=True,
        )
    return 0


def main() -> int:
    args = create_parser().parse_args()
    return run_observer(args, adapter=XRoboToolkitInputAdapter())


if __name__ == "__main__":
    raise SystemExit(main())
