#!/usr/bin/env python3
"""PICO XRFrame -> G2 URDF IK -> validated live dual-arm servo control."""

import argparse
from collections.abc import Callable
import importlib
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid
from typing import TextIO

from apps.pico_g2_dual_arm_preview import (
    LatestXRFrameMessageMailbox,
    emit_session,
)
from teleop.backends.direct_joint import DirectJointBackend
from teleop.control.dual_arm_live import (
    AxisAlignedEndEffectorWorkspace,
    DualArmLivePipeline,
    DualArmLiveResult,
    PartialPhysicalSendError,
)
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyError, SafetyGateway
from teleop.contracts.schema import JointSchema
from teleop.retargeting.dual_arm import DualArmRetargeter, RELEASE_THRESHOLD
from teleop.retargeting.gripper import TRIGGER_NEUTRAL_THRESHOLD
from teleop.robots.base import IKSolver
from teleop.robots.g2 import (
    G2Adapter,
    G2CommandSendError,
    G2CoupledConstraintError,
    G2GDKJointServoCommandSender,
    G2GDKGripperCommandSender,
    G2StateReadError,
    G2TelemetryUnavailableError,
    G2TrackingError,
    make_g2_joint_schema,
)
from teleop.robots.g2_urdf_ik import G2URDFIKSolver
from teleop.robots.g2_head_lane import CombinedControlFault, G2HeadLane, GuardedG2ControlRobot
from teleop.recording.head_journal import AsyncHeadJournal
from teleop.xr import XRInputError, XRoboToolkitInputAdapter
from teleop.xr.wire import decode_xr_frame_message, message_age_ns


MAX_DUAL_ARM_VELOCITY_RAD_S = 1.0
MAX_LIVE_STEP_RAD = 0.02
HARD_TRACKING_ERROR_RAD = 0.20


class RecoverableXRFrameTimingError(ValueError):
    """A frame became unusable before any physical command was attempted."""


class NewerControlRelease(ValueError):
    """Discard the pending arm/tool part of a cycle; head may have sent already."""


class _StaleEpisodeTracker:
    """Collapse a stale run into one bounded warning and one recovery event."""

    def __init__(self, *, log_period_ns: int = 1_000_000_000) -> None:
        if type(log_period_ns) is not int or log_period_ns <= 0:
            raise ValueError("log_period_ns must be a positive integer")
        self._log_period_ns = log_period_ns
        self._run_frames = 0
        self._run_logs_suppressed = 0
        self._last_log_ns: int | None = None
        self.episodes = 0
        self.max_run_frames = 0
        self.logs_suppressed = 0

    def note_stale(self, reason: str, *, now_ns: int) -> str | None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("stale reason must be a non-empty string")
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a non-negative integer")
        if self._run_frames == 0:
            self.episodes += 1
            self._last_log_ns = None
        self._run_frames += 1
        self.max_run_frames = max(self.max_run_frames, self._run_frames)
        if (
            self._last_log_ns is None
            or now_ns - self._last_log_ns >= self._log_period_ns
        ):
            self._last_log_ns = now_ns
            return reason
        self._run_logs_suppressed += 1
        self.logs_suppressed += 1
        return None

    def note_fresh(self) -> tuple[int, int] | None:
        if self._run_frames == 0:
            return None
        recovered = (self._run_frames, self._run_logs_suppressed)
        self._run_frames = 0
        self._run_logs_suppressed = 0
        self._last_log_ns = None
        return recovered


class DualArmSoftwareStopGesture:
    """Long-B stop that is accepted only while both arm clutches are released."""

    def __init__(self, *, hold_seconds: float = 1.0) -> None:
        if not math.isfinite(hold_seconds) or hold_seconds <= 0.0:
            raise ValueError("hold_seconds must be finite and positive")
        self._hold_ns = int(hold_seconds * 1_000_000_000)
        self._pressed_since_ns: int | None = None

    def update(self, frame, *, now_ns: int) -> bool:
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a non-negative integer")
        eligible = frame.right_controller.buttons.secondary and _both_grips_released(frame)
        if not eligible:
            self._pressed_since_ns = None
            return False
        if self._pressed_since_ns is None:
            self._pressed_since_ns = now_ns
            return False
        return now_ns - self._pressed_since_ns >= self._hold_ns


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


def _both_grips_released(frame) -> bool:
    return (
        frame.left_controller.grip < RELEASE_THRESHOLD
        and frame.right_controller.grip < RELEASE_THRESHOLD
    )


