"""Consume validated PICO joint-jog messages and command a live G2."""

import argparse
from dataclasses import dataclass
import importlib
import json
import math
import os
import signal
import sys
import threading
import time
from typing import Callable, TextIO

from teleop.backends.direct_joint import DirectJointBackend
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.contracts.intent import JointJogIntent
from teleop.retargeting.joint_jog import JointJogMapper, TeleopInputMode
from teleop.robots.g2 import (
    G2Adapter,
    G2CoupledConstraintError,
    G2GDKJointCommandSender,
    G2StateReadError,
    g2_live_step_rad,
    make_g2_joint_schema,
)
from teleop.xr import XRInputError, XRoboToolkitInputAdapter


LIVE_WIRE_VERSION = 2
MAX_CONTROL_STATE_AGE_NS = 200_000_000
_LIVE_WIRE_KEYS = {
    "version",
    "sequence_id",
    "emitted_monotonic_ns",
    "joint_name",
    "direction",
    "deadman",
    "exit_requested",
}


@dataclass(frozen=True)
class PicoJointControlState:
    sequence_id: int
    emitted_monotonic_ns: int
    joint_name: str
    direction: int
    deadman: bool
    exit_requested: bool

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
        if type(self.direction) is not int or self.direction not in (-1, 0, 1):
            raise ValueError("direction must be -1, 0, or 1")
        if not isinstance(self.deadman, bool):
            raise ValueError("deadman must be bool")
        if not isinstance(self.exit_requested, bool):
            raise ValueError("exit_requested must be bool")
        if self.direction != 0 and not self.deadman:
            raise ValueError("non-zero direction requires deadman")
        if self.exit_requested and (self.deadman or self.direction != 0):
            raise ValueError("exit request requires neutral controls and released deadman")


