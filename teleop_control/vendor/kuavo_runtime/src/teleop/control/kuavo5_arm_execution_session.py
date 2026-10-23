"""Single-owner execution logic, NOT a robot driver or an independent watchdog.

The caller must invoke tick on the robot-side control clock independently of IK,
and supply nonblocking, audited publish/hold operations. A returned hold request
does not prove that physical motion has stopped. Do not wire this into the
readonly adapter.
"""

from copy import copy
from dataclasses import dataclass, replace
import math
from numbers import Real

from teleop.contracts.command import JointCommand
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointSchema
from teleop.control.joint_motion_gate import JointMotionGate, MotionGateError
from teleop.control.kuavo5_tracking_governor import KuavoTrackingGovernor
from teleop.robots.kuavo5_arm_joint_encoder import encode_kuavo_arm_joint_command
from teleop.robots.kuavo5_native_ik import ARM_NAMES
from teleop.robots.kuavo5_live_guard import RuntimeMotionPaused


@dataclass(frozen=True)
class GripSample:
    sequence_id: int
    created_time_ns: int
    active_sides: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, 'active_sides', tuple(self.active_sides))
        if type(self.sequence_id) is not int or self.sequence_id < 0:
            raise ValueError('invalid input sequence')
        if type(self.created_time_ns) is not int or self.created_time_ns < 0:
            raise ValueError('invalid input time')
        if len(set(self.active_sides)) != len(self.active_sides) \
                or not set(self.active_sides) <= {'left', 'right'}:
            raise ValueError('invalid active sides')


@dataclass(frozen=True)
class ReturnSample:
    """Fresh operator-input lease for an explicitly requested fixed return.

    This is a distinct authority, not a statement that either Grip is pressed.
    The owner must cancel it when a real Grip is pressed or input is lost.
    """

    sequence_id: int
    created_time_ns: int

    def __post_init__(self):
        if type(self.sequence_id) is not int or self.sequence_id < 0 \
                or type(self.created_time_ns) is not int or self.created_time_ns < 0:
            raise ValueError('invalid return input lease')

    @property
    def active_sides(self):
        return ('left', 'right')


@dataclass(frozen=True)
class ArmSolveTicket:
    """One in-process solve reservation; not a wire token or motion permission."""

    sample: GripSample
    epoch: int
    started_time_ns: int


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    reason: str
    max_step_rad: float = 0.
    hold_request_succeeded: bool | None = None


