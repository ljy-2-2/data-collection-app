#!/usr/bin/env python3
"""PICO head direction + live G2 state -> head joint preview; never sends motion."""

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

from teleop.retargeting.head_follow import HeadFollowMapper
from teleop.robots.g2 import G2Adapter, G2StateReadError, make_g2_joint_schema
from teleop.robots.g2_head import (
    G2_HEAD_PITCH,
    G2_HEAD_ROLL,
    G2_HEAD_YAW,
    G2HeadJointMapper,
    G2HeadJointPreview,
)
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
        raise argparse.ArgumentTypeError("must be an integer") from error
    if converted < 3:
        raise argparse.ArgumentTypeError("must be file descriptor 3 or greater")
    return converted


class _LatestMailbox:
    """Single-slot mailbox: old XR frames never build a control backlog."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest: XRFrameMessage | None = None
        self._highest_sequence = -1
        self._closed = False
        self.overwritten_count = 0

    def publish(self, message: XRFrameMessage) -> None:
        with self._condition:
            sequence = message.frame.sequence_id
            if sequence <= self._highest_sequence:
                raise ValueError("XR frame sequence was replayed")
            if self._latest is not None:
                self.overwritten_count += 1
            self._latest = message
            self._highest_sequence = sequence
            self._condition.notify_all()

    def invalidate(self) -> None:
        with self._condition:
            self._latest = None
            self._condition.notify_all()

    def wait_for_update(
        self, previous_sequence: int, *, timeout_s: float
    ) -> tuple[XRFrameMessage | None, bool]:
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed
                or (
                    self._latest is not None
                    and self._latest.frame.sequence_id != previous_sequence
                ),
                timeout=timeout_s,
            )
            return self._latest, self._closed

    def is_current(self, message: XRFrameMessage) -> bool:
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
    frames = 0
    last_stale = False
    result = 0
    print(
        "PICO_HEAD_FRAME_PIPE_PRECHECK mode=head-read-and-preview "
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
                last_stale = frame.stale
                output.write(
                    encode_xr_frame_message(
                        XRFrameMessage(time.monotonic_ns(), frame)
                    )
                    + "\n"
                )
                output.flush()
                frames += 1
            remaining = deadline - time.monotonic()
            if remaining > 0.0:
                time.sleep(min(period, remaining))
    except (XRInputError, BrokenPipeError) as error:
        print(
            f"PICO_HEAD_FRAME_PIPE_ERROR: {error} physical_robot_command_sent=False",
            file=diagnostics,
            flush=True,
        )
        result = 2
    finally:
        try:
            adapter.close()
        except XRInputError as error:
            print(
                f"PICO_HEAD_FRAME_PIPE_CLOSE_ERROR: {error} "
                "physical_robot_command_sent=False",
                file=diagnostics,
                flush=True,
            )
            result = 2
    print(
        f"PICO_HEAD_FRAME_PIPE_SUMMARY frames={frames} last_frame_stale={last_stale} "
        "physical_robot_command_sent=False",
        file=diagnostics,
        flush=True,
    )
    if result:
        return result
    return 0 if frames > 0 and not last_stale else 1


def _format_preview(message: XRFrameMessage, intent, preview: G2HeadJointPreview) -> str:
    clamped = ",".join(preview.clamped_joints) or "none"
    return (
        "PICO_G2_HEAD_PREVIEW "
        f"xr_sequence={message.frame.sequence_id} "
        f"pico_left_deg={math.degrees(intent.yaw_left_rad):+.2f} "
        f"pico_up_deg={math.degrees(intent.pitch_up_rad):+.2f} "
        f"idx11_current={preview.anchor_positions[G2_HEAD_YAW]:+.6f} "
        f"idx11_target={preview.targets[G2_HEAD_YAW]:+.6f} "
        f"idx12_hold={preview.roll_hold_rad:+.6f} "
        f"idx13_current={preview.anchor_positions[G2_HEAD_PITCH]:+.6f} "
        f"idx13_target={preview.targets[G2_HEAD_PITCH]:+.6f} "
        f"clamped={clamped} mapping_hypothesis=left:+idx11,up:-idx13 "
        f"direction_calibrated={preview.direction_calibrated} "
        "joint_command_created=False physical_robot_command_sent=False"
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
) -> int:
    if not math.isfinite(settle_seconds) or settle_seconds < 0.0:
        raise ValueError("settle_seconds must be finite and non-negative")
    print(
        "PICO_G2_HEAD_WORKER_PRECHECK mode=live-read-only-head-preview "
        "latest_frame_only=True physical_robot_command_sent=False",
        file=output,
        flush=True,
    )
    mailbox = _LatestMailbox()
    stats = {"received": 0, "reader_rejected": 0, "frames": 0, "previews": 0,
             "stale": 0, "rejected": 0, "superseded": 0, "state_unavailable": 0}
    initialized = False
    adapter = None
    success = None
    exit_code = 0
    state_available_at_end = False

    def read_messages() -> None:
        try:
            for line in input_stream:
                if not line.strip():
                    continue
                try:
                    mailbox.publish(decode_xr_frame_message(line.strip()))
                    stats["received"] += 1
                except (TypeError, ValueError, RuntimeError) as error:
                    mailbox.invalidate()
                    stats["reader_rejected"] += 1
                    print(
                        f"PICO_G2_HEAD_REJECTED reason={error} "
                        "physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
        finally:
            mailbox.close()

    threading.Thread(target=read_messages, name="pico-head-frame-reader", daemon=True).start()
    head_mapper = HeadFollowMapper()
    anchor_state = None
    try:
        success = gdk.GDKRes.kSuccess
        if gdk.gdk_init() != success:
            raise RuntimeError("GDK initialization failed")
        initialized = True
        robot = gdk.Robot()
        if settle_seconds:
            time.sleep(settle_seconds)
        adapter = G2Adapter(dry_run=False, state_reader=robot)
        joint_mapper = G2HeadJointMapper(make_g2_joint_schema())
        print(
            "PICO_G2_HEAD_WORKER_READY waiting_for_fresh_head_frame=True "
            "roll_joint_commanded=False physical_robot_command_sent=False",
            file=output,
            flush=True,
        )
        last_sequence = -1
        while not stop_requested():
            message, closed = mailbox.wait_for_update(last_sequence, timeout_s=0.05)
            if message is None:
                if closed:
                    break
                continue
            if message.frame.sequence_id == last_sequence:
                if closed:
                    break
                continue
            last_sequence = message.frame.sequence_id
            try:
                now_ns = monotonic_ns()
                message_age_ns(message, now_ns)
                intent = head_mapper.update(message.frame, now_ns=now_ns)
                if intent is None:
                    if head_mapper.status == "stale_or_invalid_time":
                        anchor_state = None
                        stats["stale"] += 1
                    continue
                if head_mapper.status == "anchored" or anchor_state is None:
                    anchor_state = adapter.read_state(now_ns=now_ns)
                    state_available_at_end = True
                    message_age_ns(message, monotonic_ns())
                    if not mailbox.is_current(message):
                        head_mapper.reset()
                        anchor_state = None
                        stats["superseded"] += 1
                        continue
                preview = joint_mapper.preview(intent, anchor_state)
            except G2StateReadError as error:
                head_mapper.reset()
                anchor_state = None
                state_available_at_end = False
                stats["state_unavailable"] += 1
                print(
                    f"PICO_G2_HEAD_STATE_UNAVAILABLE reason={error} "
                    "action=reanchor_on_next_fresh_frame physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                continue
            except ValueError as error:
                head_mapper.reset()
                anchor_state = None
                stats["rejected"] += 1
                print(
                    f"PICO_G2_HEAD_REJECTED reason={error} "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                continue
            stats["frames"] += 1
            print(_format_preview(message, intent, preview), file=output, flush=True)
            stats["previews"] += 1
    except Exception as error:
        print(
            f"PICO_G2_HEAD_WORKER_ERROR: {error} physical_robot_command_sent=False",
            file=errors,
            flush=True,
        )
        exit_code = 2
    finally:
        if adapter is not None:
            adapter.stop()
        if initialized:
            try:
                release_result = gdk.gdk_release()
            except Exception as error:
                print(
                    f"PICO_G2_HEAD_WORKER_RELEASE_ERROR: {error} "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                exit_code = 2
            else:
                if release_result != success:
                    print(
                        f"PICO_G2_HEAD_WORKER_RELEASE_ERROR: {release_result!r} "
                        "physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
                    exit_code = 2
    print(
        "PICO_G2_HEAD_WORKER_SUMMARY "
        + " ".join(f"{name}={value}" for name, value in stats.items())
        + f" messages_overwritten={mailbox.overwritten_count} "
        + f"state_available_at_end={state_available_at_end} "
        + "physical_robot_command_sent=False",
        file=output,
        flush=True,
    )
    if exit_code:
        return exit_code
    return 0 if stats["previews"] > 0 and state_available_at_end else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    roles = parser.add_subparsers(dest="role", required=True)
    emitter = roles.add_parser("emit")
    emitter.add_argument("--duration", type=_positive_float, default=30.0)
    emitter.add_argument("--hz", type=_positive_float, default=10.0)
    emitter.add_argument(
        "--frame-fd", "--intent-fd", dest="frame_fd",
        type=_file_descriptor, default=None, help=argparse.SUPPRESS,
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
                    args.frame_fd, "w", buffering=1, encoding="utf-8", closefd=False
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
                f"PICO_G2_HEAD_WORKER_ERROR: {error} physical_robot_command_sent=False",
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