def encode_control_state(state: PicoJointControlState) -> str:
    if not isinstance(state, PicoJointControlState):
        raise TypeError("state must be PicoJointControlState")
    return json.dumps(
        {
            "version": LIVE_WIRE_VERSION,
            "sequence_id": state.sequence_id,
            "emitted_monotonic_ns": state.emitted_monotonic_ns,
            "joint_name": state.joint_name,
            "direction": state.direction,
            "deadman": state.deadman,
            "exit_requested": state.exit_requested,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_control_state(line: str) -> PicoJointControlState:
    if not isinstance(line, str):
        raise ValueError("control state must be text")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError("invalid control-state JSON") from error
    if not isinstance(payload, dict) or set(payload) != _LIVE_WIRE_KEYS:
        raise ValueError("control-state fields do not match version 2")
    if (
        type(payload["version"]) is not int
        or payload["version"] != LIVE_WIRE_VERSION
    ):
        raise ValueError("unsupported control-state version")
    return PicoJointControlState(
        sequence_id=payload["sequence_id"],
        emitted_monotonic_ns=payload["emitted_monotonic_ns"],
        joint_name=payload["joint_name"],
        direction=payload["direction"],
        deadman=payload["deadman"],
        exit_requested=payload["exit_requested"],
    )


class LatestControlStateMailbox:
    """Thread-safe single-slot mailbox; publishing replaces older state."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest: PicoJointControlState | None = None
        self._highest_sequence_id = -1
        self._closed = False
        self._overwritten_count = 0

    @property
    def overwritten_count(self) -> int:
        with self._condition:
            return self._overwritten_count

    def publish(self, state: PicoJointControlState) -> None:
        if not isinstance(state, PicoJointControlState):
            raise TypeError("state must be PicoJointControlState")
        with self._condition:
            if self._closed:
                raise RuntimeError("mailbox is closed")
            if state.sequence_id <= self._highest_sequence_id:
                raise ValueError("PICO control-state sequence was replayed")
            if self._latest is not None:
                self._overwritten_count += 1
            self._latest = state
            self._highest_sequence_id = state.sequence_id
            self._condition.notify_all()

    def snapshot(self) -> tuple[PicoJointControlState | None, bool]:
        with self._condition:
            return self._latest, self._closed

    def wait_for_update(
        self,
        previous_sequence_id: int,
        *,
        timeout_s: float,
    ) -> tuple[PicoJointControlState | None, bool]:
        if type(previous_sequence_id) is not int:
            raise ValueError("previous_sequence_id must be int")
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("timeout_s must be finite and non-negative")
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed
                or (
                    self._latest is not None
                    and self._latest.sequence_id != previous_sequence_id
                ),
                timeout=timeout_s,
            )
            return self._latest, self._closed

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def emit_live_session(
    args: argparse.Namespace,
    *,
    adapter: object,
    output: TextIO,
    diagnostics: TextIO,
    stop_requested: Callable[[], bool] = lambda: False,
) -> int:
    mode = TeleopInputMode(args.mode)
    if mode is not TeleopInputMode.JOINT_JOG:
        raise ValueError("live emitter requires joint-jog mode")
    mapper = JointJogMapper(
        make_g2_joint_schema(),
        initial_joint=getattr(args, "initial_joint", None),
    )
    frame_count = 0
    state_count = 0
    exit_requested = False
    exit_code = 0
    print(
        "PICO_LIVE_STATE_PRECHECK mode=joint-jog latest-state-only=True",
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
                status = mapper.update(frame, mode)
                if status.reason in ("selected_previous", "selected_next"):
                    print(
                        f"PICO_SELECTED joint={status.selected_joint} "
                        "physical_robot_command_sent=False",
                        file=diagnostics,
                        flush=True,
                    )
                state = PicoJointControlState(
                    sequence_id=frame.sequence_id,
                    emitted_monotonic_ns=time.monotonic_ns(),
                    joint_name=status.selected_joint,
                    direction=status.direction,
                    deadman=status.deadman,
                    exit_requested=status.exit_requested,
                )
                output.write(encode_control_state(state) + "\n")
                output.flush()
                state_count += 1
                if status.exit_requested:
                    exit_requested = True
                    print(
                        "PICO_JOINT_JOG_EXIT_REQUESTED button=B hold_seconds=1.0",
                        file=diagnostics,
                        flush=True,
                    )
                    break
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(sample_period, remaining))
    except (XRInputError, BrokenPipeError) as error:
        print(f"PICO_LIVE_STATE_ERROR: {error}", file=diagnostics, flush=True)
        exit_code = 2
    finally:
        try:
            adapter.close()
        except XRInputError as error:
            print(f"PICO_LIVE_STATE_CLOSE_ERROR: {error}", file=diagnostics, flush=True)
            exit_code = 2
    print(
        f"PICO_LIVE_STATE_SUMMARY frames={frame_count} states={state_count} "
        f"exit_requested={exit_requested} physical_robot_command_sent=False",
        file=diagnostics,
        flush=True,
    )
    if exit_code:
        return exit_code
    return 0 if frame_count else 1


@dataclass(frozen=True)
class LiveControlExecution:
    joint_name: str
    current_position: float
    target_position: float
    delta_rad: float


def _control_state_age_ns(state: PicoJointControlState, now_ns: int) -> int:
    if type(now_ns) is not int or now_ns < 0:
        raise ValueError("monotonic clock must return a non-negative integer")
    age_ns = now_ns - state.emitted_monotonic_ns
    if age_ns < 0:
        raise ValueError("PICO control state timestamp is in the future")
    if age_ns > MAX_CONTROL_STATE_AGE_NS:
        raise ValueError("PICO control state is stale")
    return age_ns


def execute_live_control_state(
    control_state: PicoJointControlState,
    *,
    adapter: G2Adapter,
    router: ControlRouter,
    safety: SafetyGateway,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    control_state_is_current: Callable[
        [PicoJointControlState], bool
    ] = lambda _state: True,
) -> LiveControlExecution | None:
    if not isinstance(control_state, PicoJointControlState):
        raise TypeError("control_state must be PicoJointControlState")
    if control_state.exit_requested:
        return None
    if not control_state.deadman or control_state.direction == 0:
        return None
    state_read_started_ns = monotonic_ns()
    _control_state_age_ns(control_state, state_read_started_ns)
    robot_state = adapter.read_state(now_ns=state_read_started_ns)
    command_time_ns = monotonic_ns()
    _control_state_age_ns(control_state, command_time_ns)
    if not control_state_is_current(control_state):
        raise ValueError("PICO control state was superseded during state read")
    delta_rad = g2_live_step_rad(control_state.joint_name) * control_state.direction
    command = router.route(
        JointJogIntent(
            deltas={control_state.joint_name: delta_rad},
            source="pico_continuous_joint_jog",
        ),
        robot_state,
        now_ns=command_time_ns,
    )
    validated = safety.validate(
        command,
        robot_state,
        adapter.get_joint_schema(),
        now_ns=command_time_ns,
    )
    current = robot_state.positions[control_state.joint_name]
    target = validated.targets[control_state.joint_name]
    adapter.send_joint_command(validated, now_ns=command_time_ns)
    return LiveControlExecution(
        joint_name=control_state.joint_name,
        current_position=current,
        target_position=target,
        delta_rad=delta_rad,
    )


def run_g2_live_worker(
    *,
    input_stream: TextIO,
    output: TextIO,
    errors: TextIO,
    gdk: object,
    settle_seconds: float,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    stop_requested: Callable[[], bool] = lambda: False,
    control_hz: float = 10.0,
) -> int:
    if (
        isinstance(settle_seconds, bool)
        or not isinstance(settle_seconds, (int, float))
        or not math.isfinite(settle_seconds)
        or settle_seconds < 0.0
    ):
        raise ValueError("settle_seconds must be finite and non-negative")
    if (
        isinstance(control_hz, bool)
        or not isinstance(control_hz, (int, float))
        or not math.isfinite(control_hz)
        or control_hz <= 0
    ):
        raise ValueError("control_hz must be finite and positive")

    print(
        "PICO_G2_LIVE_PRECHECK mode=live-direct-joint latest_state_only=True "
        f"control_hz={float(control_hz):g} arm_velocity_rad_s=0.2 "
        "body_head_velocity_rad_s=0.1 ttl_ms=100",
        file=output,
        flush=True,
    )
    sent_count = 0
    worker_rejected_count = 0
    reader_stats = {"received": 0, "rejected": 0}
    exit_requested = False
    initialized = False
    success = None
    adapter = None
    exit_code = 0
    mailbox = LatestControlStateMailbox()
    coupled_limit_latched = False

    def read_input_states() -> None:
        try:
            for line in input_stream:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    mailbox.publish(decode_control_state(stripped))
                except (TypeError, ValueError, RuntimeError) as error:
                    reader_stats["rejected"] += 1
                    print(
                        f"PICO_G2_LIVE_REJECTED reason={error}",
                        file=errors,
                        flush=True,
                    )
                    continue
                reader_stats["received"] += 1
        finally:
            mailbox.close()

    def control_state_is_current(candidate: PicoJointControlState) -> bool:
        latest, input_closed = mailbox.snapshot()
        return (
            latest is not None
            and not input_closed
            and latest.sequence_id == candidate.sequence_id
        )

    try:
        success = gdk.GDKRes.kSuccess
        result = gdk.gdk_init()
        if result != success:
            raise RuntimeError(f"GDK initialization failed: {result!r}")
        initialized = True
        robot = gdk.Robot()
        if settle_seconds:
            time.sleep(settle_seconds)
        adapter = G2Adapter(
            dry_run=False,
            state_reader=robot,
            command_sender=G2GDKJointCommandSender(gdk=gdk, robot=robot),
            allow_live_commands=True,
            max_step_rad=0.02,
            default_velocity_rad_s=0.2,
        )
        router = ControlRouter(DirectJointBackend(default_ttl_ms=100))
        safety = SafetyGateway(max_state_age_ms=200)
        print(
            "PICO_G2_LIVE_READY waiting_for_pico_intent=True",
            file=output,
            flush=True,
        )
        reader_thread = threading.Thread(
            target=read_input_states,
            name="pico-live-state-reader",
            daemon=True,
        )
        reader_thread.start()
        last_observed_sequence = -1
        last_stale_sequence = -1
        control_period_ns = round(1_000_000_000 / float(control_hz))
        next_control_ns = monotonic_ns()

        while not stop_requested():
            now_ns = monotonic_ns()
            wait_ns = max(0, next_control_ns - now_ns)
            wait_s = min(0.05, wait_ns / 1_000_000_000) if wait_ns else 0.0
            control_state, input_closed = mailbox.wait_for_update(
                last_observed_sequence,
                timeout_s=wait_s,
            )
            if control_state is None:
                if input_closed:
                    break
                time.sleep(0.001)
                continue
            last_observed_sequence = max(
                last_observed_sequence,
                control_state.sequence_id,
            )
            if control_state.exit_requested:
                exit_requested = True
                print(
                    "PICO_G2_LIVE_EXIT reason=controller_B_long_press",
                    file=output,
                    flush=True,
                )
                break
            if input_closed:
                break
            if not control_state.deadman or control_state.direction == 0:
                if coupled_limit_latched:
                    print(
                        "PICO_G2_COUPLED_LIMIT_REARMED "
                        "reason=deadman_released_or_stick_centered",
                        file=output,
                        flush=True,
                    )
                    coupled_limit_latched = False
                next_control_ns = monotonic_ns()
                continue
            if coupled_limit_latched:
                next_control_ns = monotonic_ns() + control_period_ns
                continue

            now_ns = monotonic_ns()
            if now_ns < next_control_ns:
                continue
            try:
                execution = execute_live_control_state(
                    control_state,
                    adapter=adapter,
                    router=router,
                    safety=safety,
                    monotonic_ns=monotonic_ns,
                    control_state_is_current=control_state_is_current,
                )
            except G2StateReadError:
                raise
            except G2CoupledConstraintError as error:
                worker_rejected_count += 1
                coupled_limit_latched = True
                print(
                    "PICO_G2_COUPLED_LIMIT_REJECTED "
                    f"constraint={error.constraint_name} "
                    f"joints={','.join(error.joint_names)} "
                    f"value={error.value:+.6f} "
                    f"vendor_limit={error.vendor_limit:+.6f} "
                    f"software_limit={error.software_limit:+.6f} "
                    f"margin_rad={error.vendor_limit - error.software_limit:.6f} "
                    "action=command_not_sent session_continues=True "
                    "rearm=release_Grip_or_center_stick",
                    file=errors,
                    flush=True,
                )
                next_control_ns = now_ns + control_period_ns
                continue
            except ValueError as error:
                worker_rejected_count += 1
                if control_state.sequence_id != last_stale_sequence:
                    print(
                        f"PICO_G2_LIVE_REJECTED reason={error}",
                        file=errors,
                        flush=True,
                    )
                    last_stale_sequence = control_state.sequence_id
                next_control_ns = now_ns + control_period_ns
                continue
            next_control_ns = now_ns + control_period_ns
            if execution is None:
                continue
            sent_count += 1
            print(
                "PICO_G2_LIVE_SENT "
                f"xr_sequence={control_state.sequence_id} "
                f"joint={execution.joint_name} "
                f"current={execution.current_position:+.6f} "
                f"target={execution.target_position:+.6f} "
                f"delta={execution.delta_rad:+.6f} commands_sent={sent_count} "
                "physical_robot_command_sent=True",
                file=output,
                flush=True,
            )
    except Exception as error:
        print(f"PICO_G2_LIVE_ERROR: {error}", file=errors, flush=True)
        exit_code = 2
    finally:
        if adapter is not None:
            try:
                adapter.stop()
            except Exception as error:
                print(
                    f"PICO_G2_LIVE_STOP_ERROR: {error}",
                    file=errors,
                    flush=True,
                )
                exit_code = 2
        if initialized:
            try:
                release_result = gdk.gdk_release()
            except Exception as error:
                print(f"PICO_G2_LIVE_RELEASE_ERROR: {error}", file=errors, flush=True)
                exit_code = 2
            else:
                if release_result != success:
                    print(
                        f"PICO_G2_LIVE_RELEASE_ERROR: {release_result!r}",
                        file=errors,
                        flush=True,
                    )
                    exit_code = 2

    print(
        f"PICO_G2_LIVE_SUMMARY commands_sent={sent_count} "
        f"states_received={reader_stats['received']} "
        f"states_overwritten={mailbox.overwritten_count} "
        f"rejected={reader_stats['rejected'] + worker_rejected_count} "
        f"exit_requested={exit_requested}",
        file=output,
        flush=True,
    )
    if exit_code:
        return exit_code
    return 0 if sent_count or exit_requested else 1


def _settle_seconds(value: str) -> float:
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(converted) or converted < 0.0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return converted


def _positive_float(value: str) -> float:
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(converted) or converted <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return converted


def _intent_file_descriptor(value: str) -> int:
    try:
        converted = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if converted < 3:
        raise argparse.ArgumentTypeError("must be file descriptor 3 or greater")
    return converted


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run live PICO-to-G2 joint control.")
    subparsers = parser.add_subparsers(dest="role", required=True)
    emit_parser = subparsers.add_parser("emit")
    emit_parser.add_argument(
        "--mode",
        choices=(TeleopInputMode.JOINT_JOG.value,),
        default=TeleopInputMode.JOINT_JOG.value,
    )
    emit_parser.add_argument("--duration", type=_positive_float, default=20.0)
    emit_parser.add_argument("--hz", type=_positive_float, default=50.0)
    emit_parser.add_argument(
        "--initial-joint",
        choices=make_g2_joint_schema().commandable_names,
        default=None,
    )
    emit_parser.add_argument(
        "--intent-fd",
        type=_intent_file_descriptor,
        default=None,
        help=argparse.SUPPRESS,
    )
    worker_parser = subparsers.add_parser("worker")
    worker_parser.add_argument(
        "--settle-seconds", type=_settle_seconds, default=2.0
    )
    worker_parser.add_argument("--control-hz", type=_positive_float, default=10.0)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    termination_signum = None

    def record_stop(signum: int, _frame: object) -> None:
        nonlocal termination_signum
        termination_signum = termination_signum or signum

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, record_stop)
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
                print(f"PICO_LIVE_STATE_ERROR: {error}", file=sys.stderr, flush=True)
                return 2
        result = emit_live_session(
            args,
            adapter=XRoboToolkitInputAdapter(),
            output=protocol_output,
            diagnostics=sys.stderr,
            stop_requested=lambda: termination_signum is not None,
        )
    else:
        try:
            gdk = importlib.import_module("agibot_gdk")
        except ImportError as error:
            print(f"PICO_G2_LIVE_ERROR: {error}", file=sys.stderr, flush=True)
            return 2
        result = run_g2_live_worker(
            input_stream=sys.stdin,
            output=sys.stdout,
            errors=sys.stderr,
            gdk=gdk,
            settle_seconds=args.settle_seconds,
            control_hz=args.control_hz,
            stop_requested=lambda: termination_signum is not None,
        )
    if termination_signum is not None:
        return 128 + termination_signum
    return result


if __name__ == "__main__":
    raise SystemExit(main())