def _controls_neutral_for_rearm(frame) -> bool:
    return (
        _both_grips_released(frame)
        and frame.left_controller.trigger <= TRIGGER_NEUTRAL_THRESHOLD
        and frame.right_controller.trigger <= TRIGGER_NEUTRAL_THRESHOLD
    )


def _format_sent(result: DualArmLiveResult, *, command_count: int, adapter: G2Adapter) -> str:
    if result.state is None:
        raise ValueError("only a complete sent result can be formatted")
    if result.validated is not None:
        max_delta = max(
            abs(target - result.state.positions[name])
            for name, target in result.validated.targets.items()
        )
        if adapter.last_request is None:
            raise ValueError("arm result is missing its G2 request")
        max_velocity = max(adapter.last_request.joint_velocities)
        joint_targets = len(result.validated.targets)
    else:
        max_delta = 0.0
        max_velocity = 0.0
        joint_targets = 0
    return (
        "PICO_G2_DUAL_ARM_LIVE_SENT "
        f"xr_sequence={result.frame_sequence_id} "
        f"active_sides={','.join(result.active_sides)} "
        f"joint_targets={joint_targets} "
        f"gripper_sides={','.join(result.gripper_sides)} "
        f"max_joint_delta={max_delta:.6f} "
        f"max_velocity_rad_s={max_velocity:.3f} commands_sent={command_count} "
        "safety_validated=True physical_robot_command_sent=True"
    )


