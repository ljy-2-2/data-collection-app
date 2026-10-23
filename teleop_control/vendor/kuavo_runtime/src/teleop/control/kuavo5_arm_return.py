"""Fixed joint return configuration and measured arrival evidence; no I/O.

Joint limits and a bounded reference are not a collision planner. Neither the
robot meshes nor the surrounding environment are checked by this module.
"""

import math
from numbers import Real

from teleop.robots.kuavo5_native_ik import ARM_NAMES


class ArmReturnState:
    DEFAULTS = dict(max_velocity_rad_s=.3, max_acceleration_rad_s2=.6,
                    timeout_s=120., position_tolerance_rad=.03,
                    quiet_velocity_rad_s=.05, settle_seconds=.3)
    ACTIVE = ('preparing', 'running', 'settling')

    def __init__(self, schema, config):
        if not isinstance(config, dict) or set(config)-({'targets'} | set(self.DEFAULTS)):
            raise ValueError('unknown or invalid arm return configuration')
        targets = config.get('targets')
        if not hasattr(targets, 'keys') or set(targets) != set(ARM_NAMES) \
                or tuple(schema.names) != ARM_NAMES:
            raise ValueError('return targets must contain exactly the 14 arm joints')
        self.targets = {}
        for joint in schema.joints:
            value = targets[joint.name]
            if isinstance(value, bool) or not isinstance(value, Real) \
                    or not math.isfinite(value) \
                    or not joint.position_min <= value <= joint.position_max:
                raise ValueError('return target outside joint limits: '+joint.name)
            self.targets[joint.name] = float(value)
        self.options = dict(self.DEFAULTS, **{k: v for k, v in config.items() if k != 'targets'})
        ceilings = dict(max_velocity_rad_s=.3, max_acceleration_rad_s2=.6,
                        timeout_s=120., position_tolerance_rad=.05,
                        quiet_velocity_rad_s=.1, settle_seconds=2.)
        for name, value in self.options.items():
            if isinstance(value, bool) or not isinstance(value, Real) \
                    or not math.isfinite(value) or not 0 < value <= ceilings[name]:
                raise ValueError('invalid arm return '+name)
            self.options[name] = float(value)
        if self.options['settle_seconds'] < .1:
            raise ValueError('arm return requires at least 100ms of quiet feedback')
        speed, acceleration = (self.options['max_velocity_rad_s'],
                               self.options['max_acceleration_rad_s2'])
        if .06+speed*speed/(2*acceleration)+speed*.02 >= .2:
            raise ValueError('arm return lacks tracking-bound braking reserve')
        self.state, self.reason = 'idle', 'waiting for an explicit return request'
        self.started_ns = self.deadline_ns = self.finished_ns = None
        self._previous = self._quiet_since = None
        self._quiet_samples = 0
        self.max_error = self.max_speed = None
        self.origin = None

    @property
    def active(self):
        return self.state in self.ACTIVE

    def request(self, now_ns):
        self.state, self.reason = 'preparing', 'waiting for hold acknowledgement and newer observations'
        self.started_ns, self.deadline_ns = now_ns, now_ns+int(self.options['timeout_s']*1e9)
        self.finished_ns = None
        self._previous = self._quiet_since = None
        self._quiet_samples = 0
        self.max_error = self.max_speed = None
        self.origin = None

    def finish(self, state, reason, now_ns):
        if state not in ('completed', 'cancelled', 'failed'):
            raise ValueError('invalid return outcome')
        self.state, self.reason, self.finished_ns = state, str(reason), now_ns
        self._quiet_since = None
        self._quiet_samples = 0

    def activate(self, feedback):
        self.origin = {n: float(feedback.positions[n]) for n in ARM_NAMES}
        self.state, self.reason = 'running', 'bounded joint return; collision avoidance unavailable'
        self._previous = feedback

    def arrived(self, feedback, reference):
        """Require distinct measured samples, never simulated planned progress."""
        previous = self._previous
        if previous is not None and feedback.timestamp_ns <= previous.timestamp_ns:
            return False
        self._previous = feedback
        self.max_error = max(abs(feedback.positions[n]-self.targets[n]) for n in ARM_NAMES)
        self.max_speed = (None if previous is None else max(
            abs(feedback.positions[n]-previous.positions[n]) /
            ((feedback.timestamp_ns-previous.timestamp_ns)/1e9) for n in ARM_NAMES))
        near_reference = reference is not None and all(
            abs(reference[n]-self.targets[n]) <= self.options['position_tolerance_rad']
            for n in ARM_NAMES)
        # A large sample gap is not evidence of continuous quiet motion.
        quiet = previous is not None and 0 < feedback.timestamp_ns-previous.timestamp_ns <= 100_000_000 \
            and self.max_error <= self.options['position_tolerance_rad'] \
            and self.max_speed <= self.options['quiet_velocity_rad_s'] and near_reference
        if not quiet:
            self._quiet_since = None
            self._quiet_samples = 0
            self.state, self.reason = 'running', 'waiting for measured arrival and quiet feedback'
            return False
        if self._quiet_since is None:
            self._quiet_since = feedback.timestamp_ns
        self._quiet_samples += 1
        self.state, self.reason = 'settling', 'checking distinct quiet measured feedback samples'
        return self._quiet_samples >= 3 and feedback.timestamp_ns-self._quiet_since \
            >= int(self.options['settle_seconds']*1e9)

    def status(self):
        return dict(enabled=True, state=self.state, active=self.active, reason=self.reason,
                    started_ns=self.started_ns, deadline_ns=self.deadline_ns,
                    finished_ns=self.finished_ns, targets_rad=dict(self.targets),
                    start_positions_rad=None if self.origin is None else dict(self.origin),
                    max_position_error_rad=self.max_error,
                    measured_secant_speed_rad_s=self.max_speed,
                    quiet_sample_count=self._quiet_samples,
                    joint_target_limits_validated=True, collision_checked=False,
                    environment_collision_checked=False, **self.options)
