"""Pipe PICO joint-jog intents to a read-only G2 preview worker."""

import argparse
from dataclasses import dataclass
import importlib
import json
import math
import os
import signal
import sys
import time
from typing import Callable, TextIO

from apps.preview_g2_joint import build_preview_from_state
from teleop.retargeting.joint_jog import JointJogMapper, TeleopInputMode
from teleop.robots.g2 import G2Adapter, make_g2_joint_schema
from teleop.xr import XRInputError, XRoboToolkitInputAdapter


WIRE_VERSION = 1
STEP_RAD = 0.01
MAX_MESSAGE_AGE_NS = 200_000_000
_WIRE_KEYS = {
    "version",
    "sequence_id",
    "emitted_monotonic_ns",
    "joint_name",
    "delta_rad",
}


@dataclass(frozen=True)
class PicoJointJogMessage:
    sequence_id: int
    emitted_monotonic_ns: int
    joint_name: str
    delta_rad: float

    def __post_init__(self) -> None:
        if type(self.sequence_id) is not int or self.sequence_id < 0:
            raise ValueError("sequence_id must be a non-negative integer")
        if (
            type(self.emitted_monotonic_ns) is not int
            or self.emitted_monotonic_ns < 0
        ):
            raise ValueError(
                "emitted_monotonic_ns must be a non-negative integer"
            )
        if self.joint_name not in make_g2_joint_schema().commandable_names:
            raise ValueError(f"unknown G2 joint: {self.joint_name!r}")
        if isinstance(self.delta_rad, bool) or not isinstance(
            self.delta_rad, (int, float)
        ):
            raise ValueError("delta_rad must be a number")
        converted = float(self.delta_rad)
        if not math.isfinite(converted) or converted not in (-STEP_RAD, STEP_RAD):
            raise ValueError("delta_rad must be exactly +0.01 or -0.01")
        object.__setattr__(self, "delta_rad", converted)


