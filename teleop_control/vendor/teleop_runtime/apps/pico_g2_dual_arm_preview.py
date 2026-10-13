#!/usr/bin/env python3
"""PICO XRFrame -> live G2 state -> dual-arm IK preview; never sends motion."""

import argparse
from collections.abc import Callable
import importlib
import math
import os
import signal
import sys
import threading
import time
from typing import TextIO

from teleop.backends.direct_joint import DirectJointBackend
from teleop.control.dual_arm_preview import DualArmPreviewPipeline, DualArmPreviewResult
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.retargeting.dual_arm import DualArmRetargeter
from teleop.contracts.schema import JointSchema
from teleop.robots.base import IKSolver
from teleop.robots.g2 import G2Adapter, G2StateReadError, make_g2_joint_schema
from teleop.robots.g2_urdf_ik import G2URDFIKSolver
from teleop.xr import XRInputAdapter, XRInputError, XRoboToolkitInputAdapter
from teleop.xr.wire import (
    XRFrameMessage,
    decode_xr_frame_message,
    encode_xr_frame_message,
    message_age_ns,
)


def _positive_float(value: str) -> float:
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(converted) or converted <= 0.0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return converted


def _nonnegative_float(value: str) -> float:
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(converted) or converted < 0.0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return converted


def _file_descriptor(value: str) -> int:
    try:
        converted = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer file descriptor") from error
    if converted < 3:
        raise argparse.ArgumentTypeError("must be file descriptor 3 or greater")
    return converted