def run_g2_live_worker(
    *,
    input_stream: TextIO,
    output: TextIO,
    errors: TextIO,
    gdk: object,
    settle_seconds: float,
    control_hz: float = 100.0,
    telemetry_period_s: float = 0.1,
    max_joint_velocity_rad_s: float = MAX_DUAL_ARM_VELOCITY_RAD_S,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    stop_requested: Callable[[], bool] = lambda: False,
    solver_factory: Callable[[JointSchema], IKSolver] | None = None,
    head_follow: bool = False,
    head_log_path: Path | None = None,
    retarget_scale: float = 1.0,
    collection_socket: str | None = None,
) -> int:
    if type(head_follow) is not bool or (head_follow and control_hz != 100):
        raise ValueError("head-follow requires control_hz=100 and a boolean switch")
    numeric = {
        "settle_seconds": settle_seconds,
        "control_hz": control_hz,
        "telemetry_period_s": telemetry_period_s,
        "max_joint_velocity_rad_s": max_joint_velocity_rad_s,
        "retarget_scale": retarget_scale,
    }
    for name, value in numeric.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"{name} must be finite")
    if settle_seconds < 0.0:
        raise ValueError("settle_seconds must be non-negative")
    if control_hz <= 0.0:
        raise ValueError("control_hz must be positive")
    if telemetry_period_s <= 0.0:
        raise ValueError("telemetry_period_s must be positive")
    if not 0.0 < max_joint_velocity_rad_s <= MAX_DUAL_ARM_VELOCITY_RAD_S:
        raise ValueError("max_joint_velocity_rad_s must be within (0, 1.0]")
    max_step_rad = max_joint_velocity_rad_s / control_hz
    if max_step_rad > MAX_LIVE_STEP_RAD:
        raise ValueError(
            "velocity/control_hz exceeds the 0.02 rad live step limit"
        )

    print(
        "PICO_G2_DUAL_ARM_LIVE_PRECHECK mode=live-dual-arm "
        f"latest_frame_only=True control_hz={control_hz:g} "
        f"telemetry_hz={1.0 / telemetry_period_s:g} "
        f"max_velocity_rad_s={max_joint_velocity_rad_s:.3f} "
        f"max_step_rad={max_step_rad:.6f} ttl_ms=100 "
        f"tracking_soft_rad=0.100 tracking_hard_rad={HARD_TRACKING_ERROR_RAD:.3f} "
        "physical_robot_commands_enabled=True",
        file=output,
        flush=True,
    )

    mailbox = LatestXRFrameMessageMailbox()
    reader_stats = {"received": 0, "rejected": 0}
    frames = sent = idle = stale = rejected = superseded = state_unavailable = 0
    telemetry_refreshes = 0
    arm_lane_rejected = gripper_lane_rejected = 0
    workspace_clamps = tracking_recoveries = hard_tracking_faults = 0
    single_gripper_sends = dual_gripper_sends = 0
    sent_logs_suppressed = 0
    stale_tracker = _StaleEpisodeTracker()
    last_sent_log_ns: int | None = None
    last_event_log_ns: dict[str, int] = {}
    max_timing_ms = {
        "state_read": 0.0,
        "mapping": 0.0,
        "arm": 0.0,
        "gripper": 0.0,
        "total": 0.0,
    }
    safety_latched = False
    state_unavailable_latched = False
    software_stop_requested = False
    stop_gesture = DualArmSoftwareStopGesture()
    initialized = False
    adapter = None
    pipeline = None
    success = None
    exit_code = 0
    head_lane = guarded_robot = journal = collection = None
    session_id = uuid.uuid4().hex if head_follow else None
    exit_reason = "input_closed"

    def audit(event, **data):
        if journal is not None:
            journal.write(event, session_id=session_id, **data)

    def combined_health():
        journal.check_health()
        if stop_requested():
            raise CombinedControlFault("operator stop requested")
        if reader_stats["rejected"]:
            raise CombinedControlFault("invalid XR input; restart required")

    def combined_frame_guard():
        combined_health()
        latest, closed = mailbox.wait_for_update(-1, timeout_s=0)
        if closed or latest is None:
            raise CombinedControlFault("XR input closed or invalidated")
        message_age_ns(latest, monotonic_ns())
        if latest.frame.stale:
            raise CombinedControlFault("latest XR frame stale during combined cycle")
        message_age_ns(message, monotonic_ns())
        if message.frame.stale:
            raise CombinedControlFault("XR frame stale during combined cycle")

    def record_stale(reason: str) -> None:
        nonlocal stale
        stale += 1
        event_reason = stale_tracker.note_stale(reason, now_ns=monotonic_ns())
        if event_reason is None:
            return
        print(
            f"PICO_G2_DUAL_ARM_STALE_SKIPPED reason={event_reason} "
            "action=current_frame_skipped "
            "fresh_frame_reanchors_automatically=True "
            "gripper_state_preserved=True "
            "physical_robot_command_sent=False",
            file=output,
            flush=True,
        )

    def record_fresh_recovery() -> None:
        recovered = stale_tracker.note_fresh()
        if recovered is None:
            return
        run_frames, run_logs_suppressed = recovered
        print(
            "PICO_G2_DUAL_ARM_INPUT_RECOVERED "
            f"stale_run_frames={run_frames} "
            f"stale_logs_suppressed={run_logs_suppressed} "
            "arms_reanchor_automatically=True "
            "gripper_state_preserved=True",
            file=output,
            flush=True,
        )

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
                        f"PICO_G2_DUAL_ARM_LIVE_REJECTED reason={error} "
                        "physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
                    continue
                reader_stats["received"] += 1
        finally:
            mailbox.close()

    threading.Thread(
        target=read_input_messages,
        name="pico-dual-arm-live-frame-reader",
        daemon=True,
    ).start()

    try:
        if head_follow:
            if head_log_path is None:
                head_log_path = (Path(__file__).resolve().parents[1] /
                                 "artifacts" / "head-follow" / f"combined-{session_id}.jsonl")
            head_log_path = Path(head_log_path)
            head_log_path.parent.mkdir(parents=True, exist_ok=True)
            journal = AsyncHeadJournal(head_log_path)
            audit("combined_start", head_enabled=True, head_rate_rad_s=.3,
                  arm_rate_rad_s=max_joint_velocity_rad_s, control_hz=control_hz)
            combined_health()
            print(f"PICO_G2_COMBINED_JOURNAL session_id={session_id} path={head_log_path}",
                  file=output, flush=True)
        success = gdk.GDKRes.kSuccess
        init_result = gdk.gdk_init()
        if init_result != success:
            raise RuntimeError(f"GDK initialization failed: {init_result!r}")
        initialized = True
        robot = gdk.Robot()
        if settle_seconds:
            time.sleep(settle_seconds)
        if head_follow:
            guarded_robot = GuardedG2ControlRobot(robot, clock_ns=monotonic_ns, audit=audit)
            guarded_robot.guard = combined_health
            robot = guarded_robot
            head_lane = G2HeadLane(robot, clock_ns=monotonic_ns, audit=audit,
                                   health=combined_health, stopped=stop_requested)
        schema = make_g2_joint_schema(
            max_step_rad=max_step_rad,
            arm_velocity_rad_s=max_joint_velocity_rad_s,
        )
        solver = (
            solver_factory(schema)
            if solver_factory is not None
            else G2URDFIKSolver(
                joint_schema=schema,
                max_joint_velocity_rad_s=max_joint_velocity_rad_s,
                control_period_s=1.0 / control_hz,
            )
        )
        sender = G2GDKJointServoCommandSender(
            gdk=gdk,
            robot=robot,
            control_period_s=1.2 / control_hz,
            max_arm_velocity_rad_s=max_joint_velocity_rad_s,
        )
        gripper_sender = G2GDKGripperCommandSender(gdk=gdk, robot=robot)
        adapter = G2Adapter(
            dry_run=False,
            state_reader=robot,
            command_sender=sender,
            gripper_sender=gripper_sender,
            allow_live_commands=True,
            max_step_rad=max_step_rad,
            default_velocity_rad_s=max_joint_velocity_rad_s,
            live_arm_velocity_cap_rad_s=max_joint_velocity_rad_s,
            continuous_servo_commands=True,
            ik_solver=solver,
        )
        if collection_socket is not None:
            from apps.platform_collection import PlatformCollectionBridge

            initial_state = adapter.read_state(now_ns=monotonic_ns())
            initial_grippers = adapter.read_gripper_state()
            collection = PlatformCollectionBridge(
                collection_socket,
                joint_positions=initial_state.positions,
                gripper_fractions=initial_grippers,
            )
        pipeline = DualArmLivePipeline(
            robot=adapter,
            router=ControlRouter(DirectJointBackend(default_ttl_ms=100)),
            safety=SafetyGateway(max_state_age_ms=200),
            retargeter=DualArmRetargeter(scale=retarget_scale),
            workspace=AxisAlignedEndEffectorWorkspace.g2_conservative(),
            telemetry_period_s=telemetry_period_s,
        )
        if head_lane is not None:
            # Validate the shared state and both tools before any lane can send.
            adapter.read_state(now_ns=monotonic_ns())
            adapter.read_gripper_state()
            combined_health()
            audit("combined_ready", head_automatic_without_grip=True)
            print("PICO_G2_HEAD_COMBINED_READY head_automatic_without_grip=True "
                  "head_target_rate_rad_s=0.3 yaw_absolute_limits_deg=-8,45 "
                  "pitch_start_offset_limit_deg=20 roll_follow=False "
                  "stop=Ctrl-C_or_neutral_long_B physical_combined_acceptance=pending",
                  file=output, flush=True)
        print(
            "PICO_G2_DUAL_ARM_LIVE_READY waiting_for_grip=True "
            "left_grip_controls_left_arm=True right_grip_controls_right_arm=True "
            "left_trigger_controls_left_gripper=True "
            "right_trigger_controls_right_gripper=True "
            "release_each_trigger_to_arm_its_gripper=True",
            file=output,
            flush=True,
        )

        last_sequence = -1
        while not stop_requested():
            if head_lane is not None:
                combined_health()
            message, input_closed = mailbox.wait_for_update(
                last_sequence,
                timeout_s=0.05,
            )
            if message is None:
                if input_closed:
                    break
                continue
            if message.frame.sequence_id == last_sequence:
                if input_closed:
                    break
                if head_lane is not None and monotonic_ns()-message.frame.received_monotonic_ns > 300_000_000:
                    head_lane.session.reset()
                    pipeline.reset_arm_control()
                continue
            last_sequence = message.frame.sequence_id
            if stop_requested():
                break
            frames += 1

            gesture_now_ns = monotonic_ns()
            try:
                message_age_ns(message, gesture_now_ns)
            except ValueError:
                stop_gesture.update(message.frame, now_ns=gesture_now_ns)
            else:
                if collection is not None and not message.frame.stale:
                    collection_event = collection.process_buttons(message.frame)
                    if collection_event is not None:
                        print(
                            "PICO_G2_COLLECTION "
                            f"button={'A' if collection_event == 'start' else 'B'} "
                            f"action={collection_event}",
                            file=output,
                            flush=True,
                        )
                if collection is None and stop_gesture.update(message.frame, now_ns=gesture_now_ns):
                    pipeline.reset()
                    software_stop_requested = True
                    exit_reason = "controller_B"
                    print(
                        "PICO_G2_DUAL_ARM_SOFTWARE_STOP button=B "
                        "hold_seconds=1.0 both_grips_released=True",
                        file=output,
                        flush=True,
                    )
                    break

            if head_lane is not None:
                # Stale input is skipped before *any* lane sends. The next fresh
                # frame reanchors; SDK/head faults below instead exit the worker.
                try:
                    message_age_ns(message, monotonic_ns())
                    if message.frame.stale:
                        raise ValueError("XR frame marked stale")
                except ValueError as error:
                    head_lane.session.reset()
                    pipeline.reset_arm_control()
                    record_stale(str(error))
                    continue
                if input_closed:
                    break
                guarded_robot.begin_cycle(combined_frame_guard)
                head_lane.step(message)
                guarded_robot.check()

            if safety_latched:
                if _controls_neutral_for_rearm(message.frame):
                    pipeline.reset()
                    safety_latched = False
                    print(
                        "PICO_G2_DUAL_ARM_LIVE_REARMED "
                        "reason=both_grips_and_triggers_released",
                        file=output,
                        flush=True,
                    )
                else:
                    continue

            try:
                prepare_now_ns = monotonic_ns()
                try:
                    message_age_ns(message, prepare_now_ns)
                except ValueError as error:
                    raise RecoverableXRFrameTimingError(str(error)) from error

                def final_freshness_gate() -> int:
                    if guarded_robot is not None:
                        guarded_robot.check()
                        latest, _ = mailbox.wait_for_update(-1, timeout_s=0)
                        if latest is not None and latest.frame.sequence_id > message.frame.sequence_id:
                            for side in ("left", "right"):
                                old = getattr(message.frame, f"{side}_controller")
                                new = getattr(latest.frame, f"{side}_controller")
                                trigger_released = (old.trigger > TRIGGER_NEUTRAL_THRESHOLD
                                                    and new.trigger <= TRIGGER_NEUTRAL_THRESHOLD)
                                # Keep the original 30 ms Grip release debounce.
                                grip_released = (old.grip >= RELEASE_THRESHOLD
                                                 and new.grip < RELEASE_THRESHOLD
                                                 and latest.frame.received_monotonic_ns -
                                                 message.frame.received_monotonic_ns >= 30_000_000)
                                if trigger_released or grip_released:
                                    raise NewerControlRelease("newer controller release supersedes pending control")
                    send_now_ns = monotonic_ns()
                    try:
                        message_age_ns(message, send_now_ns)
                    except ValueError as error:
                        raise RecoverableXRFrameTimingError(str(error)) from error
                    # The mailbox supplies the newest frame when this cycle starts.
                    # A normal 100 Hz update may arrive while GDK state, FK, IK, and
                    # safety checks are running; that must not cancel an otherwise
                    # fresh command or latch the operator out.  Each physical call
                    # still checks the strict message age/command TTL, and the next
                    # cycle naturally consumes the newest mailbox frame.
                    return send_now_ns

                result = pipeline.execute(
                    message.frame,
                    now_ns=prepare_now_ns,
                    before_send=final_freshness_gate,
                )
                if guarded_robot is not None:
                    # A gripper SDK exception can be contained inside the old
                    # lane pipeline. In combined mode, a failed shared SDK is fatal.
                    guarded_robot.check()
                    if any(f.physical_outcome == "unknown" for f in result.gripper_failures):
                        raise CombinedControlFault("gripper physical outcome unknown")
                    audit("combined_cycle", xr_sequence=message.frame.sequence_id,
                          arm_sides=list(result.active_sides),
                          gripper_sides=list(result.gripper_sides),
                          arm_status=result.status,
                          head_status=head_lane.session.status,
                          total_ms=(monotonic_ns()-guarded_robot.cycle_ns)/1e6,
                          arm_ms=result.timings.arm_ms,
                          gripper_ms=result.timings.gripper_ms)
            except NewerControlRelease as error:
                pipeline.reset_arm_control()
                audit("controls_updated_skip", xr_sequence=message.frame.sequence_id, reason=str(error))
                continue
            except RecoverableXRFrameTimingError as error:
                pipeline.reset_arm_control()
                record_stale(str(error))
                continue
            except G2TelemetryUnavailableError as error:
                if head_lane is not None:
                    raise CombinedControlFault(f"shared telemetry unavailable: {error}") from error
                state_unavailable += 1
                if not state_unavailable_latched:
                    print(
                        f"PICO_G2_DUAL_ARM_LIVE_STATE_UNAVAILABLE reason={error} "
                        "action=current_frame_skipped session_continues=True "
                        "physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
                    state_unavailable_latched = True
                continue
            except G2TrackingError as error:
                if error.error_rad >= HARD_TRACKING_ERROR_RAD:
                    if head_lane is not None:
                        raise CombinedControlFault(f"arm hard tracking fault: {error}") from error
                    pipeline.reset()
                    state_unavailable += 1
                    hard_tracking_faults += 1
                    safety_latched = True
                    print(
                        f"PICO_G2_DUAL_ARM_LIVE_STATE_UNSAFE reason={error} "
                        f"hard_tracking_limit={HARD_TRACKING_ERROR_RAD:.3f} "
                        "action=command_not_sent "
                        "rearm=release_both_grips_and_triggers "
                        "physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
                else:
                    pipeline.reset_arm_control()
                    tracking_recoveries += 1
                    print(
                        f"PICO_G2_DUAL_ARM_TRACKING_RESYNC reason={error} "
                        f"hard_tracking_limit={HARD_TRACKING_ERROR_RAD:.3f} "
                        "action=current_cycle_skipped "
                        "fresh_robot_state_reanchors_automatically=True "
                        "held_grips_continue=True "
                        "physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
                continue
            except G2StateReadError as error:
                if head_lane is not None:
                    raise CombinedControlFault(f"shared state fault: {error}") from error
                pipeline.reset()
                state_unavailable += 1
                safety_latched = True
                print(
                    f"PICO_G2_DUAL_ARM_LIVE_STATE_UNSAFE reason={error} "
                    "action=command_not_sent "
                    "rearm=release_both_grips_and_triggers "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                continue
            except G2CommandSendError as error:
                if head_lane is not None:
                    raise CombinedControlFault(f"arm SDK outcome unknown: {error}") from error
                pipeline.reset()
                rejected += 1
                safety_latched = True
                exit_code = 2
                print(
                    f"PICO_G2_DUAL_ARM_LIVE_SEND_ERROR reason={error} "
                    "action=stream_stopped "
                    "rearm=release_both_grips_and_triggers "
                    "physical_robot_command_outcome=unknown",
                    file=errors,
                    flush=True,
                )
                continue
            except PartialPhysicalSendError as error:
                if head_lane is not None:
                    raise CombinedControlFault(f"partial combined send: {error}") from error
                pipeline.reset()
                rejected += 1
                safety_latched = True
                exit_code = 2
                print(
                    f"PICO_G2_DUAL_ARM_LIVE_PARTIAL_SEND reason={error} "
                    "action=stream_stopped "
                    "rearm=release_both_grips_and_triggers "
                    "physical_robot_command_outcome=partial_or_unknown",
                    file=errors,
                    flush=True,
                )
                continue
            except (G2CoupledConstraintError, SafetyError) as error:
                # The pipeline already discarded only the arm integration state.
                # Keep both independent gripper lanes armed across a known
                # pre-send arm rejection; no physical arm outcome is unknown.
                pipeline.reset_arm_control()
                rejected += 1
                print(
                    f"PICO_G2_DUAL_ARM_LIVE_CYCLE_SKIPPED reason={error} "
                    "action=current_cycle_not_sent "
                    "fresh_frame_reanchors_automatically=True "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                continue
            except ValueError as error:
                if head_lane is not None:
                    raise CombinedControlFault(f"combined control invalid: {error}") from error
                pipeline.reset()
                rejected += 1
                safety_latched = True
                if "superseded" in str(error):
                    superseded += 1
                print(
                    f"PICO_G2_DUAL_ARM_LIVE_REJECTED reason={error} "
                    "action=command_not_sent "
                    "rearm=release_both_grips_and_triggers "
                    "physical_robot_command_sent=False",
                    file=errors,
                    flush=True,
                )
                continue

            if state_unavailable_latched:
                print(
                    "PICO_G2_DUAL_ARM_LIVE_STATE_RECOVERED "
                    "held_arm_control_continues=True",
                    file=output,
                    flush=True,
                )
                state_unavailable_latched = False

            if result.status != "stale":
                record_fresh_recovery()

            if collection is not None:
                collection.process_result(result)

            if result.telemetry_refreshed:
                telemetry_refreshes += 1
            if len(result.gripper_sides) == 2:
                dual_gripper_sends += 1
            elif len(result.gripper_sides) == 1:
                single_gripper_sends += 1

            max_timing_ms["state_read"] = max(
                max_timing_ms["state_read"], result.timings.state_read_ms
            )
            max_timing_ms["mapping"] = max(
                max_timing_ms["mapping"], result.timings.mapping_ms
            )
            max_timing_ms["arm"] = max(
                max_timing_ms["arm"], result.timings.arm_ms
            )
            max_timing_ms["gripper"] = max(
                max_timing_ms["gripper"], result.timings.gripper_ms
            )
            max_timing_ms["total"] = max(
                max_timing_ms["total"], result.timings.total_ms
            )

            for lane_rejection in result.arm_rejections:
                arm_lane_rejected += 1
                event_key = f"arm:{lane_rejection.side}"
                event_now_ns = monotonic_ns()
                previous_event_ns = last_event_log_ns.get(event_key)
                if (
                    previous_event_ns is None
                    or event_now_ns - previous_event_ns >= 1_000_000_000
                ):
                    last_event_log_ns[event_key] = event_now_ns
                    print(
                        "PICO_G2_ARM_LANE_REJECTED "
                        f"side={lane_rejection.side} reason={lane_rejection.reason} "
                        "action=that_arm_command_not_sent "
                        "other_arm_and_grippers_continue=True "
                        "physical_robot_command_sent=False",
                        file=errors,
                        flush=True,
                    )
            for clamp in result.workspace_clamps:
                workspace_clamps += 1
                event_key = f"workspace:{clamp.side}"
                event_now_ns = monotonic_ns()
                previous_event_ns = last_event_log_ns.get(event_key)
                if (
                    previous_event_ns is None
                    or event_now_ns - previous_event_ns >= 1_000_000_000
                ):
                    last_event_log_ns[event_key] = event_now_ns
                    print(
                        "PICO_G2_WORKSPACE_SATURATED "
                        f"side={clamp.side} axis={clamp.axis} "
                        f"requested={clamp.requested:.6f} "
                        f"clamped={clamp.clamped:.6f} "
                        f"bounds=[{clamp.lower:.6f},{clamp.upper:.6f}] "
                        "arm_stream_continues=True",
                        file=output,
                        flush=True,
                    )
            for lane_failure in result.gripper_failures:
                gripper_lane_rejected += 1
                rearm = (
                    f"release_{lane_failure.side}_trigger"
                    if lane_failure.physical_outcome == "unknown"
                    else "automatic_next_frame"
                )
                event_key = (
                    f"gripper:{lane_failure.side}:"
                    f"{lane_failure.physical_outcome}"
                )
                event_now_ns = monotonic_ns()
                previous_event_ns = last_event_log_ns.get(event_key)
                if (
                    previous_event_ns is None
                    or event_now_ns - previous_event_ns >= 1_000_000_000
                ):
                    last_event_log_ns[event_key] = event_now_ns
                    print(
                        "PICO_G2_GRIPPER_LANE_REJECTED "
                        f"side={lane_failure.side} reason={lane_failure.reason} "
                        f"physical_outcome={lane_failure.physical_outcome} "
                        "arm_control_continues=True "
                        f"rearm={rearm}",
                        file=errors,
                        flush=True,
                    )

            if result.status == "sent":
                sent += 1
                sent_log_now_ns = monotonic_ns()
                if (
                    last_sent_log_ns is None
                    or sent_log_now_ns - last_sent_log_ns >= 1_000_000_000
                ):
                    last_sent_log_ns = sent_log_now_ns
                    print(
                        _format_sent(result, command_count=sent, adapter=adapter),
                        file=output,
                        flush=True,
                    )
                else:
                    sent_logs_suppressed += 1
            elif result.status == "stale":
                record_stale("xr_frame_marked_stale")
            else:
                idle += 1
    except Exception as error:
        exit_reason = f"fault: {error}"
        print(f"PICO_G2_DUAL_ARM_LIVE_ERROR: {error}", file=errors, flush=True)
        exit_code = 2
    finally:
        if stop_requested() and exit_code == 0:
            exit_reason = "operator_stop"
        if head_lane is not None:
            try:
                head_lane.close()
            except Exception as error:
                exit_code = 2
                exit_reason = f"head cleanup: {error}"
        if pipeline is not None:
            try:
                pipeline.reset()
            except Exception as error:
                print(
                    f"PICO_G2_DUAL_ARM_LIVE_STOP_ERROR: {error}",
                    file=errors,
                    flush=True,
                )
                exit_code = 2
        if adapter is not None:
            adapter.stop()
        if collection is not None:
            collection.close()
        if initialized:
            try:
                release_result = gdk.gdk_release()
            except Exception as error:
                print(
                    f"PICO_G2_DUAL_ARM_LIVE_RELEASE_ERROR: {error}",
                    file=errors,
                    flush=True,
                )
                exit_code = 2
            else:
                if release_result != success:
                    print(
                        f"PICO_G2_DUAL_ARM_LIVE_RELEASE_ERROR: {release_result!r}",
                        file=errors,
                        flush=True,
                    )
                    exit_code = 2
        if journal is not None:
            try:
                audit("combined_summary", reason=exit_reason, exit_code=exit_code,
                      head_commands=head_lane.sent if head_lane else 0,
                      arm_gripper_cycles=sent, dual_gripper_sends=dual_gripper_sends,
                      max_sdk_call_ms=guarded_robot.max_call_ms if guarded_robot else 0)
            except Exception as error:
                print(f"PICO_G2_COMBINED_JOURNAL_ERROR: {error}", file=errors, flush=True)
                exit_code = 2
            finally:
                try:
                    journal.close()
                except Exception as error:
                    print(f"PICO_G2_COMBINED_JOURNAL_ERROR: {error}", file=errors, flush=True)
                    exit_code = 2

    print(
        "PICO_G2_DUAL_ARM_LIVE_SUMMARY "
        f"messages_received={reader_stats['received']} "
        f"messages_overwritten={mailbox.overwritten_count} "
        f"reader_rejected={reader_stats['rejected']} frames={frames} sent={sent} "
        f"idle={idle} stale={stale} rejected={rejected} "
        f"superseded={superseded} state_unavailable={state_unavailable} "
        f"telemetry_refreshes={telemetry_refreshes} "
        f"arm_lane_rejected={arm_lane_rejected} "
        f"workspace_clamps={workspace_clamps} "
        f"tracking_recoveries={tracking_recoveries} "
        f"hard_tracking_faults={hard_tracking_faults} "
        f"gripper_lane_rejected={gripper_lane_rejected} "
        f"single_gripper_sends={single_gripper_sends} "
        f"dual_gripper_sends={dual_gripper_sends} "
        f"sent_logs_suppressed={sent_logs_suppressed} "
        f"stale_episodes={stale_tracker.episodes} "
        f"max_stale_run_frames={stale_tracker.max_run_frames} "
        f"stale_logs_suppressed={stale_tracker.logs_suppressed} "
        f"max_state_read_ms={max_timing_ms['state_read']:.3f} "
        f"max_mapping_ms={max_timing_ms['mapping']:.3f} "
        f"max_arm_ms={max_timing_ms['arm']:.3f} "
        f"max_gripper_ms={max_timing_ms['gripper']:.3f} "
        f"max_cycle_ms={max_timing_ms['total']:.3f} "
        "servo_target_expires_on_stop=True",
        file=output,
        flush=True,
    )
    if exit_code:
        return exit_code
    return 0 if sent > 0 or software_stop_requested or (head_lane and head_lane.sent > 0) else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    roles = parser.add_subparsers(dest="role", required=True)
    emitter = roles.add_parser("emit")
    emitter.add_argument("--duration", type=_positive_float, default=30.0)
    emitter.add_argument("--hz", type=_positive_float, default=100.0)
    emitter.add_argument("--frame-fd", "--intent-fd", dest="frame_fd", type=_file_descriptor)
    worker = roles.add_parser("worker")
    worker.add_argument("--settle-seconds", type=_nonnegative_float, default=2.0)
    worker.add_argument("--control-hz", type=_positive_float, default=100.0)
    worker.add_argument("--head-follow", action="store_true")
    worker.add_argument("--head-log-path", type=Path)
    worker.add_argument("--retarget-scale", type=_positive_float, default=1.0)
    worker.add_argument("--collection-socket")
    worker.add_argument(
        "--max-joint-velocity-rad-s",
        type=_positive_float,
        default=MAX_DUAL_ARM_VELOCITY_RAD_S,
    )
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
            print(f"PICO_G2_DUAL_ARM_LIVE_ERROR: {error}", file=sys.stderr)
            return 2
        return run_g2_live_worker(
            input_stream=sys.stdin,
            output=sys.stdout,
            errors=sys.stderr,
            gdk=gdk,
            settle_seconds=args.settle_seconds,
            control_hz=args.control_hz,
            max_joint_velocity_rad_s=args.max_joint_velocity_rad_s,
            stop_requested=lambda: stopped,
            head_follow=args.head_follow,
            head_log_path=args.head_log_path,
            retarget_scale=args.retarget_scale,
            collection_socket=args.collection_socket,
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
