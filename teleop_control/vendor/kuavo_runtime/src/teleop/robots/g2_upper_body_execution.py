"""G2-specific final checks and normal SDK transport for nineteen-joint targets.

No SDK import, initialization, or physical action occurs at import/construction.
The caller must check input freshness immediately before invoking RobotAdapter.
"""

import math

from teleop.contracts.command import ValidatedJointCommand
from teleop.control.safety import SafetyGateway
from teleop.robots.g2 import G2JointControlRequestPreview, make_g2_joint_schema
from teleop.robots.g2_upper_body_safety import (
    G2UpperBodySafetyProfile,
    UpperBodyEnvelopeError,
)


class UpperBodyPause(ValueError):
    """Skip this candidate without discarding the operator's fixed-hand anchor."""


class UpperBodyRateWait(UpperBodyPause):
    """An otherwise bounded target arrived before its earliest send time."""
    def __init__(self, delay_s):
        self.delay_s = delay_s
        super().__init__(f'wait {delay_s * 1000:.3f} ms for velocity budget')


class G2UpperBodyExecutionGate:
    def __init__(self, collision_model):
        self.profile = G2UpperBodySafetyProfile.balanced()
        self.schema = make_g2_joint_schema(
            arm_velocity_rad_s=self.profile.arm_velocity_rad_s,
            body_velocity_rad_s=self.profile.body_velocity_rad_s,
        )
        self.names = tuple(n for n in self.schema.names if '_head_' not in n)
        self.model = collision_model
        self._anchor = None
        self._grippers = None
        self._sent = None
        self._sent_ns = None
        self._prepared = None
        self.minimum_distance_m = math.inf
        self.path_samples = 0

    def reset_motion(self):
        """Declutch command history; retain the process-wide body/head envelope."""
        self._sent = self._sent_ns = self._prepared = None

    @property
    def needs_anchor(self):
        return self._sent is None

    def prepare(self, command, state, gripper_fractions, *, now_ns):
        self._prepared = None
        if set(command.targets) != set(self.names):
            raise ValueError('upper-body execution requires exactly nineteen body/arm joints')
        if command.ttl_ms > 100 or now_ns >= command.created_time_ns + command.ttl_ms * 1_000_000:
            raise ValueError('upper-body command expired or TTL exceeds 100 ms')
        validated = SafetyGateway(max_state_age_ms=100).validate(
            command, state, self.schema, now_ns=now_ns)
        if set(gripper_fractions) != {'left', 'right'} or any(
            isinstance(v, bool) or not isinstance(v, (float, int)) or
            not math.isfinite(v) or not 0 <= v <= 1 for v in gripper_fractions.values()
        ):
            raise ValueError('complete finite gripper feedback is required')
        if self._anchor is None:
            self._anchor = dict(state.positions)
            self._grippers = dict(gripper_fractions)
        candidate = dict(state.positions)
        candidate.update(validated.targets)
        try:
            self.profile.validate_body_transition(
                self._anchor, state.positions, candidate
            )
        except UpperBodyEnvelopeError as error:
            raise UpperBodyPause(str(error)) from error
        for name in self.schema.names:
            if '_head_' in name and abs(state.positions[name] - self._anchor[name]) > .01:
                raise ValueError('external head motion detected; stop the upper-body session')
        if any(abs(gripper_fractions[s] - self._grippers[s]) > .02 for s in self._grippers):
            raise ValueError('external gripper motion detected; stop the upper-body session')

        reference = self._sent if self._sent is not None else state.positions
        if self._sent_ns is None:
            # First SDK request is the observed posture, never an IK jump.
            dt = 0.0
        else:
            dt = (now_ns - self._sent_ns) / 1e9
            if dt <= 0:
                raise UpperBodyPause('send timestamp must advance')
            dt = min(dt, .02)  # No accumulated catch-up step after a late cycle.
        required_dt = max(abs(candidate[n] - reference[n]) / self.schema.get(n).max_velocity
                          for n in self.names)
        if required_dt > dt + 1e-12:
            if self._sent_ns is not None and required_dt <= .02 + 1e-12:
                raise UpperBodyRateWait(required_dt - dt)
            raise UpperBodyPause('candidate exceeds elapsed-time velocity step')

        grippers = {s: -.785 + .785 * gripper_fractions[s] for s in ('left', 'right')}
        starts = [state.positions]
        if self._sent is not None:
            starts.append(dict(state.positions, **self._sent))
        for start in starts:
            result = self.model.check_path(
                start, candidate, start_grippers=grippers, end_grippers=grippers,
                max_step_rad=.01, max_gripper_step_rad=.10, margin_m=.005)
            self.path_samples += result.sample_count
            self.minimum_distance_m = min(self.minimum_distance_m, result.minimum_distance_m)
            if not result.clear:
                raise UpperBodyPause(f'mesh collision: {result.first_violation}')
        self._prepared = validated
        return validated

    def accept_sent(self, command, *, now_ns):
        if command is not self._prepared or not isinstance(command, ValidatedJointCommand):
            raise ValueError('only the exact prepared command can advance send history')
        self._sent = dict(command.targets)
        self._sent_ns = now_ns
        self._prepared = None


class G2UpperBodyServoSender:
    """One normal-mode vendor request; an SDK failure permanently disables this sender."""

    def __init__(self, *, gdk, robot):
        self._gdk, self._robot = gdk, robot
        profile = G2UpperBodySafetyProfile.balanced()
        self._schema = make_g2_joint_schema(
            arm_velocity_rad_s=profile.arm_velocity_rad_s,
            body_velocity_rad_s=profile.body_velocity_rad_s,
        )
        self._names = tuple(n for n in self._schema.names if '_head_' not in n)
        self._control_period_s = 1.2 / profile.control_hz
        self._failed = False
        self.attempts = 0

    def send(self, request):
        if self._failed:
            raise RuntimeError('upper-body SDK sender latched after an uncertain send')
        if not isinstance(request, G2JointControlRequestPreview):
            raise TypeError('expected validated adapter request')
        if tuple(request.joint_names) != self._names or not (
            len(request.joint_positions) == len(request.joint_velocities) == 19
        ):
            raise ValueError('normal upper-body sender requires the ordered nineteen joints')
        if not math.isfinite(request.life_time_s) or not 0 < request.life_time_s <= .1:
            raise ValueError('upper-body request TTL must be within (0, 100] ms')
        for name, position, velocity in zip(request.joint_names, request.joint_positions,
                                             request.joint_velocities):
            joint = self._schema.get(name)
            if (isinstance(position, bool) or isinstance(velocity, bool) or
                not math.isfinite(position) or not math.isfinite(velocity) or
                not joint.position_min <= position <= joint.position_max or
                not 0 <= velocity <= joint.max_velocity):
                raise ValueError(f'invalid upper-body SDK target: {name}')
        vendor = self._gdk.JointServoControlReq()
        vendor.control_period = self._control_period_s
        vendor.joint_names = list(request.joint_names)
        vendor.joint_positions = list(request.joint_positions)
        # SDK documents this field as reserved. Rate enforcement lives in the gate.
        vendor.joint_velocities = list(request.joint_velocities)
        self.attempts += 1
        try:
            result = self._robot.joint_servo_control(vendor, enable_low_latency=False)
            if result not in (None, 0):
                raise RuntimeError(f'GDK rejected upper-body request: {result!r}')
        except Exception:
            self._failed = True
            raise
