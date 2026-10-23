"""Pure bounded joint planning; not an actuator or permission to send motion."""

from dataclasses import dataclass
import math
from numbers import Real

from teleop.contracts.command import JointCommand
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointSchema


class MotionGateError(ValueError):
    """No new plan was produced; the caller must not advance an actuator."""


@dataclass(frozen=True)
class JointMotionPlan:
    command: JointCommand
    range_clamped_joints: tuple[str, ...]
    max_step_rad: float
    physical_robot_command_sent: bool = False


class JointMotionGate:
    """Slew toward a candidate inside an initial, test-only joint envelope.

    Feedback freshness and candidate lifetime are checked on every tick. This
    module deliberately does not alter readonly schemas or return a validated
    command. Collision checks and physical-controller semantics are separate.
    """

    def __init__(self, schema: JointSchema, *, max_velocity_rad_s=0.6,
                 max_acceleration_rad_s2=None,
                 max_offset_rad=math.radians(10), period_s=0.02,
                 max_tracking_error_rad=0.2, max_state_age_ms=250,
                 synchronization_groups=None):
        if not isinstance(schema, JointSchema) or not schema.names:
            raise ValueError("non-empty joint schema is required")
        for name, value in (("max_velocity_rad_s", max_velocity_rad_s),
                            ("period_s", period_s),
                            ("max_tracking_error_rad", max_tracking_error_rad)):
            if isinstance(value, bool) or not isinstance(value, Real) \
                    or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if max_offset_rad is not None and (
                isinstance(max_offset_rad, bool) or not isinstance(max_offset_rad, Real)
                or not math.isfinite(max_offset_rad) or max_offset_rad <= 0):
            raise ValueError("max_offset_rad must be None or finite and positive")
        if max_acceleration_rad_s2 is not None and (
                isinstance(max_acceleration_rad_s2, bool)
                or not isinstance(max_acceleration_rad_s2, Real)
                or not math.isfinite(max_acceleration_rad_s2)
                or max_acceleration_rad_s2 <= 0):
            raise ValueError("max_acceleration_rad_s2 must be finite and positive")
        if type(max_state_age_ms) is not int or max_state_age_ms <= 0:
            raise ValueError("max_state_age_ms must be a positive integer")
        if synchronization_groups is None:
            groups = (tuple(schema.names),)
        else:
            try:
                if isinstance(synchronization_groups, (str, bytes)):
                    raise ValueError('groups must partition the joint schema')
                groups = tuple(tuple(group) for group in synchronization_groups)
                flattened = tuple(name for group in groups for name in group)
                if not groups or any(not group for group in groups) \
                        or len(flattened) != len(schema.names) \
                        or set(flattened) != set(schema.names):
                    raise ValueError('groups must partition the joint schema')
            except TypeError as exc:
                raise ValueError('groups must partition the joint schema') from exc
        self.synchronization_groups = groups
        self.schema = schema
        self.velocity = float(max_velocity_rad_s)
        self.acceleration = (None if max_acceleration_rad_s2 is None
                             else float(max_acceleration_rad_s2))
        self.offset = None if max_offset_rad is None else float(max_offset_rad)
        self.period = float(period_s)
        self.tracking_error = float(max_tracking_error_rad)
        self.state_age_ns = max_state_age_ms * 1_000_000
        self.reset()

    def reset(self):
        self._anchor = None
        self._previous = None
        self._last_tick_ns = None
        self._previous_velocity = None

    @staticmethod
    def _braking_velocity_limit(distance, acceleration, elapsed, speed):
        """Largest current-tick speed with interval-independent stop margin.

        Position is advanced with the end-of-tick velocity.  Reserving both
        this tick's displacement and the continuous braking distance keeps the
        bound valid even when later control intervals become shorter.
        """
        if distance <= 0:
            return 0.
        interval_change = acceleration*elapsed
        limit = math.sqrt(interval_change*interval_change+2*acceleration*distance) \
                - interval_change
        return min(speed, limit)

    def _check_inputs(self, candidate, feedback, now_ns):
        if not isinstance(candidate, JointCommand) or not isinstance(feedback, RobotState):
            raise MotionGateError("JointCommand and RobotState are required")
        if type(now_ns) is not int or now_ns < 0:
            raise MotionGateError("invalid planning clock")
        if not 0 <= now_ns-feedback.timestamp_ns <= self.state_age_ns:
            raise MotionGateError("feedback is stale or in the future")
        if not 0 <= now_ns-candidate.created_time_ns < candidate.ttl_ms * 1_000_000:
            raise MotionGateError("candidate expired or is in the future")
        if set(candidate.targets) != set(self.schema.names):
            raise MotionGateError("candidate must contain exactly the configured joint set")
        for joint in self.schema.joints:
            for source, positions in (("candidate", candidate.targets),
                                      ("feedback", feedback.positions)):
                value = positions.get(joint.name)
                if isinstance(value, bool) or not isinstance(value, Real) \
                        or not math.isfinite(value):
                    raise MotionGateError(f"invalid {source} value for {joint.name}")
                if not joint.position_min <= value <= joint.position_max:
                    raise MotionGateError(f"{source} exceeds joint limit: {joint.name}")

    def advance(self, candidate: JointCommand, feedback: RobotState, *, now_ns: int):
        self._check_inputs(candidate, feedback, now_ns)
        if self._last_tick_ns is not None and now_ns < self._last_tick_ns:
            raise MotionGateError("planning clock moved backwards")
        previous = self._previous or {n: float(feedback.positions[n]) for n in self.schema.names}
        if self._previous is not None and any(
                abs(previous[n]-feedback.positions[n]) > self.tracking_error
                for n in self.schema.names):
            raise MotionGateError("feedback tracking error; plan paused")
        anchor = self._anchor or dict(previous)
        elapsed = (self.period if self._last_tick_ns is None
                   else min(self.period, (now_ns-self._last_tick_ns) / 1_000_000_000))
        bounded, clamped = {}, []
        for joint in self.schema.joints:
            lower = (joint.position_min if self.offset is None else
                     max(joint.position_min, anchor[joint.name]-self.offset))
            upper = (joint.position_max if self.offset is None else
                     min(joint.position_max, anchor[joint.name]+self.offset))
            requested = candidate.targets[joint.name]
            bounded[joint.name] = min(max(requested, lower), upper)
            if bounded[joint.name] != requested:
                clamped.append(joint.name)
        delta = {n: bounded[n]-previous[n] for n in self.schema.names}
        # Synchronize only joints that belong to the same configured group.
        # The default remains one group, preserving existing callers' behavior.
        factors = {}
        for group in self.synchronization_groups:
            factor = 1.0
            for name in group:
                joint = self.schema.get(name)
                speed = min(self.velocity, joint.max_velocity or self.velocity)
                if delta[name]:
                    factor = min(factor, speed*elapsed / abs(delta[name]))
            factors.update(dict.fromkeys(group, factor))
        if self.acceleration is None:
            targets = {n: previous[n]+factors[n]*delta[n] for n in self.schema.names}
        elif elapsed <= 0:
            targets = dict(previous)
        else:
            previous_velocity = self._previous_velocity or dict.fromkeys(self.schema.names, 0.)
            targets, velocities = {}, {}
            for joint in self.schema.joints:
                name = joint.name
                speed = min(self.velocity, joint.max_velocity or self.velocity)
                change = self.acceleration*elapsed
                distance = abs(delta[name])
                target_speed = self._braking_velocity_limit(
                    distance, self.acceleration, elapsed, speed)
                desired_velocity = math.copysign(
                    min(abs(factors[name]*delta[name]/elapsed), target_speed), delta[name]) \
                    if delta[name] else 0.
                accel_low = max(-speed, previous_velocity[name]-change)
                accel_high = min(speed, previous_velocity[name]+change)
                lower = (joint.position_min if self.offset is None else
                         max(joint.position_min, anchor[name]-self.offset))
                upper = (joint.position_max if self.offset is None else
                         min(joint.position_max, anchor[name]+self.offset))
                safe_low = -self._braking_velocity_limit(
                    previous[name]-lower, self.acceleration, elapsed, speed)
                safe_high = self._braking_velocity_limit(
                    upper-previous[name], self.acceleration, elapsed, speed)
                allowed_low, allowed_high = max(accel_low, safe_low), min(accel_high, safe_high)
                if allowed_low > allowed_high+1e-9:
                    raise MotionGateError('cannot preserve acceleration and range bounds')
                if allowed_low > allowed_high:
                    allowed_high = allowed_low
                velocity = min(max(desired_velocity, allowed_low), allowed_high)
                target = previous[name]+velocity*elapsed
                targets[name] = target
                velocities[name] = velocity
            self._previous_velocity = velocities
        command = JointCommand(targets, candidate.source+"_bounded_plan", candidate.sequence_id,
                               candidate.created_time_ns, candidate.ttl_ms)
        max_step = max(abs(targets[n]-previous[n]) for n in self.schema.names)
        self._anchor, self._previous, self._last_tick_ns = anchor, targets, now_ns
        return JointMotionPlan(command, tuple(clamped), max_step)
