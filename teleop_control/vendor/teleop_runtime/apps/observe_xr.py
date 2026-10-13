import argparse
import math
import sys
import time

from teleop.contracts.xr import ControllerState, XRFrame
from teleop.xr import XRInputError, XRoboToolkitInputAdapter


def controller_activity(controller: ControllerState) -> bool:
    return (
        abs(controller.axis[0]) > 0.05
        or abs(controller.axis[1]) > 0.05
        or controller.grip > 0.05
        or controller.trigger > 0.05
        or controller.buttons.primary
        or controller.buttons.secondary
        or controller.buttons.menu
        or controller.buttons.axis_click
    )


def format_frame(frame: XRFrame) -> str:
    left = frame.left_controller
    right = frame.right_controller
    return (
        f"seq={frame.sequence_id} device_ts_ns={frame.device_timestamp_ns} "
        f"received_ns={frame.received_monotonic_ns} stale={frame.stale} "
        f"source={frame.source} frame={frame.coordinate_frame} "
        f"head={frame.head_pose.xyz_xyzw} "
        f"left_pose={left.pose.xyz_xyzw} "
        f"left_axis={left.axis} left_grip={left.grip:.2f} "
        f"left_trigger={left.trigger:.2f} "
        f"left_buttons=(X={left.buttons.primary},Y={left.buttons.secondary},"
        f"menu={left.buttons.menu},axis_click={left.buttons.axis_click}) "
        f"right_pose={right.pose.xyz_xyzw} "
        f"right_axis={right.axis} right_grip={right.grip:.2f} "
        f"right_trigger={right.trigger:.2f} "
        f"right_buttons=(A={right.buttons.primary},B={right.buttons.secondary},"
        f"menu={right.buttons.menu},axis_click={right.buttons.axis_click})"
    )


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
        description="Observe validated PICO XRFrame input without creating robot commands."
    )
    parser.add_argument("--duration", type=positive_finite_float, default=10.0)
    parser.add_argument("--hz", type=positive_finite_float, default=50.0)
    parser.add_argument("--print-hz", type=positive_finite_float, default=5.0)
    return parser


def main() -> int:
    args = create_parser().parse_args()

    adapter = XRoboToolkitInputAdapter()
    frame_count = 0
    stale_count = 0
    activity_seen = False
    left_primary_seen = False
    right_primary_seen = False
    last_frame: XRFrame | None = None
    next_print = 0.0

    try:
        adapter.start()
        deadline = time.monotonic() + args.duration
        sample_period = 1.0 / args.hz
        while time.monotonic() < deadline:
            frame = adapter.read_frame()
            if frame is not None:
                last_frame = frame
                frame_count += 1
                stale_count += int(frame.stale)
                activity_seen = activity_seen or controller_activity(
                    frame.left_controller
                ) or controller_activity(frame.right_controller)
                left_primary_seen = (
                    left_primary_seen or frame.left_controller.buttons.primary
                )
                right_primary_seen = (
                    right_primary_seen or frame.right_controller.buttons.primary
                )
                now = time.monotonic()
                if now >= next_print:
                    print(format_frame(frame), flush=True)
                    next_print = now + 1.0 / args.print_hz
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(sample_period, remaining))
    except XRInputError as error:
        print(f"XR_OBSERVE_ERROR: {error}", file=sys.stderr, flush=True)
        return 2
    finally:
        try:
            adapter.close()
        except XRInputError as error:
            print(f"XR_CLOSE_WARNING: {error}", file=sys.stderr, flush=True)

    print(
        f"XR_OBSERVE_SUMMARY frames={frame_count} stale_frames={stale_count} "
        f"controller_activity_seen={activity_seen} X_seen={left_primary_seen} "
        f"A_seen={right_primary_seen} hardware_command_sent=False",
        flush=True,
    )
    if last_frame is None:
        print("XR_OBSERVE_FAILED: no valid headset timestamp was received", flush=True)
        return 1
    if last_frame.stale:
        print("XR_OBSERVE_FAILED: the final XR frame was stale", flush=True)
        return 1
    if not activity_seen:
        print(
            "XR_OBSERVE_WARNING: no controller input activity was seen; "
            "wake the controllers and disable Hand Tracking while validating controller mode",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
