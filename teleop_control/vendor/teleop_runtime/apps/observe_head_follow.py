"""Preview head direction only. No G2 imports, connection or command sender."""

import argparse
import math
import sys
import time

from teleop.contracts.head import HeadOrientationIntent
from teleop.contracts.xr import ControllerButtons, ControllerState, Pose, XRFrame
from teleop.retargeting.head_follow import HeadFollowMapper
from teleop.xr import XRInputDataError, XRInputError, XRoboToolkitInputAdapter


def positive(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return result


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="synthetic angles, no PICO SDK")
    parser.add_argument("--duration", type=positive, default=60.0)
    parser.add_argument("--hz", type=positive, default=50.0)
    parser.add_argument("--print-hz", type=positive, default=5.0)
    parser.add_argument("--anchor-delay", type=positive, default=3.0)
    return parser


def format_preview(intent: HeadOrientationIntent, sequence: int) -> str:
    yaw = math.degrees(intent.yaw_left_rad)
    pitch = math.degrees(intent.pitch_up_rad)
    return (
        f"HEAD_FOLLOW_PREVIEW seq={sequence} "
        f"pico_left_deg={yaw:+.2f} pico_up_deg={pitch:+.2f} "
        f"g2_requested_left_deg={yaw:+.2f} g2_requested_up_deg={pitch:+.2f} "
        "roll=ignored target_kind=relative_direction_only "
        "g2_joint_calibrated=False physical_robot_command_sent=False"
    )


def run_demo() -> int:
    mapper = HeadFollowMapper()
    controller = ControllerState(Pose((0, 0, 0), (0, 0, 0, 1)), (0, 0), 0, 0, ControllerButtons())
    for sequence, (label, axis, angle) in enumerate((
        ("anchor", (0, 1, 0), 0), ("left_10", (0, 1, 0), 10),
        ("right_10", (0, 1, 0), -10), ("up_10", (1, 0, 0), 10),
        ("down_10", (1, 0, 0), -10), ("roll_20_ignored", (0, 0, 1), 20),
    ), start=1):
        half = math.radians(angle) / 2
        q = tuple(v * math.sin(half) for v in axis) + (math.cos(half),)
        frame = XRFrame(sequence, sequence, sequence, False,
                        Pose((0, 0, 0), q), controller, controller, source="synthetic_demo")
        intent = mapper.update(frame)
        print(f"DEMO case={label} {format_preview(intent, sequence)}", flush=True)
    print("HEAD_FOLLOW_SUMMARY source=synthetic real_pico_verified=False physical_robot_command_sent=False")
    return 0


def run_live(args: argparse.Namespace) -> int:
    adapter = XRoboToolkitInputAdapter()
    mapper = HeadFollowMapper()
    previews = 0
    last_valid_ns: int | None = None
    last_device_timestamp = 0
    last_source_advance_ns: int | None = None
    last_status = ""
    next_print = 0.0
    failure = False
    print("头部角度预览：左转为正，抬头为正；不跟随歪头。无需按手柄。", flush=True)
    print("此处 G2 数值只是期望方向变化，不是实测关节角；不连接 G2。Ctrl-C 退出。", flush=True)
    try:
        adapter.start()
        started = time.monotonic()
        deadline = started + args.duration
        anchor_at = started + args.anchor_delay
        while time.monotonic() < deadline:
            sample_error = None
            try:
                frame = adapter.read_frame()
            except XRInputDataError as error:
                frame = None
                sample_error = str(error)
                mapper.reset()
                last_valid_ns = None
            # Track source progress even during the preparation countdown.
            # received_monotonic_ns alone is a poll time, not source freshness.
            if frame is not None and frame.device_timestamp_ns > last_device_timestamp:
                last_device_timestamp = frame.device_timestamp_ns
                last_source_advance_ns = frame.received_monotonic_ns
            now = time.monotonic()
            intent = None
            if now < anchor_at:
                status = "prepare_look_forward"
                if now >= next_print:
                    print(f"HEAD_FOLLOW_PREPARE anchor_in_seconds={max(0, anchor_at-now):.1f}", flush=True)
                    next_print = now + 1
            elif frame is None:
                mapper.reset()
                last_valid_ns = None
                status = (f"invalid_sample: {sample_error}" if sample_error else
                          "no_pico_data_check_WORK_and_Send_Data")
            else:
                try:
                    intent = mapper.update(frame, now_ns=time.monotonic_ns())
                except ValueError as error:
                    status = str(error)
                else:
                    status = mapper.status
                # Polling can be faster than the device. Duplicate snapshots are
                # normal and must not toggle the terminal status every tick.
                duplicate = status == "duplicate_or_out_of_order"
                if intent is not None:
                    last_valid_ns = last_source_advance_ns
                    previews += 1
                elif not duplicate:
                    last_valid_ns = None
                if duplicate:
                    status = last_status
            if status != last_status:
                print(f"HEAD_FOLLOW_STATUS status={status} physical_robot_command_sent=False", flush=True)
                if status == "anchored":
                    print("已取当前朝向为零点（恢复数据后也会重新取零点），现在可左右转头、抬头低头。", flush=True)
                last_status = status
            if intent is not None and (now >= next_print or status == "anchored"):
                print(format_preview(intent, frame.sequence_id), flush=True)
                next_print = now + 1 / args.print_hz
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1 / args.hz, remaining))
    except KeyboardInterrupt:
        print("HEAD_FOLLOW_STOP requested=True physical_robot_command_sent=False", flush=True)
    except XRInputError as error:
        print(f"HEAD_FOLLOW_ERROR: {error}", file=sys.stderr, flush=True)
        failure = True
    finally:
        try:
            adapter.close()
        except XRInputError as error:
            print(f"HEAD_FOLLOW_CLOSE_ERROR: {error}", file=sys.stderr, flush=True)
            failure = True
    fresh_at_end = last_valid_ns is not None and 0 <= time.monotonic_ns() - last_valid_ns <= 300_000_000
    print(f"HEAD_FOLLOW_SUMMARY previews={previews} fresh_at_end={fresh_at_end} "
          "direction_check=operator_required physical_robot_command_sent=False", flush=True)
    if failure or previews == 0 or not fresh_at_end:
        print("HEAD_FOLLOW_FAILED: 未获得持续新鲜的头部数据；检查 PICO WORK、Head、Send Data 和接收服务。", flush=True)
        return 1
    return 0


def main() -> int:
    args = create_parser().parse_args()
    if not args.demo and args.duration <= args.anchor_delay:
        create_parser().error("--duration must exceed --anchor-delay")
    print("HEAD_FOLLOW_PRECHECK mode=angle-preview-only physical_robot_command_sent=False", flush=True)
    return run_demo() if args.demo else run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