class ArmExecutionSession:
    """Serial state machine with explicit asynchronous-IK generation fencing.

    All time stamps must already be in the same trusted monotonic clock domain.
    A network adapter must preserve source age, NOT stamp delayed packets fresh.
    publish returns normally on acceptance, or raises; hold has the same contract.
    These callback semantics and scheduling are not established for physical ROS.
    """

    def __init__(self, schema, *, publish, hold, max_velocity_rad_s=.6,
                 max_acceleration_rad_s2=None,
                 max_offset_rad=math.radians(10), period_s=.02,
                 max_tracking_error_rad=.2, max_state_age_ms=250,
                 max_input_age_ms=100, recover_unreachable=False,
                 tracking_governor=None, reference_sync="all_joints"):
        if tuple(schema.names) != ARM_NAMES:
            raise ValueError('fixed left7/right7 schema required')
        if type(max_input_age_ms) is not int or max_input_age_ms <= 0:
            raise ValueError('input age must be a positive integer')
        if not callable(publish) or not callable(hold):
            raise ValueError('explicit transport callbacks required')
        if type(recover_unreachable) is not bool:
            raise ValueError('recover_unreachable must be boolean')
        if reference_sync not in ('all_joints', 'per_arm'):
            raise ValueError('reference_sync must be all_joints or per_arm')
        self._recover_unreachable = recover_unreachable
        self._recovery_deadline = None
        self._initial_numeric_wait = False
        self._resume_check = False
        self._options = dict(max_velocity_rad_s=max_velocity_rad_s,
                             max_acceleration_rad_s2=max_acceleration_rad_s2,
                             max_offset_rad=max_offset_rad, period_s=period_s,
                             max_tracking_error_rad=max_tracking_error_rad,
                             max_state_age_ms=max_state_age_ms,
                             synchronization_groups=((ARM_NAMES[:7], ARM_NAMES[7:])
                                 if reference_sync == "per_arm" else None))
        JointMotionGate(schema, **self._options)  # Validate all configuration now.
        if tracking_governor is not None:
            if not isinstance(tracking_governor, KuavoTrackingGovernor):
                raise ValueError('KuavoTrackingGovernor required')
            tracking_governor.validate_profile(
                velocity=max_velocity_rad_s,
                acceleration=max_acceleration_rad_s2,
                period=period_s,
                hard_tracking=max_tracking_error_rad)
        self.schema = schema
        self._publish, self._hold = publish, hold
        self._input_age_ns = max_input_age_ms * 1_000_000
        self._state_age_ns = max_state_age_ms * 1_000_000
        self._input = None
        self._last_input_seq = -1
        self._clock_ns = -1
        self._epoch = 0
        self._retarget_epoch = 0
        self._min_candidate_seq = 0
        self._min_candidate_time = 0
        self._last_candidate_seq = -1
        self._candidate = None
        self._candidate_sides = ()
        self._sides = ()
        self._requires_release = True
        self._moving = False
        self._status, self._reason = 'waiting_release', 'release both Grips before arming'
        self._hold_succeeded = None
        self._session_schema = None
        self._gate = None
        self._inactive = None
        self._last_published = None
        self._last_publish_ns = None
        self._solve_ticket = None
        self._last_solve_seq = -1
        self._tracking_governor = tracking_governor
        self._tracking_diagnostics = None
        self._return_profile = None

    @property
    def epoch(self):
        return self._epoch

    @property
    def retarget_epoch(self):
        """Generation for controller-to-robot anchors, excluding live side changes."""
        return self._retarget_epoch

    @property
    def requires_release(self):
        return self._requires_release

    @property
    def last_input_sequence_id(self):
        """Last accepted input, never advanced by a rejected observation."""
        return self._last_input_seq

    @property
    def last_published(self):
        return None if self._last_published is None else dict(self._last_published)

    @property
    def last_publish_time_ns(self):
        return self._last_publish_ns

    @property
    def candidate_deadline_ns(self):
        if self._recovery_deadline is not None:
            return self._recovery_deadline
        if self._candidate is None or self._requires_release or self._terminal():
            return None
        return self._candidate.created_time_ns + self._candidate.ttl_ms*1_000_000

    @property
    def recovery_deadline_ns(self):
        return self._recovery_deadline

    @property
    def tracking_diagnostics(self):
        return None if self._tracking_diagnostics is None else dict(self._tracking_diagnostics)

    def _record_tracking(self, result, *, phase):
        if result is None or self._tracking_governor is None:
            self._tracking_diagnostics = None
            return
        self._tracking_diagnostics = dict(
            enabled=True,
            phase=phase,
            soft_lead_rad=self._tracking_governor.lead_rad,
            tracking_limited=result.tracking_limited,
            goal_arm_scales=dict(result.arm_scales),
            limited_joints=list(result.limited_joints),
            max_lead_rad=result.max_lead_rad,
            stalled_joint=result.stalled_joint,
            stall_age_s=result.stall_age_s)

    def _reset_tracking(self, sides=('left', 'right')):
        if self._tracking_governor is not None:
            self._tracking_governor.reset(sides)
        self._tracking_diagnostics = None

    @property
    def awaiting_recoverable_target(self):
        """Fresh ordinary solve rejection before first output, not missing data.

        This never authorizes a mode request or publication. It only identifies
        a healthy numeric wait to the owner of an already requested mode.
        """
        return (self._initial_numeric_wait and not self._moving
                and self._candidate is None and bool(self._sides)
                and not self._requires_release and not self._terminal())

    def _terminal(self):
        return self._status in ('fault', 'closed')

    def _result(self, step=0.):
        return ExecutionResult(self._status, self._reason, step, self._hold_succeeded)

    def _pause(self, reason):
        if self._moving:
            self._moving = False
            try:
                self._hold(reason)
                self._hold_succeeded = True
            except Exception as exc:
                self._hold_succeeded = False
                self._status, self._reason = 'fault', f'{reason}; hold failed: {exc}'

    def _invalidate(self, reason):
        self._pause(reason)
        self._reset_tracking()
        self._recovery_deadline = None
        self._initial_numeric_wait = False
        self._resume_check = False
        self._epoch += 1
        self._retarget_epoch += 1
        self._candidate = None
        self._candidate_sides = ()
        self._gate = self._inactive = None
        self._requires_release = True
        if not self._terminal():
            self._status, self._reason = 'waiting_release', reason

    def _clock(self, now_ns):
        if type(now_ns) is not int or now_ns < 0 or now_ns < self._clock_ns:
            detail = ('type='+type(now_ns).__name__ if type(now_ns) is not int
                      else 'now='+str(now_ns)+' last='+str(self._clock_ns))
            self._invalidate('invalid or backwards control clock: '+detail)
            return False
        self._clock_ns = now_ns
        return True

    def observe_input(self, sample: GripSample, *, now_ns: int):
        """Consume a NEW source frame; call even while an IK request is in flight."""
        if self._return_profile is not None:
            raise ValueError('end return authority before observing physical Grip input')
        return self._observe_input(sample, now_ns=now_ns, sample_type=GripSample)

    def observe_return_input(self, sample: ReturnSample, *, now_ns: int):
        if self._return_profile is None:
            raise ValueError('explicit return authority has not been started')
        return self._observe_input(sample, now_ns=now_ns, sample_type=ReturnSample)

    def _observe_input(self, sample, *, now_ns, sample_type):
        if self._terminal() or not self._clock(now_ns):
            return self._epoch
        if not isinstance(sample, sample_type) or sample.sequence_id <= self._last_input_seq \
                or not 0 <= now_ns-sample.created_time_ns < self._input_age_ns \
                or (self._input is not None
                    and sample.created_time_ns < self._input.created_time_ns):
            self._invalidate('stale, future or reordered input')
            return self._epoch
        if self._input is not None and now_ns-self._input.created_time_ns >= self._input_age_ns:
            self._invalidate('input gap before new packet')
        self._input, self._last_input_seq = sample, sample.sequence_id
        sides = tuple(sorted(sample.active_sides))
        previous_sides = self._sides
        if sides != previous_sides and (not sides or not previous_sides):
            self._pause('Grip released or active side changed')
            self._reset_tracking()
            self._recovery_deadline = None
            self._initial_numeric_wait = False
            self._resume_check = False
            self._epoch += 1
            self._retarget_epoch += 1
            self._candidate = None
            self._candidate_sides = ()
            self._gate = self._inactive = None
            self._last_candidate_seq = -1
            self._min_candidate_seq = sample.sequence_id
            self._min_candidate_time = sample.created_time_ns
        elif sides != previous_sides:
            # Changing one hand while the other remains active is a clutch
            # transition inside the same physical arm-control session.  Fence
            # asynchronous results from the old side set, but preserve the
            # output planner and the continuously active arm.
            self._epoch += 1
            self._initial_numeric_wait = False
            self._reset_tracking(tuple(set(sides).symmetric_difference(previous_sides)))
            if self._inactive is not None and self._last_published is not None:
                for side, names in (('left', ARM_NAMES[:7]),
                                    ('right', ARM_NAMES[7:])):
                    if side in previous_sides and side not in sides:
                        self._inactive.update(
                            {name: self._last_published[name] for name in names})
        self._sides = sides
        if self._terminal():
            return self._epoch
        if not sides:
            self._requires_release = False
            self._status, self._reason = 'idle', 'both Grips released'
        elif not self._requires_release:
            self._status, self._reason = 'waiting_candidate', 'waiting for fresh IK'
            if self._recovery_deadline is not None:
                self._recovery_deadline = sample.created_time_ns + self._input_age_ns
        return self._epoch

    def invalidate_input(self, reason: str, *, now_ns: int):
        """Invalidate a source frame even when its receive timestamp is recent."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('input invalidation reason is required')
        if self._terminal() or not self._clock(now_ns):
            return self._epoch
        self._invalidate(reason.strip())
        return self._epoch

    def start_return(self, sample: ReturnSample, *, now_ns: int,
                     max_velocity_rad_s: float, max_acceleration_rad_s2: float):
        """Use the same sender with a new explicit authority and slower limits.

        The owner waits for the prior hold reply before calling this. Reusing
        the last released frame only transfers authority; it never renews age.
        """
        if self._terminal() or not self._clock(now_ns) or self._return_profile is not None:
            return False
        if not isinstance(sample, ReturnSample) or not isinstance(self._input, GripSample) \
                or self._input.active_sides or self._requires_release \
                or sample.sequence_id != self._input.sequence_id \
                or sample.created_time_ns != self._input.created_time_ns \
                or not 0 <= now_ns-sample.created_time_ns < self._input_age_ns:
            return False
        options = dict(self._options, max_velocity_rad_s=min(
            self._options['max_velocity_rad_s'], max_velocity_rad_s),
            max_acceleration_rad_s2=min(
                self._options['max_acceleration_rad_s2'] or max_acceleration_rad_s2,
                max_acceleration_rad_s2), synchronization_groups=None)
        JointMotionGate(self.schema, **options)
        governor = KuavoTrackingGovernor(lead_rad=.06)
        governor.validate_profile(velocity=options['max_velocity_rad_s'],
            acceleration=options['max_acceleration_rad_s2'],
            period=options['period_s'], hard_tracking=options['max_tracking_error_rad'])
        self._invalidate('explicit fixed joint return started')
        self._return_profile = (self._options, self._tracking_governor)
        self._options, self._tracking_governor = options, governor
        self._input = sample
        self._sides = sample.active_sides
        self._requires_release = False
        self._min_candidate_seq = sample.sequence_id
        self._min_candidate_time = sample.created_time_ns
        self._last_candidate_seq = -1
        self._status, self._reason = 'waiting_candidate', 'waiting for fixed return target'
        return True

    def end_return(self, reason, *, now_ns: int):
        """Restore ordinary planning, fence old work, and require real release."""
        if self._return_profile is not None:
            self._options, self._tracking_governor = self._return_profile
            self._return_profile = None
        return self.invalidate_input(reason, now_ns=now_ns)

    def fence_after_hold(self, *, now_ns: int):
        """Discard pre-hold computation/anchors without granting motion permission.

        The caller separately waits for the transport reply and newer XR/feedback.
        Preserve release requirements, the fixed envelope and publication clock.
        A cancelled worker keeps its reservation until its result is drained.
        """
        if self._terminal() or not self._clock(now_ns):
            return
        self._epoch += 1
        self._retarget_epoch += 1
        self._candidate = None
        self._candidate_sides = ()
        self._gate = self._inactive = None
        self._moving = False
        self._reset_tracking()

        self._recovery_deadline = None
        self._initial_numeric_wait = False
        self._resume_check = False

    def synchronize_output_baseline(self, positions, *, published_ns: int,
                                    now_ns: int):
        """Align planning with the binding's exact last enqueued seed/target.

        Vendor entry seeds before its mode services complete, so later feedback
        may differ slightly from the actual output slew origin.  This operation
        records that origin without granting publication permission or renewing
        input/candidate lifetime.
        """
        if self._terminal() or not self._clock(now_ns):
            return False
        if self._session_schema is None:
            raise ValueError('session envelope required before output synchronization')
        if type(published_ns) is not int or published_ns < 0 or published_ns > now_ns:
            raise ValueError('invalid output baseline timestamp')
        if not isinstance(positions, dict) or tuple(positions) != ARM_NAMES:
            raise ValueError('exact arm output baseline required')
        baseline = dict(positions)
        gate = JointMotionGate(self._session_schema, **self._options)
        seed = JointCommand(baseline, 'binding_output_baseline', 0,
                            published_ns, 200)
        gate.advance(seed, RobotState(baseline, published_ns),
                     now_ns=published_ns)
        self._gate = gate
        self._inactive = {
            name: baseline[name]
            for side, names in (('left', ARM_NAMES[:7]),
                                ('right', ARM_NAMES[7:]))
            if side not in self._sides
            for name in names
        }
        self._last_published = baseline
        self._last_publish_ns = published_ns
        return True

    def synchronize_reference_velocity(self, positions, velocities_rad_s, *, published_ns: int):
        """Use exact successful sender evidence as the next planning velocity.

        This records a publication already committed by this session. It never
        publishes, changes Grip state, refreshes input/candidate ages, or grants
        motion permission. Sender secants can be slower than the nominal-time
        planner after a delayed tick; retaining the old internal velocity would
        create an artificial acceleration back to that obsolete speed.
        """
        if self._terminal() or self._gate is None or self._last_published is None:
            return False
        if self._options['max_acceleration_rad_s2'] is None:
            return False
        if type(published_ns) is not int or published_ns != self._last_publish_ns:
            raise ValueError('reference velocity belongs to another publication')
        if not isinstance(positions, dict) or tuple(positions) != ARM_NAMES \
                or not isinstance(velocities_rad_s, dict) or tuple(velocities_rad_s) != ARM_NAMES:
            raise ValueError('exact arm position/velocity evidence required')
        normalized = {}
        for joint in self.schema.joints:
            position, velocity = positions[joint.name], velocities_rad_s[joint.name]
            if any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value)
                   for value in (position, velocity)):
                raise ValueError('invalid reference velocity evidence: '+joint.name)
            # A degree/radian wire round trip can change the final binary bits.
            if abs(position-self._last_published[joint.name]) > 1e-12:
                raise ValueError('reference position belongs to another publication')
            limit = min(self._options['max_velocity_rad_s'],
                        joint.max_velocity or self._options['max_velocity_rad_s'])
            if abs(velocity) > limit+1e-12:
                raise ValueError('reference velocity exceeds configured speed')
            normalized[joint.name] = max(-limit, min(limit, velocity))
        trial = copy(self._gate)
        trial._previous_velocity = normalized
        self._gate = trial
        return True


    def offer(self, candidate: JointCommand, *, epoch: int):
        """Accept a result only for the same active Grip generation; never renew TTL."""
        if self._terminal() or self._requires_release or not self._sides \
                or type(epoch) is not int or epoch != self._epoch \
                or not isinstance(candidate, JointCommand):
            return False
        if candidate.sequence_id != self._last_input_seq \
                or candidate.sequence_id <= self._last_candidate_seq \
                or not self._input.created_time_ns <= candidate.created_time_ns <= self._clock_ns:
            return False
        self._candidate = candidate
        self._candidate_sides = self._sides
        if self._inactive is not None:
            for side, names in (('left', ARM_NAMES[:7]),
                                ('right', ARM_NAMES[7:])):
                if side in self._candidate_sides:
                    for name in names:
                        self._inactive.pop(name, None)
        self._last_candidate_seq = candidate.sequence_id
        return True

    def _solve_input_ready(self, now_ns):
        if self._terminal() or not self._clock(now_ns):
            return False
        if self._input is None or not 0 <= now_ns-self._input.created_time_ns < self._input_age_ns:
            self._invalidate('input timeout')
            return False
        if self._recovery_deadline is not None and now_ns >= self._recovery_deadline:
            self._invalidate('IK recovery window expired; release Grip before retry')
            return False
        return not self._requires_release and bool(self._sides)

    def begin_solve(self, *, now_ns: int):
        """Reserve the latest observed input for ONE serial IK worker.

        Call all session methods on the control thread. The IK worker receives
        immutable inputs and returns its result to that thread; it must not call
        this session concurrently. No queue of historical input is accumulated.
        A cancelled solve still occupies the slot until its result is drained,
        so a release cannot start a second call against shared official IK state.
        """
        if self._return_profile is not None or not self._solve_input_ready(now_ns) or self._solve_ticket is not None \
                or self._last_input_seq <= max(self._last_solve_seq, self._last_candidate_seq):
            return None
        ticket = ArmSolveTicket(self._input, self._epoch, now_ns)
        self._solve_ticket = ticket
        self._last_solve_seq = self._last_input_seq
        return ticket

    def _consume_solve(self, ticket, now_ns):
        # Identity, not dataclass equality: fabricated/copied/cross-session
        # tickets must neither be accepted nor consume the actual reservation.
        if ticket is None or ticket is not self._solve_ticket:
            return False
        self._solve_ticket = None
        if not self._solve_input_ready(now_ns):
            return False
        if ticket.epoch != self._epoch \
                or ticket.sample.active_sides != self._sides \
                or ticket.sample.sequence_id <= self._last_candidate_seq:
            return False
        # Success and failure share the original source deadline. Fresh input
        # can renew a frozen reference, never the age of this computation.
        # Check here as a queued result can be drained before the caller's
        # in-flight timeout check. Ignore cancelled generations above first.
        if now_ns-ticket.sample.created_time_ns >= 200_000_000:
            self._invalidate('IK solve exceeded 200ms source deadline; release Grip before retry')
            return False
        return True

    def _reject_solved(self, ticket, reason, *, recoverable=False):
        previous_deadline = self.candidate_deadline_ns
        self._candidate = None
        self._initial_numeric_wait = False
        self._last_candidate_seq = ticket.sample.sequence_id
        rejection = 'IK candidate rejected: '+reason
        if recoverable is True and self._recover_unreachable and self._moving \
                and self._last_published is not None and previous_deadline is not None:
            # Frozen output is a fresh, unchanged reference, not replay of an
            # old IK goal. Only new source input renews its 100 ms lease.
            deadline = self._input.created_time_ns + self._input_age_ns
            if self._clock_ns < deadline:
                self._recovery_deadline = deadline
                self._status, self._reason = 'waiting_target', rejection
                return
        if self._moving:
            self._invalidate(rejection)
        else:
            self._pause(rejection)
            if not self._terminal():
                # An ordinary numerical miss does not revoke a valid Grip.
                # Before first output there is no old motion to brake/replay;
                # wait without publishing. Missing/expired/invalid data never
                # sets this marker and keeps the original stop boundary.
                self._initial_numeric_wait = (recoverable is True
                    and self._recover_unreachable)
                self._status, self._reason = 'waiting_candidate', rejection

    def offer_solved(self, ticket: ArmSolveTicket, candidate: JointCommand, *, now_ns: int):
        """Accept bounded-age IK from the same Grip epoch despite newer input.

        A current input lease and the original candidate lease are independent.
        Normalizing to the reserved source timestamp only SHORTENS lifetime;
        completion time can never turn an old pose into a fresh command.
        """
        if not self._consume_solve(ticket, now_ns):
            return False
        source_ns = ticket.sample.created_time_ns
        if not isinstance(candidate, JointCommand) \
                or candidate.sequence_id != ticket.sample.sequence_id \
                or not source_ns <= candidate.created_time_ns <= ticket.started_time_ns \
                or candidate.ttl_ms > 200 \
                or not 0 <= now_ns-source_ns < candidate.ttl_ms*1_000_000:
            self._reject_solved(ticket, 'invalid or expired asynchronous result')
            return False
        self._candidate = replace(candidate, created_time_ns=source_ns)
        self._initial_numeric_wait = False
        self._candidate_sides = ticket.sample.active_sides
        if self._inactive is not None:
            for side, names in (('left', ARM_NAMES[:7]),
                                ('right', ARM_NAMES[7:])):
                if side in self._candidate_sides:
                    for name in names:
                        self._inactive.pop(name, None)
        self._last_candidate_seq = candidate.sequence_id
        if self._recovery_deadline is not None:
            self._resume_check = True
            self._recovery_deadline = None
        return True

    def reject_solve(self, ticket: ArmSolveTicket, *, reason: str, now_ns: int, recoverable=False):
        """Consume a failed in-flight solve without replaying the previous goal."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('candidate rejection reason is required')
        if not self._consume_solve(ticket, now_ns):
            return False
        self._reject_solved(ticket, reason.strip(), recoverable=recoverable)
        return True

    def reject_candidate(self, *, sequence_id: int, epoch: int, reason: str):
        """Fence an explicit failure for the newest input from older candidates.

        An IK rejection is itself a result for the current frame.  Keeping the
        previous candidate would silently replay an obsolete target, so consume
        this sequence number, clear the candidate and pause until a newer frame
        produces a valid one.  Late failures from older epochs/frames are ignored.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('candidate rejection reason is required')
        if self._terminal() or self._requires_release or not self._sides \
                or type(sequence_id) is not int or sequence_id < 0 \
                or type(epoch) is not int or epoch != self._epoch \
                or sequence_id != self._last_input_seq:
            return False
        self._candidate = None
        self._candidate_sides = ()
        self._initial_numeric_wait = False
        self._last_candidate_seq = max(self._last_candidate_seq, sequence_id)
        rejection = 'IK candidate rejected: '+reason.strip()
        if self._moving:
            self._invalidate(rejection)
        else:
            self._pause(rejection)
            if not self._terminal():
                self._status = 'waiting_candidate'
                self._reason = rejection
        return True

    def _check_feedback(self, feedback, now_ns):
        if not isinstance(feedback, RobotState) \
                or not 0 <= now_ns-feedback.timestamp_ns <= self._state_age_ns:
            raise MotionGateError('feedback stale or future')
        for j in self.schema.joints:
            v = feedback.positions.get(j.name)
            if isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) \
                    or not j.position_min <= v <= j.position_max:
                raise MotionGateError('invalid feedback: '+j.name)
            if self._session_schema is not None:
                bound = self._session_schema.get(j.name)
                if not bound.position_min <= v <= bound.position_max:
                    raise MotionGateError('feedback outside fixed session envelope: '+j.name)

    def tick(self, feedback: RobotState, *, now_ns: int, publication_allowed=True):
        """One bounded planning tick. No thread, ROS node or hardware watchdog here."""
        if self._terminal() or not self._clock(now_ns):
            return self._result()
        if self._input is None or not 0 <= now_ns-self._input.created_time_ns < self._input_age_ns:
            self._invalidate('input timeout')
            return self._result()
        try:
            self._check_feedback(feedback, now_ns)
        except MotionGateError as exc:
            self._invalidate(str(exc))
            return self._result()
        if self._session_schema is None:
            offset = self._options['max_offset_rad']
            self._session_schema = (self.schema if offset is None else JointSchema(tuple(
                replace(j, position_min=max(j.position_min, feedback.positions[j.name]-offset),
                        position_max=min(j.position_max, feedback.positions[j.name]+offset))
                for j in self.schema.joints)))
        if self._requires_release or not self._sides:
            return self._result()
        if self._recovery_deadline is not None:
            return self._tick_recovery(feedback, now_ns, publication_allowed)
        if self._inactive is None:
            self._inactive = {n: feedback.positions[n] for side, names in
                              (('left', ARM_NAMES[:7]), ('right', ARM_NAMES[7:]))
                              if side not in set(self._sides).intersection(self._candidate_sides)
                              for n in names}
        candidate = self._candidate
        if candidate is None or not 0 <= now_ns-candidate.created_time_ns < candidate.ttl_ms*1_000_000:
            wait_reason = (self._reason if candidate is None
                           and self._status == 'waiting_candidate'
                           and self._reason.startswith('IK candidate rejected: ')
                           else 'IK candidate missing or expired')
            if self._moving:
                # Once a command has been published, losing the candidate is a
                # stop boundary.  A continuously held Grip must never turn the
                # resulting hold into an automatic rearm.
                self._invalidate(wait_reason)
            else:
                self._pause(wait_reason)
                if not self._terminal():
                    self._status, self._reason = 'waiting_candidate', wait_reason
            return self._result()
        try:
            # Validate ALL IK values before masking the inactive side.
            JointMotionGate(self.schema, **self._options)._check_inputs(candidate, feedback, now_ns)
            session_schema = self._session_schema
            inactive = self._inactive
            # Fix the session envelope before every segment, not just each Grip press.
            targets = {j.name: min(max(candidate.targets[j.name], j.position_min), j.position_max)
                       for j in session_schema.joints}
            targets.update(inactive)
            if self._tracking_governor is not None:
                previous = (dict(feedback.positions) if self._last_published is None
                            else dict(self._last_published))
                velocities = (None if self._gate is None
                              else self._gate._previous_velocity)
                tracking = self._tracking_governor.apply(
                    targets, feedback.positions, previous,
                    active_sides=self._sides, now_ns=now_ns,
                    previous_velocities=velocities)
                targets = dict(tracking.targets)
                targets.update(inactive)
                self._record_tracking(tracking, phase='bounded_candidate')
            bounded = replace(candidate, targets=targets)
            # advance replaces its dictionaries; fork so a failed publication cannot commit.
            trial = copy(self._gate) if self._gate is not None else JointMotionGate(session_schema, **self._options)
            if trial._previous_velocity is not None and inactive:
                # A released hand is explicitly frozen, not allowed to keep
                # moving under an old acceleration planner's braking state.
                # Preserve the continuously engaged hand's dynamic state.
                trial._previous_velocity = {
                    name: 0. if name in inactive else velocity
                    for name, velocity in trial._previous_velocity.items()}
            if self._last_publish_ns is not None:
                elapsed = (now_ns-self._last_publish_ns)/1e9
                if elapsed <= 0:
                    self._status, self._reason = 'waiting_tick', 'no new control time elapsed'
                    return self._result()
                trial.period = min(self._options['period_s'], elapsed)
            plan = trial.advance(bounded, feedback, now_ns=now_ns)
            if self._resume_check:
                # Tracking error compares the bounded reference about to be
                # sent with actual feedback, not the distant final IK goal.
                # Otherwise lowering speed creates a deadlock: a valid goal
                # stays far away precisely because recovery refuses to move.
                for name in ARM_NAMES:
                    if abs(plan.command.targets[name]-feedback.positions[name]) > self._options['max_tracking_error_rad']:
                        self._candidate = None
                        self._candidate_sides = ()
                        self._resume_check = False
                        self._recovery_deadline = self._input.created_time_ns + self._input_age_ns
                        self._status = 'waiting_target'
                        self._reason = 'recovery next reference outside tracking bound: '+name
                        return self._tick_recovery(feedback, now_ns, publication_allowed)
            for name in ARM_NAMES:
                if abs(plan.command.targets[name]-feedback.positions[name]) \
                        > self._options['max_tracking_error_rad']+1e-12:
                    raise MotionGateError('planned reference outside tracking bound: '+name)
            encoded = encode_kuavo_arm_joint_command(plan.command)
            for name, degrees in zip(encoded.names, encoded.positions_deg):
                if abs(math.radians(degrees)-feedback.positions[name]) \
                        > self._options['max_tracking_error_rad']:
                    raise MotionGateError('encoded reference outside tracking bound: '+name)
        except (ValueError, TypeError, KeyError) as exc:
            self._invalidate(str(exc))
            return self._result()
        if publication_allowed is not True:
            self._status, self._reason = 'waiting_transport', 'external arm mode not acknowledged'
            return self._result()
        self._moving = True  # A transport exception may occur after partial delivery.
        try:
            published_ns = self._publish(encoded)
            # Live transports return a trusted timestamp AFTER enqueue. Using
            # that conservative origin keeps the next plan below the actual
            # send-to-send speed bound even when computation delay varies.
            if published_ns is not None and (type(published_ns) is not int or published_ns < now_ns):
                raise ValueError('invalid publication completion timestamp')
        except RuntimeMotionPaused as exc:
            self._invalidate(str(exc))
            return self._result()
        except Exception as exc:
            self._status, self._reason = 'fault', f'publish failed: {exc}'
            self._pause(self._reason)
            return self._result()
        self._gate, self._session_schema, self._inactive = trial, session_schema, inactive
        self._last_published = dict(plan.command.targets)
        self._last_publish_ns = now_ns if published_ns is None else published_ns
        self._resume_check = False
        self._status = 'published'
        stalled = (None if self._tracking_diagnostics is None else
                   self._tracking_diagnostics['stalled_joint'])
        if stalled is None:
            self._reason = 'transport callback returned; physical execution unverified'
        else:
            self._reason = ('tracking limited without disconnect: '+stalled+
                            '; reverse target remains available')
        return self._result(plan.max_step_rad)

    def _tick_recovery(self, feedback, now_ns, publication_allowed):
        """Brake the accepted reference, then hold while source input is fresh.

        Rejected IK never supplies a new destination. A moving reference sheds
        its existing velocity gradually instead of discontinuously commanding
        zero on every ordinary solve failure. Hard stops and Grip release keep
        their independent authority. This is reference planning, not a claim
        about physical stopping distance or measured motor acceleration.
        """
        if now_ns >= self._recovery_deadline:
            self._invalidate('IK recovery window expired; release Grip before retry')
            return self._result()
        try:
            if self._tracking_governor is not None and self._last_published is not None:
                velocities = (None if self._gate is None
                              else self._gate._previous_velocity)
                tracking = self._tracking_governor.observe_recovery(
                    feedback.positions, self._last_published,
                    active_sides=self._sides, now_ns=now_ns,
                    previous_velocities=velocities)
                self._record_tracking(tracking, phase='ik_recovery_existing_watch')
            if any(abs(self._last_published[n]-feedback.positions[n]) > self._options['max_tracking_error_rad']
                   for n in ARM_NAMES):
                raise MotionGateError('feedback tracking error during IK recovery')
            trial = copy(self._gate) if self._gate is not None else None
            positions = dict(self._last_published)
            acceleration = self._options['max_acceleration_rad_s2']
            elapsed = (self._options['period_s'] if self._last_publish_ns is None
                       else min(self._options['period_s'],
                                max(0., (now_ns-self._last_publish_ns)/1e9)))
            if trial is not None and trial._previous_velocity is not None:
                velocities = dict.fromkeys(self.schema.names, 0.)
                active = {name for side, names in (('left', ARM_NAMES[:7]),
                                                   ('right', ARM_NAMES[7:]))
                          if side in self._sides for name in names}
                if acceleration is not None:
                    for name in active:
                        previous_velocity = trial._previous_velocity[name]
                        velocity = math.copysign(
                            max(0., abs(previous_velocity)-acceleration*elapsed),
                            previous_velocity)
                        velocities[name] = velocity
                        positions[name] += velocity*elapsed
                trial._previous_velocity = velocities
                trial._previous = dict(positions)
                trial._last_tick_ns = now_ns
            reference = JointCommand(positions, 'kuavo_ik_wait_reference',
                                     self._last_input_seq, now_ns, 1)
            JointMotionGate(self._session_schema or self.schema, **self._options)._check_inputs(
                reference, feedback, now_ns)
            if any(abs(positions[name]-feedback.positions[name]) > self._options['max_tracking_error_rad']
                   for name in ARM_NAMES):
                raise MotionGateError('feedback tracking error during IK recovery braking')
            encoded = encode_kuavo_arm_joint_command(reference)
            # The ROS message contains degrees, not the pre-encoding radian
            # values above. Even an exact 0.2 rad can round-trip slightly past
            # the transport's unchanged strict bound. Keep the old reference
            # and wait for a new target instead of causing a sender fault.
            for name, degrees in zip(encoded.names, encoded.positions_deg):
                if abs(math.radians(degrees)-feedback.positions[name]) \
                        > self._options['max_tracking_error_rad']:
                    self._status = 'waiting_target'
                    self._reason = 'recovery next reference outside tracking bound: '+name
                    return self._result()
        except (ValueError, TypeError, KeyError) as exc:
            self._invalidate(str(exc))
            return self._result()
        self._status = 'waiting_target'
        if not self._reason.startswith('recovery next reference outside tracking bound:'):
            self._reason = 'ordinary IK unreachable; bounded braking/hold reference'
        if publication_allowed is not True:
            return self._result()
        if self._last_publish_ns is not None and now_ns <= self._last_publish_ns:
            return self._result()
        try:
            published_ns = self._publish(encoded)
            if published_ns is not None and (type(published_ns) is not int or published_ns < now_ns):
                raise ValueError('invalid publication completion timestamp')
        except RuntimeMotionPaused as exc:
            self._invalidate(str(exc))
            return self._result()
        except Exception as exc:
            self._status, self._reason = 'fault', 'recovery publish failed: '+str(exc)
            self._invalidate(self._reason)
            return self._result()
        self._last_publish_ns = now_ns if published_ns is None else published_ns
        step = max(abs(positions[name]-self._last_published[name]) for name in ARM_NAMES)
        self._last_published = positions
        self._gate = trial
        return self._result(step)

    def close(self):
        if self._terminal():
            return self._result()
        self._pause('session closed')
        self._reset_tracking()
        self._epoch += 1
        self._retarget_epoch += 1
        self._candidate = None
        self._candidate_sides = ()
        self._recovery_deadline = None
        self._resume_check = False
        if not self._terminal():
            self._status, self._reason = 'closed', 'no further publications'
        return self._result()
