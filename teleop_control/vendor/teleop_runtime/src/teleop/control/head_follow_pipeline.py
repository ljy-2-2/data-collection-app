"""Head input -> robot-supplied joint mapper -> shared command/safety boundary."""
import time
import math
from types import MappingProxyType

from teleop.backends.direct_joint import DirectJointBackend
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.contracts.intent import JointPositionIntent
from teleop.retargeting.head_follow import HeadFollowMapper


class HeadFollowSession:
    def __init__(self, adapter, joint_mapper, *, roll_joint,
                 send_enabled=False, on_anchor=lambda state: None,
                 clock_ns=time.monotonic_ns, stop_requested=lambda: False,
                 target_rate_rad_s=0.05, target_limits=None,
                 before_send=lambda command, state, frame: None):
        if (type(target_rate_rad_s) not in (int, float) or not math.isfinite(target_rate_rad_s)
                or not 0 < target_rate_rad_s <= .3):
            raise ValueError('head target rate must be within (0, 0.3]')
        self.target_rate = target_rate_rad_s
        self.target_limits = MappingProxyType(dict(target_limits or {}))
        self.before_send = before_send
        self.adapter, self.joint_mapper, self.roll_joint = adapter, joint_mapper, roll_joint
        self.send_enabled, self.on_anchor = send_enabled, on_anchor
        self.clock, self.stop_requested = clock_ns, stop_requested
        self.mapper = HeadFollowMapper()
        self.router, self.safety = ControlRouter(DirectJointBackend()), SafetyGateway()
        self.highest_device_timestamp = 0
        self.reset()

    def reset(self):
        self.mapper.reset()
        self.anchor, self.intent, self.previous = None, None, None
        self.last_ns, self.last_input_ns = None, None
        self._progress = {}
        self.last_feedback = {}
        self.last_mapping, self.anchor_head_quaternion = {}, None
        self.status = 'waiting_for_anchor'

    def _check_progress(self, state, now):
        # SDK acceptance alone does not establish that a motor follows a target.
        # Use the last *sent* target, never the operator's unconstrained request.
        if not self.send_enabled:
            return
        for name, target in self.previous.items():
            current = state.positions[name]
            gap = target - current
            if abs(gap) <= 0.001:
                self._progress.pop(name, None)
                continue
            direction = 1 if gap > 0 else -1
            previous = self._progress.get(name)
            if previous is None or previous[2] != direction:
                self._progress[name] = (current, now, direction)
            elif direction * (current - previous[0]) >= 0.0005:
                self._progress[name] = (current, now, direction)
            elif now - previous[1] >= 4_000_000_000:
                raise ValueError(f'head no progress for 4 seconds: {name} '
                                 f'measured_rad={current:+.6f} last_sent_rad={target:+.6f}')

    def step(self, frame):
        now = self.clock()
        if self.stop_requested():
            self.reset()
            return None
        if self.anchor is None and frame.device_timestamp_ns <= self.highest_device_timestamp:
            return None
        intent = self.mapper.update(frame, now_ns=now)
        if self.mapper.status == 'stale_or_invalid_time':
            self.reset()
            self.status = 'paused_stale'
            return None
        if intent is not None:
            self.intent, self.last_input_ns = intent, frame.received_monotonic_ns
            self.highest_device_timestamp = max(self.highest_device_timestamp, frame.device_timestamp_ns)
        if self.last_input_ns is None or now - self.last_input_ns > 300_000_000:
            self.reset()
            self.status = 'paused_no_new_device_sample'
            return None
        state = self.adapter.read_state(now_ns=now)
        self.last_feedback = {name: state.positions[name] for name in
                              (self.previous or self.joint_mapper.preview(self.intent, state).anchor_positions)}
        if not 0 <= self.clock() - state.timestamp_ns <= 200_000_000:
            raise ValueError('head state read too slow')
        for name, (low, high) in self.target_limits.items():
            margin = .005 if name == self.roll_joint else .003
            if not low-margin <= state.positions[name] <= high+margin:
                raise ValueError(f'head feedback outside fixed session bounds: {name}')
        if self.anchor is None:
            self.anchor = state
            self.anchor_head_quaternion = list(frame.head_pose.orientation_xyzw)
            preview = self.joint_mapper.preview(self.intent, state)
            self.previous = {name: state.positions[name] for name in preview.targets}
            self.previous[self.roll_joint] = state.positions[self.roll_joint]
            if self.roll_joint in self.target_limits:
                self.previous[self.roll_joint] = self.target_limits[self.roll_joint][0]
            self.on_anchor(state)
            self.last_ns = self.clock()
            self.status = 'anchored_no_motion'
            return None
        for name, target in self.previous.items():
            if abs(target - state.positions[name]) > 0.1:
                raise ValueError(f'head tracking error exceeds 0.1 rad: {name}')
        roll_hold = self.target_limits.get(self.roll_joint, (self.anchor.positions[self.roll_joint],))[0]
        if abs(state.positions[self.roll_joint] - roll_hold) > 0.005:
            raise ValueError('held roll joint moved; restart required')
        self._check_progress(state, self.clock())
        preview = self.joint_mapper.preview(self.intent, self.anchor)
        step = self.target_rate * max(0, min((now - self.last_ns) / 1e9, 0.01))
        targets = {}
        self.last_mapping = {
            'requested_intent_rad': {'yaw_left': self.intent.yaw_left_rad,
                                     'pitch_up': self.intent.pitch_up_rad},
            'anchor_head_quaternion': self.anchor_head_quaternion,
            'anchor_positions_rad': dict(preview.anchor_positions),
            'schema_targets_rad': dict(preview.targets),
            'bounded_targets_rad': {},
        }
        limited = bool(preview.clamped_joints)
        for name, requested in preview.targets.items():
            if name in self.target_limits:
                low, high = self.target_limits[name]
                bounded = min(max(requested, low), high)
                limited = limited or bounded != requested
                requested = bounded
            self.last_mapping['bounded_targets_rad'][name] = requested
            current, previous = state.positions[name], self.previous[name]
            # Do not let commands run arbitrarily ahead of actual feedback.
            desired = min(max(requested, current - 0.01), current + 0.01)
            targets[name] = previous + min(max(desired - previous, -step), step)
            if abs(targets[name] - current) > 0.01 + 1e-12:
                raise ValueError(f'head feedback moved beyond target lead bound: {name}')
        targets[self.roll_joint] = roll_hold
        command = self.router.route(JointPositionIntent(targets, source='head-follow'), state, now_ns=now)
        validated = self.safety.validate(command, state, self.adapter.get_joint_schema(), now_ns=self.clock())
        self.before_send(validated, state, frame)
        if (self.stop_requested()
                or self.clock() - frame.received_monotonic_ns > 300_000_000
                or self.clock() - self.last_input_ns > 300_000_000):
            self.reset()
            return None
        if self.send_enabled:
            self.adapter.send_joint_command(validated, now_ns=self.clock())
        self.previous, self.last_ns = targets, self.clock()
        self.status = 'limited' if limited else 'tracking'
        return validated