def encode_message(message: PicoJointJogMessage) -> str:
    if not isinstance(message, PicoJointJogMessage):
        raise TypeError("message must be PicoJointJogMessage")
    return json.dumps(
        {
            "version": WIRE_VERSION,
            "sequence_id": message.sequence_id,
            "emitted_monotonic_ns": message.emitted_monotonic_ns,
            "joint_name": message.joint_name,
            "delta_rad": message.delta_rad,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_message(line: str) -> PicoJointJogMessage:
    if not isinstance(line, str):
        raise ValueError("intent message must be text")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError("invalid message JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("intent message must be a JSON object")
    if set(payload) != _WIRE_KEYS:
        raise ValueError("intent message fields do not match version 1")
    if type(payload["version"]) is not int or payload["version"] != WIRE_VERSION:
        raise ValueError("unsupported intent message version")
    return PicoJointJogMessage(
        sequence_id=payload["sequence_id"],
        emitted_monotonic_ns=payload["emitted_monotonic_ns"],
        joint_name=payload["joint_name"],
        delta_rad=payload["delta_rad"],
    )


def _positive_finite_float(value: str) -> float:
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(converted) or converted <= 0.0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return converted


def _nonnegative_finite_float(value: str) -> float:
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(converted) or converted < 0.0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return converted


def _intent_file_descriptor(value: str) -> int:
    try:
        converted = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "must be an integer file descriptor"
        ) from error
    if converted < 3:
        raise argparse.ArgumentTypeError("must be file descriptor 3 or greater")
    return converted


def emit_session(
    args: argparse.Namespace,
    *,
    adapter: object,
    output: TextIO,
    diagnostics: TextIO,
    stop_requested: Callable[[], bool] = lambda: False,
) -> int:
    mode = TeleopInputMode(args.mode)
    mapper = JointJogMapper(
        make_g2_joint_schema(),
        step_rad=STEP_RAD,
        initial_joint=getattr(args, "initial_joint", None),
    )
    frame_count = 0
    intent_count = 0
    controller_activity_seen = False
    x_seen = False
    a_seen = False
    max_right_grip = 0.0
    max_abs_right_axis_y = 0.0
    last_frame_stale = False
    exit_code = 0

    print(
        f"PICO_INTENT_PIPE_PRECHECK mode={mode.value} "
        "physical_robot_command_sent=False",
        file=diagnostics,
        flush=True,
    )
    if mode is TeleopInputMode.JOINT_JOG:
        print(
            "X/A select a joint; hold right Grip and deflect the right stick. "
            "Only intent data enters the pipe.",
            file=diagnostics,
            flush=True,
        )

    try:
        adapter.start()
        deadline = time.monotonic() + args.duration
        sample_period = 1.0 / args.hz
        while time.monotonic() < deadline and not stop_requested():
            frame = adapter.read_frame()
            if frame is not None:
                frame_count += 1
                last_frame_stale = frame.stale
                left = frame.left_controller
                right = frame.right_controller
                x_seen = x_seen or left.buttons.primary
                a_seen = a_seen or right.buttons.primary
                max_right_grip = max(max_right_grip, right.grip)
                max_abs_right_axis_y = max(
                    max_abs_right_axis_y, abs(right.axis[1])
                )
                controller_activity_seen = controller_activity_seen or any(
                    (
                        abs(left.axis[0]) > 0.05,
                        abs(left.axis[1]) > 0.05,
                        abs(right.axis[0]) > 0.05,
                        abs(right.axis[1]) > 0.05,
                        left.grip > 0.05,
                        right.grip > 0.05,
                        left.trigger > 0.05,
                        right.trigger > 0.05,
                        left.buttons.primary,
                        left.buttons.secondary,
                        left.buttons.menu,
                        left.buttons.axis_click,
                        right.buttons.primary,
                        right.buttons.secondary,
                        right.buttons.menu,
                        right.buttons.axis_click,
                    )
                )
                status = mapper.update(frame, mode)
                if status.reason in ("selected_previous", "selected_next"):
                    print(
                        f"PICO_SELECTED joint={status.selected_joint} "
                        "physical_robot_command_sent=False",
                        file=diagnostics,
                        flush=True,
                    )
                if status.intent is not None:
                    joint_name, delta_rad = next(iter(status.intent.deltas.items()))
                    message = PicoJointJogMessage(
                        sequence_id=frame.sequence_id,
                        emitted_monotonic_ns=time.monotonic_ns(),
                        joint_name=joint_name,
                        delta_rad=delta_rad,
                    )
                    output.write(encode_message(message) + "\n")
                    output.flush()
                    intent_count += 1
            remaining = deadline - time.monotonic()
            if remaining > 0.0:
                time.sleep(min(sample_period, remaining))
    except (XRInputError, BrokenPipeError) as error:
        print(
            f"PICO_INTENT_PIPE_ERROR: {error} "
            "physical_robot_command_sent=False",
            file=diagnostics,
            flush=True,
        )
        exit_code = 2
    finally:
        try:
            adapter.close()
        except XRInputError as error:
            print(
                f"PICO_INTENT_PIPE_CLOSE_ERROR: {error} "
                "physical_robot_command_sent=False",
                file=diagnostics,
                flush=True,
            )
            exit_code = 2

    print(
        f"PICO_INTENT_PIPE_SUMMARY frames={frame_count} intents={intent_count} "
        f"mode={mode.value} controller_activity_seen={controller_activity_seen} "
        f"max_right_grip={max_right_grip:.3f} "
        f"max_abs_right_axis_y={max_abs_right_axis_y:.3f} "
        f"X_seen={x_seen} A_seen={a_seen} physical_robot_command_sent=False",
        file=diagnostics,
        flush=True,
    )
    if exit_code != 0:
        return exit_code
    if frame_count == 0:
        print(
            "PICO_INTENT_PIPE_FAILED: no valid XR frame was received "
            "physical_robot_command_sent=False",
            file=diagnostics,
            flush=True,
        )
        return 1
    if last_frame_stale:
        print(
            "PICO_INTENT_PIPE_FAILED: the final XR frame was stale "
            "physical_robot_command_sent=False",
            file=diagnostics,
            flush=True,
        )
        return 1
    if mode is TeleopInputMode.JOINT_JOG and not controller_activity_seen:
        print(
            "PICO_CONTROLLER_INACTIVE: headset frames are arriving, but all "
            "controller inputs stayed zero. Wake both controllers, enable "
            "Controllers, disable Hand Tracking temporarily, and turn off "
            "'Switch with A'. physical_robot_command_sent=False",
            file=diagnostics,
            flush=True,
        )
        return 1
    if mode is TeleopInputMode.JOINT_JOG and intent_count == 0:
        print(
            "PICO_JOINT_JOG_INACTIVE: controller activity arrived but no jog "
            "gesture completed. Release X/A and Grip, center the right stick, "
            "then hold right Grip and deflect the stick once. "
            "physical_robot_command_sent=False",
            file=diagnostics,
            flush=True,
        )
        return 1
    return 0


def _message_age_ns(message: PicoJointJogMessage, now_ns: int) -> int:
    if type(now_ns) is not int or now_ns < 0:
        raise ValueError("monotonic clock must return a non-negative integer")
    age_ns = now_ns - message.emitted_monotonic_ns
    if age_ns < 0:
        raise ValueError("PICO intent timestamp is in the future")
    if age_ns > MAX_MESSAGE_AGE_NS:
        raise ValueError("PICO intent is stale")
    return age_ns


def _format_worker_preview(message: PicoJointJogMessage, preview: object) -> str:
    request = preview.request
    return (
        "PICO_G2_PREVIEW "
        f"xr_sequence={message.sequence_id} "
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


def run_g2_worker(
    *,
    input_stream: TextIO,
    output: TextIO,
    errors: TextIO,
    gdk: object,
    settle_seconds: float,
    monotonic_ns=time.monotonic_ns,
    stop_requested: Callable[[], bool] = lambda: False,
) -> int:
    if (
        isinstance(settle_seconds, bool)
        or not isinstance(settle_seconds, (int, float))
        or not math.isfinite(settle_seconds)
        or settle_seconds < 0.0
    ):
        raise ValueError("settle_seconds must be finite and non-negative")

    print(
        "PICO_G2_WORKER_PRECHECK mode=read-and-preview "
        "physical_robot_command_sent=False",
        file=output,
        flush=True,
    )
    preview_count = 0
    rejected_count = 0
    exit_code = 0
    last_sequence_id = -1
    success = None
    initialized = False
    adapter = None
    try:
        success = gdk.GDKRes.kSuccess
        init_result = gdk.gdk_init()
        if init_result != success:
            raise RuntimeError(
                f"GDK initialization failed: {init_result!r}"
            )
        initialized = True
        if not stop_requested():
            robot = gdk.Robot()
            if settle_seconds:
                time.sleep(settle_seconds)
            adapter = G2Adapter(dry_run=False, state_reader=robot)
            print(
                "PICO_G2_WORKER_READY waiting_for_pico_intent=True "
                "physical_robot_command_sent=False",
                file=output,
                flush=True,
            )
        for line in (input_stream if adapter is not None else ()):
            if stop_requested():
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                message = decode_message(stripped)
                if message.sequence_id <= last_sequence_id:
                    raise ValueError("PICO intent sequence was replayed")
                _message_age_ns(message, monotonic_ns())
            except ValueError as error:
                rejected_count += 1
                print(
                    f"PICO_G2_REJECTED reason=invalid message: {error} "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                continue

            state = adapter.read_state()
            try:
                _message_age_ns(message, monotonic_ns())
                preview = build_preview_from_state(
                    state,
                    joint_name=message.joint_name,
                    delta_rad=message.delta_rad,
                )
            except ValueError as error:
                rejected_count += 1
                print(
                    f"PICO_G2_REJECTED reason={error} "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                continue
            last_sequence_id = message.sequence_id
            print(_format_worker_preview(message, preview), file=output, flush=True)
            preview_count += 1
    except Exception as error:
        print(
            f"PICO_G2_WORKER_ERROR: {error} "
            "physical_robot_command_sent=False",
            file=errors,
            flush=True,
        )
        exit_code = 2
    finally:
        if adapter is not None:
            try:
                adapter.stop()
            except Exception as error:
                print(
                    f"PICO_G2_WORKER_STOP_ERROR: {error} "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                exit_code = 2
        if initialized:
            try:
                release_result = gdk.gdk_release()
            except Exception as error:
                print(
                    f"PICO_G2_WORKER_RELEASE_ERROR: {error} "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                exit_code = 2
            else:
                if release_result != success:
                    print(
                        "PICO_G2_WORKER_RELEASE_ERROR: GDK release failed: "
                        f"{release_result!r} physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
                    exit_code = 2

    print(
        f"PICO_G2_WORKER_SUMMARY previews={preview_count} "
        f"rejected={rejected_count} physical_robot_command_sent=False",
        file=output,
        flush=True,
    )
    if exit_code != 0:
        return exit_code
    return 0 if preview_count > 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pipe PICO joint-jog intents to a read-only G2 preview worker."
    )
    subparsers = parser.add_subparsers(dest="role", required=True)
    emit_parser = subparsers.add_parser("emit")
    emit_parser.add_argument(
        "--mode",
        choices=(TeleopInputMode.OBSERVE.value, TeleopInputMode.JOINT_JOG.value),
        default=TeleopInputMode.OBSERVE.value,
    )
    emit_parser.add_argument("--duration", type=_positive_finite_float, default=20.0)
    emit_parser.add_argument("--hz", type=_positive_finite_float, default=50.0)
    emit_parser.add_argument(
        "--initial-joint",
        choices=make_g2_joint_schema().commandable_names,
        default=None,
        help="named joint selected when the session starts",
    )
    emit_parser.add_argument(
        "--intent-fd",
        type=_intent_file_descriptor,
        default=None,
        help=argparse.SUPPRESS,
    )
    worker_parser = subparsers.add_parser("worker")
    worker_parser.add_argument(
        "--settle-seconds", type=_nonnegative_finite_float, default=2.0
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    termination_signum = None

    def record_stop_request(signum: int, _frame: object) -> None:
        nonlocal termination_signum
        if termination_signum is None:
            termination_signum = signum

    def stop_requested() -> bool:
        return termination_signum is not None

    previous_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    for signum in previous_handlers:
        signal.signal(signum, record_stop_request)

    try:
        if args.role == "emit":
            protocol_output = sys.stdout
            if args.intent_fd is not None:
                try:
                    protocol_output = os.fdopen(
                        args.intent_fd,
                        "w",
                        buffering=1,
                        encoding="utf-8",
                        closefd=False,
                    )
                except OSError as error:
                    print(
                        f"PICO_INTENT_PIPE_ERROR: cannot open intent fd "
                        f"{args.intent_fd}: {error} "
                        "physical_robot_command_sent=False",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 2
            result = emit_session(
                args,
                adapter=XRoboToolkitInputAdapter(),
                output=protocol_output,
                diagnostics=sys.stderr,
                stop_requested=stop_requested,
            )
        else:
            try:
                gdk = importlib.import_module("agibot_gdk")
            except ImportError as error:
                print(
                    f"PICO_G2_WORKER_ERROR: {error} "
                    "physical_robot_command_sent=False",
                    file=sys.stderr,
                    flush=True,
                )
                return 2
            result = run_g2_worker(
                input_stream=sys.stdin,
                output=sys.stdout,
                errors=sys.stderr,
                gdk=gdk,
                settle_seconds=args.settle_seconds,
                stop_requested=stop_requested,
            )
        if termination_signum is not None:
            print(
                f"PICO_G2_PROCESS_STOP signal={termination_signum} "
                "cleanup_completed=True physical_robot_command_sent=False",
                file=sys.stderr,
                flush=True,
            )
            return 128 + termination_signum
        return result
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