class LatestXRFrameMessageMailbox:
    """Thread-safe single-slot mailbox that never queues old XR frames."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest: XRFrameMessage | None = None
        self._highest_sequence_id = -1
        self._closed = False
        self._overwritten_count = 0

    @property
    def overwritten_count(self) -> int:
        with self._condition:
            return self._overwritten_count

    def publish(self, message: XRFrameMessage) -> None:
        if not isinstance(message, XRFrameMessage):
            raise TypeError("message must be an XRFrameMessage")
        with self._condition:
            sequence_id = message.frame.sequence_id
            if sequence_id <= self._highest_sequence_id:
                raise ValueError("XR frame sequence was replayed")
            if self._latest is not None:
                self._overwritten_count += 1
            self._latest = message
            self._highest_sequence_id = sequence_id
            self._condition.notify_all()

    def invalidate(self) -> None:
        """Drop a previously published frame after malformed input is observed."""
        with self._condition:
            self._latest = None
            self._condition.notify_all()

    def wait_for_update(
        self,
        previous_sequence_id: int,
        *,
        timeout_s: float,
    ) -> tuple[XRFrameMessage | None, bool]:
        if type(previous_sequence_id) is not int:
            raise ValueError("previous_sequence_id must be int")
        if not math.isfinite(timeout_s) or timeout_s < 0.0:
            raise ValueError("timeout_s must be finite and non-negative")
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed
                or (
                    self._latest is not None
                    and self._latest.frame.sequence_id != previous_sequence_id
                ),
                timeout=timeout_s,
            )
            return self._latest, self._closed

    def is_current(self, message: XRFrameMessage) -> bool:
        if not isinstance(message, XRFrameMessage):
            raise TypeError("message must be an XRFrameMessage")
        with self._condition:
            return (
                self._latest is not None
                and self._latest.frame.sequence_id == message.frame.sequence_id
            )

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def emit_session(
    args: argparse.Namespace,
    *,
    adapter: XRInputAdapter,
    output: TextIO,
    diagnostics: TextIO,
    stop_requested: Callable[[], bool] = lambda: False,
) -> int:
    frame_count = 0
    last_frame_stale = False
    exit_code = 0
    print(
        "PICO_DUAL_ARM_FRAME_PIPE_PRECHECK mode=dual-arm-read-and-preview "
        "physical_robot_command_sent=False",
        file=diagnostics,
        flush=True,
    )
    try:
        adapter.start()
        deadline = time.monotonic() + args.duration
        period = 1.0 / args.hz
        while time.monotonic() < deadline and not stop_requested():
            frame = adapter.read_frame()
            if frame is not None:
                last_frame_stale = frame.stale
                message = XRFrameMessage(
                    emitted_monotonic_ns=time.monotonic_ns(),
                    frame=frame,
                )
                output.write(encode_xr_frame_message(message) + "\n")
                output.flush()
                frame_count += 1
            remaining = deadline - time.monotonic()
            if remaining > 0.0:
                time.sleep(min(period, remaining))
    except (XRInputError, BrokenPipeError) as error:
        print(
            f"PICO_DUAL_ARM_FRAME_PIPE_ERROR: {error} "
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
                f"PICO_DUAL_ARM_FRAME_PIPE_CLOSE_ERROR: {error} "
                "physical_robot_command_sent=False",
                file=diagnostics,
                flush=True,
            )
            exit_code = 2

    print(
        f"PICO_DUAL_ARM_FRAME_PIPE_SUMMARY frames={frame_count} "
        f"last_frame_stale={last_frame_stale} "
        "physical_robot_command_sent=False",
        file=diagnostics,
        flush=True,
    )
    if exit_code:
        return exit_code
    if frame_count == 0 or last_frame_stale:
        return 1
    return 0


def _format_preview(result: DualArmPreviewResult) -> str:
    if (
        result.status != "preview"
        or result.retargeting.intent is None
        or result.state is None
        or result.validated is None
    ):
        raise ValueError("only a complete preview result can be formatted")
    targets = result.retargeting.intent.targets
    ee_targets = " ".join(
        f"{side}_ee=({pose.position_m[0]:+.3f},"
        f"{pose.position_m[1]:+.3f},{pose.position_m[2]:+.3f})"
        for side, pose in targets.items()
    )
    max_delta = max(
        abs(target - result.state.positions[name])
        for name, target in result.validated.targets.items()
    )
    return (
        "PICO_G2_DUAL_ARM_PREVIEW "
        f"xr_sequence={result.frame_sequence_id} "
        f"active_sides={','.join(result.active_sides)} "
        f"{ee_targets} joint_targets={len(result.validated.targets)} "
        f"left_trigger={result.retargeting.gripper_fractions['left']:.3f} "
        f"right_trigger={result.retargeting.gripper_fractions['right']:.3f} "
        f"max_joint_delta={max_delta:.6f} safety_validated=True "
        "physical_robot_command_sent=False"
    )


def run_g2_worker(
    *,
    input_stream: TextIO,
    output: TextIO,
    errors: TextIO,
    gdk: object,
    settle_seconds: float,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    stop_requested: Callable[[], bool] = lambda: False,
    solver_factory: Callable[[JointSchema], IKSolver] | None = None,
) -> int:
    if (
        isinstance(settle_seconds, bool)
        or not isinstance(settle_seconds, (int, float))
        or not math.isfinite(settle_seconds)
        or settle_seconds < 0.0
    ):
        raise ValueError("settle_seconds must be finite and non-negative")
    print(
        "PICO_G2_DUAL_ARM_WORKER_PRECHECK mode=live-read-only-ik-preview "
        "latest_frame_only=True "
        "physical_robot_command_sent=False",
        file=output,
        flush=True,
    )

    frame_count = preview_count = idle_count = stale_count = rejected_count = 0
    state_unavailable_count = superseded_count = 0
    state_unavailable_latched = False
    reader_stats = {"received": 0, "rejected": 0}
    mailbox = LatestXRFrameMessageMailbox()
    initialized = False
    adapter = None
    success = None
    exit_code = 0

    def read_input_messages() -> None:
        try:
            for line in input_stream:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    mailbox.publish(decode_xr_frame_message(stripped))
                except (TypeError, ValueError, RuntimeError) as error:
                    mailbox.invalidate()
                    reader_stats["rejected"] += 1
                    print(
                        f"PICO_G2_DUAL_ARM_REJECTED reason={error} "
                        "physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
                    continue
                reader_stats["received"] += 1
        finally:
            mailbox.close()

    reader_thread = threading.Thread(
        target=read_input_messages,
        name="pico-dual-arm-frame-reader",
        daemon=True,
    )
    reader_thread.start()
    try:
        success = gdk.GDKRes.kSuccess
        result = gdk.gdk_init()
        if result != success:
            raise RuntimeError(f"GDK initialization failed: {result!r}")
        initialized = True
        robot = gdk.Robot()
        if settle_seconds:
            time.sleep(settle_seconds)
        schema = make_g2_joint_schema()
        solver = (
            solver_factory(schema)
            if solver_factory is not None
            else G2URDFIKSolver(
                joint_schema=schema,
                max_joint_velocity_rad_s=0.2,
                control_period_s=0.01,
            )
        )
        adapter = G2Adapter(
            dry_run=False,
            state_reader=robot,
            ik_solver=solver,
        )
        pipeline = DualArmPreviewPipeline(
            robot=adapter,
            router=ControlRouter(DirectJointBackend()),
            safety=SafetyGateway(),
            retargeter=DualArmRetargeter(),
        )
        print(
            "PICO_G2_DUAL_ARM_WORKER_READY waiting_for_grip=True "
            "left_grip_controls_left_arm=True right_grip_controls_right_arm=True "
            "trigger_values_previewed=True "
            "physical_robot_command_sent=False",
            file=output,
            flush=True,
        )

        last_observed_sequence = -1
        while not stop_requested():
            message, input_closed = mailbox.wait_for_update(
                last_observed_sequence,
                timeout_s=0.05,
            )
            if message is None:
                if input_closed:
                    break
                continue
            if message.frame.sequence_id == last_observed_sequence:
                if input_closed:
                    break
                continue
            last_observed_sequence = message.frame.sequence_id
            if stop_requested():
                break
            try:
                now_ns = monotonic_ns()
                message_age_ns(message, now_ns)
            except ValueError as error:
                pipeline.reset()
                rejected_count += 1
                print(
                    f"PICO_G2_DUAL_ARM_REJECTED reason={error} "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                continue

            frame_count += 1
            try:
                preview = pipeline.preview(message.frame, now_ns=now_ns)
                # GDK telemetry can block. Do not display a candidate computed
                # from operator input that expired during that read.
                message_age_ns(message, monotonic_ns())
                if not mailbox.is_current(message):
                    pipeline.reset()
                    superseded_count += 1
                    continue
            except G2StateReadError as error:
                pipeline.reset()
                state_unavailable_count += 1
                if not state_unavailable_latched:
                    print(
                        "PICO_G2_DUAL_ARM_STATE_UNAVAILABLE "
                        f"reason={error} action=frame_skipped "
                        "session_continues=True physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
                    state_unavailable_latched = True
                continue
            except ValueError as error:
                pipeline.reset()
                rejected_count += 1
                print(
                    f"PICO_G2_DUAL_ARM_REJECTED reason={error} "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                continue
            if state_unavailable_latched:
                print(
                    "PICO_G2_DUAL_ARM_STATE_RECOVERED session_continues=True "
                    "physical_robot_command_sent=False",
                    file=output,
                    flush=True,
                )
                state_unavailable_latched = False
            if preview.status == "preview":
                print(_format_preview(preview), file=output, flush=True)
                preview_count += 1
            elif preview.status == "stale":
                stale_count += 1
                print(
                    f"PICO_G2_DUAL_ARM_STALE xr_sequence={preview.frame_sequence_id} "
                    "ik_reset=True physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
            else:
                idle_count += 1
    except Exception as error:
        print(
            f"PICO_G2_DUAL_ARM_WORKER_ERROR: {error} "
            "physical_robot_command_sent=False",
            file=errors,
            flush=True,
        )
        exit_code = 2
    finally:
        if adapter is not None:
            try:
                adapter.reset_ik()
                adapter.stop()
            except Exception as error:
                print(
                    f"PICO_G2_DUAL_ARM_WORKER_STOP_ERROR: {error} "
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
                    f"PICO_G2_DUAL_ARM_WORKER_RELEASE_ERROR: {error} "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                exit_code = 2
            else:
                if release_result != success:
                    print(
                        f"PICO_G2_DUAL_ARM_WORKER_RELEASE_ERROR: {release_result!r} "
                        "physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
                    exit_code = 2

    print(
        "PICO_G2_DUAL_ARM_WORKER_SUMMARY "
        f"messages_received={reader_stats['received']} "
        f"messages_overwritten={mailbox.overwritten_count} "
        f"reader_rejected={reader_stats['rejected']} frames={frame_count} "
        f"previews={preview_count} idle={idle_count} stale={stale_count} "
        f"rejected={rejected_count} superseded={superseded_count} "
        f"state_unavailable={state_unavailable_count} "
        f"state_available_at_end={not state_unavailable_latched} "
        "physical_robot_command_sent=False",
        file=output,
        flush=True,
    )
    if exit_code:
        return exit_code
    return 0 if preview_count > 0 and not state_unavailable_latched else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    roles = parser.add_subparsers(dest="role", required=True)
    emitter = roles.add_parser("emit")
    emitter.add_argument("--duration", type=_positive_float, default=30.0)
    emitter.add_argument("--hz", type=_positive_float, default=10.0)
    emitter.add_argument(
        "--frame-fd",
        "--intent-fd",
        dest="frame_fd",
        type=_file_descriptor,
        default=None,
        help=argparse.SUPPRESS,
    )
    worker = roles.add_parser("worker")
    worker.add_argument("--settle-seconds", type=_nonnegative_float, default=2.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    stopped = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    previous = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    for signum in previous:
        signal.signal(signum, request_stop)
    try:
        if args.role == "emit":
            protocol_output = sys.stdout
            if args.frame_fd is not None:
                protocol_output = os.fdopen(
                    args.frame_fd,
                    "w",
                    buffering=1,
                    encoding="utf-8",
                    closefd=False,
                )
            return emit_session(
                args,
                adapter=XRoboToolkitInputAdapter(),
                output=protocol_output,
                diagnostics=sys.stderr,
                stop_requested=lambda: stopped,
            )
        try:
            gdk = importlib.import_module("agibot_gdk")
        except ImportError as error:
            print(
                f"PICO_G2_DUAL_ARM_WORKER_ERROR: {error} "
                "physical_robot_command_sent=False",
                file=sys.stderr,
                flush=True,
            )
            return 2
        return run_g2_worker(
            input_stream=sys.stdin,
            output=sys.stdout,
            errors=sys.stderr,
            gdk=gdk,
            settle_seconds=args.settle_seconds,
            stop_requested=lambda: stopped,
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
