"""Offline-testable arm sender used by the vendor-fast ROS binding.

Must run on the robot computer. All timestamps use THAT computer's monotonic
clock. This is not a network protocol: a PC must not restamp old goals on arrival.
No ROS imports or connections occur without explicit binding permissions.  The
base state machine requests logical modes 0/2; the live binding implements those
requests with the audited vendor baseline/quick2 lifecycle.
"""

import math
from numbers import Real
import threading
import time

from teleop.contracts.command import JointCommand
from teleop.contracts.robot_state import RobotState
from teleop.robots.kuavo5_arm_joint_encoder import encode_kuavo_arm_joint_command
from teleop.robots.kuavo5_native_ik import ARM_NAMES
from teleop.robots.kuavo5_live_guard import RuntimeMotionPaused


FEEDBACK_LIMIT_ROUNDOFF_RAD = 1e-4


class TransportRefused(ValueError):
    """Target/mode request was not accepted. This does not assert physical stop."""


class KuavoArmRosTransport:
    """Lower-layer sender for already bounded targets, not an IK/slew solver.

    The worker calls mode services serially OUTSIDE the control lock. A stop
    queued during a delayed external-mode request is processed after that call;
    no goal is released meanwhile. A permanently hung controller cannot be
    stopped by software: faults explicitly require the hardware emergency stop.

    publish MUST enqueue nonblocking (ROS Publisher queue_size=1), never wait
    for physical completion. pump and watchdog serialize this short enqueue.
    A process freeze/kill or kernel stall is outside this software watchdog.
    """

    def __init__(self, schema, *, publish, change_mode, motion_enabled=False,
                 clock=time.monotonic_ns, mode_timeout_s=.5, control_period_s=.02,
                 max_offset_rad=math.radians(10), max_velocity_rad_s=.6):
        if schema.names != ARM_NAMES or not callable(publish) or not callable(change_mode):
            raise ValueError('audited arm schema and explicit callbacks required')
        if type(motion_enabled) is not bool or not callable(clock):
            raise ValueError('explicit boolean permission and local monotonic clock required')
        if isinstance(mode_timeout_s, bool) or not isinstance(mode_timeout_s, Real) \
                or not math.isfinite(mode_timeout_s) or not 0 < mode_timeout_s <= 2:
            raise ValueError('mode service timeout must be in (0, 2] seconds')
        if isinstance(control_period_s, bool) or not isinstance(control_period_s, Real) \
                or not math.isfinite(control_period_s) or not 0 < control_period_s <= .02:
            raise ValueError('control period must be in (0, 20ms]')
        if max_offset_rad is not None and (
                isinstance(max_offset_rad, bool) or not isinstance(max_offset_rad, Real)
                or not math.isfinite(max_offset_rad) or max_offset_rad <= 0):
            raise ValueError('max offset must be None or finite and positive')
        if isinstance(max_velocity_rad_s, bool) or not isinstance(max_velocity_rad_s, Real) \
                or not math.isfinite(max_velocity_rad_s) or max_velocity_rad_s <= 0:
            raise ValueError('max velocity must be finite and positive')
        self.schema = schema
        self._publish, self._change_mode = publish, change_mode
        self._enabled, self._clock = motion_enabled, clock
        self._mode_timeout = float(mode_timeout_s)
        self._control_period = float(control_period_s)
        self._max_offset = (None if max_offset_rad is None else float(max_offset_rad))
        self._max_velocity = float(max_velocity_rad_s)
        self._condition = threading.Condition(threading.RLock())
        self._closed = False
        self._fault = None
        self._reason = 'not armed'
        self._release_required = True
        self._mode = 'unknown'
        self._desired_mode = 0
        self._pending_mode = None
        self._inflight_mode = None
        self._mode_started = None
        self._mode_touched = False
        self._hold_ack = False
        self._hold_ack_ns = None
        self._feedback = None
        self._raw_feedback = None
        self._publication_evidence = None
        self._pending_publication_check = None
        self._anchor = None
        self._latest = None
        self._sequence = -1
        self._last_sent = None
        self._last_sent_ns = None
        self._reference_sent_ns = None
        self._binding_baseline = None
        self._binding_baseline_ns = None
        self._last_clock_ns = -1
        self._lease_deadline = None
        self._recovery_deadline = None
        self._input_sequence = -1
        self._attempts = self._published = 0
        self._mode_thread = threading.Thread(target=self._mode_loop, name='kuavo-arm-mode', daemon=True)
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name='kuavo-arm-watchdog', daemon=True)
        self._mode_thread.start()
        self._watchdog_thread.start()

    def _now(self):
        now = self._clock()
        if type(now) is not int or now < 0 or now < self._last_clock_ns:
            raise TransportRefused('invalid or backwards robot-local clock')
        self._last_clock_ns = now
        return now

    def _validate_feedback_state(self, state, now, *, enforce_envelope):
        if state is None or not 0 <= now-state.timestamp_ns <= 250_000_000:
            raise TransportRefused('feedback is missing, stale or future')
        positions = dict(state.positions)
        for joint in self.schema.joints:
            value = positions.get(joint.name)
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
                raise TransportRefused('invalid feedback: '+joint.name+
                    ' non-finite or non-numeric value; type='+type(value).__name__[:64])
            bounded = min(joint.position_max, max(joint.position_min, value))
            if abs(bounded-value) > FEEDBACK_LIMIT_ROUNDOFF_RAD:
                raise TransportRefused(
                    f'invalid feedback: {joint.name} value_rad={value!r} '
                    f'lower_rad={joint.position_min!r} upper_rad={joint.position_max!r} '
                    f'feedback_timestamp_ns={state.timestamp_ns}')
            positions[joint.name] = bounded
            if enforce_envelope and self._max_offset is not None and self._anchor is not None \
                    and abs(bounded-self._anchor[joint.name]) > self._max_offset+1e-9:
                raise TransportRefused('feedback outside fixed session envelope')
        return state if all(positions[name] == state.positions[name] for name in ARM_NAMES) \
            else RobotState(positions, state.timestamp_ns)

    def _check_feedback(self, now):
        self._feedback = self._validate_feedback_state(
            self._feedback, now, enforce_envelope=True)

    def update_feedback(self, state):
        with self._condition:
            try:
                if not isinstance(state, RobotState):
                    raise TransportRefused('RobotState required')
                if self._feedback is not None and state.timestamp_ns <= self._feedback.timestamp_ns:
                    raise TransportRefused('feedback must be a new source observation')
                self._feedback = state
                self._raw_feedback = state
                now = self._now()
                try:
                    self._check_feedback(now)
                except (ValueError, TypeError, KeyError):
                    # Retain a fresh, hard-limit-valid observation after only a
                    # session-envelope violation.  The live binding needs that
                    # exact physical pose to request zero-velocity quiescence.
                    # It is never allowed to replace or widen the session anchor.
                    self._feedback = self._validate_feedback_state(
                        state, now, enforce_envelope=False)
                    raise
                if self._anchor is None:
                    self._anchor = {n: self._feedback.positions[n] for n in ARM_NAMES}
                return self._feedback
            except (ValueError, TypeError) as exc:
                try:
                    self._validate_feedback_state(self._feedback, self._last_clock_ns,
                                                  enforce_envelope=False)
                except (ValueError, TypeError, KeyError):
                    self._feedback = None
                self._stop_locked(str(exc))
                raise TransportRefused(str(exc)) from exc

    def _reference_time(self, reference_time_ns, now):
        stamp = now if reference_time_ns is None else reference_time_ns
        if type(stamp) is not int or not 0 <= stamp <= now \
                or (self._reference_sent_ns is not None and stamp < self._reference_sent_ns):
            raise TransportRefused('invalid reference publication timestamp')
        return stamp

    def _synchronize_output_baseline(self, positions, *, reference_time_ns=None):
        """Record a binding-owned seed/quiesce publication for later slew checks."""
        with self._condition:
            if not isinstance(positions, dict) or tuple(positions) != ARM_NAMES:
                raise TransportRefused('exact arm baseline required')
            baseline = {}
            for joint in self.schema.joints:
                value = positions[joint.name]
                if isinstance(value, bool) or not isinstance(value, Real) \
                        or not math.isfinite(value) \
                        or not joint.position_min <= value <= joint.position_max:
                    raise TransportRefused('invalid published baseline: '+joint.name)
                if self._max_offset is not None and self._anchor is not None \
                        and abs(value-self._anchor[joint.name]) > self._max_offset+1e-9:
                    raise TransportRefused('published baseline outside fixed session envelope')
                baseline[joint.name] = value
            now = self._now()
            reference_stamp = self._reference_time(reference_time_ns, now)
            self._last_sent = baseline
            self._last_sent_ns = now
            self._reference_sent_ns = reference_stamp
            self._binding_baseline = dict(baseline)
            self._binding_baseline_ns = self._last_sent_ns

    def _synchronize_cleanup_baseline(self, state, *, reference_time_ns=None):
        """Record an exit-only zero-velocity pose without changing the anchor."""
        with self._condition:
            if not isinstance(state, RobotState) or tuple(state.positions) != ARM_NAMES:
                raise TransportRefused('fresh exact arm feedback required for cleanup')
            now = self._now()
            state = self._validate_feedback_state(state, now, enforce_envelope=False)
            reference_stamp = self._reference_time(reference_time_ns, now)
            self._last_sent = {name: state.positions[name] for name in ARM_NAMES}
            self._last_sent_ns = now
            self._reference_sent_ns = reference_stamp
            self._binding_baseline = dict(self._last_sent)
            self._binding_baseline_ns = now

    def binding_output_baseline(self):
        """Return only the latest binding-owned seed/quiesce publication."""
        with self._condition:
            if self._binding_baseline is None:
                return None
            return dict(self._binding_baseline), self._binding_baseline_ns

    def output_baseline(self):
        """Return the exact latest enqueued/seeded positions and local time."""
        with self._condition:
            if self._last_sent is None or self._last_sent_ns is None:
                return None
            return dict(self._last_sent), self._last_sent_ns

    def publication_evidence(self):
        """Latest successful enqueue, not a physical execution acknowledgement."""
        with self._condition:
            return (None if self._publication_evidence is None
                    else dict(self._publication_evidence))

    def _stop_locked(self, reason, *, fault=False):
        self._latest = None
        self._lease_deadline = None
        self._recovery_deadline = None
        self._release_required = True
        self._desired_mode = 0
        self._reason = reason
        if fault:
            self._fault = reason
        # Cancel an enable request which the worker has not taken. In particular,
        # already-holding rearm -> release must never send mode2 after release.
        if self._pending_mode == 2:
            self._pending_mode = None
        # A late mode2 result may change the hardware even after local close.
        if self._mode_touched and (self._mode != 'hold' or self._inflight_mode == 2):
            if self._inflight_mode != 0:
                self._pending_mode = 0
            self._hold_ack = False
        elif self._mode == 'hold' and self._inflight_mode is None:
            self._hold_ack = True
        self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._closed:
                return
            self._stop_locked('operator released / pause requested')
            if self._fault is None:
                self._release_required = False

    def arm(self):
        with self._condition:
            if not self._enabled or self._closed or self._fault:
                raise TransportRefused('motion disabled, closed or faulted')
            if self._release_required:
                raise TransportRefused('fresh operator release required before arming')
            if self._inflight_mode is not None or self._pending_mode is not None \
                    or self._desired_mode == 2:
                raise TransportRefused('previous mode transition has not finished')
            now = self._now()
            self._check_feedback(now)
            self._desired_mode = 2
            self._pending_mode = 2
            self._hold_ack = False
            self._release_required = True
            self._lease_deadline = now+100_000_000
            self._reason = 'external arm mode requested'
            self._condition.notify_all()

    def refresh_input(self, *, sequence_id, input_deadline_ns):
        """Renew only the input lease while serial IK/mode work is in flight.

        Does not renew candidate lifetime, arm a stopped transport, or publish.
        The robot-side input adapter must first validate source age and identity.
        Ordinary expiry pauses and returns False, just like the watchdog; it
        must not tear down the entire arm/gripper session depending on which
        thread first notices the elapsed deadline. Malformed renewals still fail.
        """
        with self._condition:
            now = self._now()
            if self._closed or self._fault or self._desired_mode != 2:
                return False
            if self._recovery_deadline is not None and now >= self._recovery_deadline:
                # An expected recovery timeout can arrive before the watchdog
                # or the next session tick. Stop without treating fresh input
                # as a malformed renewal; the session still requires release.
                self._stop_locked('IK recovery deadline expired')
                return False
            if type(sequence_id) is not int or sequence_id <= self._input_sequence \
                    or type(input_deadline_ns) is not int \
                    or input_deadline_ns <= 0 or input_deadline_ns > now+100_000_000:
                self._stop_locked('invalid input renewal')
                raise TransportRefused('invalid input renewal')
            if now >= input_deadline_ns:
                self._stop_locked('new input expired before renewal; release Grip before retry')
                return False
            if self._lease_deadline is None or now >= self._lease_deadline:
                self._stop_locked('previous input/candidate lease expired before renewal; release Grip before retry')
                return False
            self._input_sequence = sequence_id
            self._lease_deadline = min(input_deadline_ns, self._recovery_deadline) \
                if self._recovery_deadline is not None else input_deadline_ns
            return True

    def constrain_recovery_deadline(self, deadline_ns):
        """Independent watchdog bound; input renewal cannot prolong IK waiting.

        Clearing the bound never re-arms a stopped transport. An expired bound
        is honored even if a late solver result attempts to clear it.
        """
        with self._condition:
            now = self._now()
            if self._recovery_deadline is not None and now >= self._recovery_deadline:
                self._stop_locked('IK recovery deadline expired')
            if deadline_ns is None:
                self._recovery_deadline = None
                return
            if type(deadline_ns) is not int or not now < deadline_ns <= now+100_000_000:
                self._stop_locked('invalid or expired IK recovery deadline')
                return
            if self._closed or self._fault or self._desired_mode != 2 or self._mode != 'external':
                return
            self._recovery_deadline = (deadline_ns if self._recovery_deadline is None else
                                       min(deadline_ns, self._recovery_deadline))
            if self._lease_deadline is not None:
                self._lease_deadline = min(self._lease_deadline, self._recovery_deadline)

    def _validate_target(self, command, deadline, now):
        if not isinstance(command, JointCommand) or tuple(command.targets) != ARM_NAMES:
            raise TransportRefused('exact left7/right7 JointCommand required')
        if type(deadline) is not int or not now < deadline <= now+100_000_000:
            raise TransportRefused('input lease invalid, expired or longer than 100ms')
        if not 0 <= now-command.created_time_ns < command.ttl_ms*1_000_000 \
                or command.ttl_ms > 200:
            raise TransportRefused('candidate invalid or expired (maximum 200ms)')
        self._check_feedback(now)
        previous = self._last_sent or dict(self._feedback.positions)
        elapsed = (self._control_period if self._last_sent_ns is None else
                   min(self._control_period, (now-self._last_sent_ns)/1e9))
        for joint in self.schema.joints:
            value = command.targets[joint.name]
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) \
                    or not joint.position_min <= value <= joint.position_max:
                raise TransportRefused('target exceeds joint limits or is invalid')
            if self._max_offset is not None \
                    and abs(value-self._anchor[joint.name]) > self._max_offset+1e-9:
                raise TransportRefused('target exceeds fixed session envelope')
            velocity_limit = min(self._max_velocity,
                                 joint.max_velocity or self._max_velocity)
            if abs(value-previous[joint.name]) > velocity_limit*elapsed+1e-9:
                raise TransportRefused('target exceeds actual-time speed bound')
            if abs(value-self._feedback.positions[joint.name]) > .2:
                raise TransportRefused('target/feedback tracking error')

    def submit(self, command, *, input_deadline_ns):
        with self._condition:
            if self._closed or self._fault or self._desired_mode != 2 or self._mode != 'external':
                raise TransportRefused('external mode has not been acknowledged or is stopped')
            try:
                now = self._now()
                if self._lease_deadline is None or now >= self._lease_deadline:
                    raise TransportRefused('previous input lease expired')
                self._validate_target(command, input_deadline_ns, now)
                if command.sequence_id <= self._sequence:
                    raise TransportRefused('reordered or replayed target')
                self._sequence = command.sequence_id
                self._lease_deadline = min(input_deadline_ns, command.created_time_ns+command.ttl_ms*1_000_000)
                self._latest = (command, self._lease_deadline)
            except (ValueError, TypeError, KeyError) as exc:
                self._stop_locked(str(exc))
                raise TransportRefused(str(exc)) from exc

    def _check_watchdog_locked(self):
        now = self._now()
        if self._mode_started is not None and time.monotonic()-self._mode_started > self._mode_timeout:
            self._stop_locked('mode service timeout; hardware emergency stop may be required', fault=True)
        if self._desired_mode == 2:
            self._check_feedback(now)
            if self._recovery_deadline is not None and now >= self._recovery_deadline:
                self._stop_locked('IK recovery deadline expired')
            if self._lease_deadline is None or now >= self._lease_deadline:
                self._stop_locked('input/candidate lease expired')
        return now

    def _check_publication_boundary(self, command, deadline):
        # Called by the ROS binding after message construction, with pump's
        # lock held. Checking outside the binding cannot cover its work time.
        now = self._check_watchdog_locked()
        if now >= deadline or self._desired_mode != 2:
            raise RuntimeMotionPaused('input/candidate lease expired before ROS enqueue')
        if self._closed or self._fault or self._mode != 'external':
            raise TransportRefused('publication no longer authorized')
        self._validate_target(command, deadline, now)

    def pump(self):
        """Publish at most one latest target; watchdog does not depend on this call."""
        with self._condition:
            if self._closed or self._fault:
                return False
            try:
                now = self._check_watchdog_locked()
                if self._desired_mode != 2 or self._mode != 'external' or self._latest is None:
                    return False
                command, deadline = self._latest
                self._latest = None
                self._validate_target(command, deadline, now)
                encoded = encode_kuavo_arm_joint_command(command)
                # Encoding must not consume a lease and then send a stale goal.
                now = self._check_watchdog_locked()
                self._validate_target(command, deadline, now)
                previous = self._last_sent or dict(self._feedback.positions)
                # Position validation keeps its conservative nominal-period
                # cap. Reference velocity describes the actual position step
                # over the measured interval between publication starts.
                reference_now = self._now()
                elapsed = (self._control_period if self._last_sent_ns is None else
                           (reference_now-self._reference_sent_ns)/1e9)
                if elapsed <= 0:
                    raise TransportRefused('no time elapsed between arm publications')
                velocities = []
                for name in encoded.names:
                    limit = min(self._max_velocity,
                                self.schema.get(name).max_velocity or self._max_velocity)
                    velocity = (command.targets[name]-previous[name])/elapsed
                    velocities.append(math.degrees(max(-limit, min(limit, velocity))))
                self._attempts += 1
                self._pending_publication_check = lambda: self._check_publication_boundary(command, deadline)
                try:
                    self._publish(encoded.names, encoded.positions_deg, tuple(velocities))
                finally:
                    self._pending_publication_check = None
                self._published += 1
                enqueue_completed = self._now()
                self._publication_evidence = dict(
                    command_sequence_id=command.sequence_id,
                    reference_started_ns=reference_now,
                    reference_interval_s=elapsed,
                    enqueue_completed_ns=enqueue_completed,
                    positions_deg=tuple(encoded.positions_deg),
                    velocities_deg_s=tuple(velocities),
                    feedback_timestamp_ns=self._feedback.timestamp_ns,
                    raw_feedback_rad=tuple(self._raw_feedback.positions[n] for n in ARM_NAMES),
                    normalized_feedback_rad=tuple(self._feedback.positions[n] for n in ARM_NAMES))
                self._last_sent = dict(command.targets)
                # Position slew uses a conservative AFTER-enqueue baseline;
                # callback work must not grant extra time to the next target.
                self._last_sent_ns = enqueue_completed
                self._reference_sent_ns = reference_now
                self._reason = 'target enqueued; physical execution unverified'
                return True
            except RuntimeMotionPaused as exc:
                # The audited binding raises this ONLY before publication.
                # Revoke this segment while keeping the observation link alive.
                self._stop_locked(str(exc))
                raise
            except Exception as exc:
                self._stop_locked('publish/check failed: '+str(exc), fault=True)
                return False

    def _mode_loop(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending_mode is not None or self._closed)
                if self._pending_mode is None:
                    return
                requested = self._pending_mode
                self._pending_mode = None
                self._inflight_mode = requested
                self._mode_touched = True
                self._mode_started = time.monotonic()
            try:
                response = self._change_mode(requested)
                if getattr(response, 'result', None) is not True \
                        or type(getattr(response, 'mode', None)) is not int or response.mode != requested:
                    raise TransportRefused('mode response rejected or does not match request')
                error = None
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
                motion_paused = isinstance(exc, RuntimeMotionPaused) and requested == 2
            with self._condition:
                duration = time.monotonic()-self._mode_started
                self._mode_started = None
                self._inflight_mode = None
                if duration > self._mode_timeout:
                    self._stop_locked('mode service timeout; physical stop unverified', fault=True)
                if error is not None:
                    self._mode = 'unknown'
                    self._hold_ack = False
                    self._stop_locked('mode request failed: '+error, fault=not motion_paused)
                    # A failed hold must not create an unbounded retry loop.
                    if requested == 0:
                        self._pending_mode = None
                    continue
                self._mode = 'external' if requested == 2 else 'hold'
                self._hold_ack = requested == 0
                if requested == 0:
                    try:
                        self._hold_ack_ns = self._now()
                    except Exception as exc:
                        self._hold_ack = False
                        self._stop_locked('invalid hold reply clock: '+str(exc), fault=True)
                if requested == 2 and (self._desired_mode != 2 or self._closed or self._fault):
                    self._pending_mode = 0
                elif requested == 2:
                    self._reason = 'external arm mode acknowledged'
                self._condition.notify_all()

    def _watchdog_loop(self):
        while True:
            with self._condition:
                if self._closed and self._inflight_mode is None and self._pending_mode is None:
                    return
                try:
                    self._check_watchdog_locked()
                except Exception as exc:
                    self._stop_locked('watchdog: '+str(exc), fault=True)
                self._condition.wait(timeout=.01)

    def status(self):
        with self._condition:
            return dict(mode=self._mode, fault=self._fault, reason=self._reason,
                        requires_release=self._release_required, closed=self._closed,
                        # The legacy flag also stays true while an authorized
                        # session runs. It gates the NEXT arm() request; it is
                        # not evidence that the current Grip must be released.
                        rearm_release_required=self._release_required,
                        release_requirement_scope='before_next_arm_request',
                        hold_acknowledged=self._hold_ack, physical_stop_verified=False,
                        hold_acknowledged_time_ns=self._hold_ack_ns,
                        publish_attempts=self._attempts, publish_enqueued=self._published,
                        mode_request_inflight=self._inflight_mode, mode_request_pending=self._pending_mode,
                        desired_mode=self._desired_mode)

    def close(self):
        with self._condition:
            if not self._closed:
                self._stop_locked('transport closed')
                self._closed = True
                self._condition.notify_all()
        # Bounded shutdown. A blocked service remains queued for hold in its daemon
        # thread; process termination would lose that fallback. Report it explicitly.
        self._mode_thread.join(timeout=self._mode_timeout+.05)
        self._watchdog_thread.join(timeout=.05)
        return self.status()


def bind_ros(schema, *, motion_enabled=False, configure_vendor_profile=False,
             max_velocity_rad_s=.6):
    """User-started binding only. Default fails before ROS imports or handles.

    The execution entry performs model/graph/feedback preflight before invoking
    this function. Explicit profile permission covers shared controller settings;
    construction itself neither configures modes nor sends a target.
    """
    if motion_enabled is not True or configure_vendor_profile is not True:
        raise TransportRefused('real ROS binding disabled: explicit motion AND vendor profile permission required')
    if schema.names != ARM_NAMES:
        raise TransportRefused('exact audited arm schema required')
    if isinstance(max_velocity_rad_s, bool) or not isinstance(max_velocity_rad_s, Real) \
            or not math.isfinite(max_velocity_rad_s) or max_velocity_rad_s <= 0:
        raise ValueError('max velocity must be finite and positive')
    from teleop.robots.kuavo5_live_ros_binding import RosArmBinding
    return RosArmBinding(schema, max_velocity_rad_s=max_velocity_rad_s)
