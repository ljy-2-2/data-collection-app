"""G2-only feedback and normal-channel head servo, injected into G2Adapter."""
import math
import time
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from teleop.robots.g2 import G2JointControlRequestPreview, make_g2_joint_schema
from teleop.robots.g2_head import G2_HEAD_JOINTS, G2_HEAD_ROLL


# This is an operational containment limit for the currently tested G2, not
# the vendor's mechanical joint limit.  Twelve real-robot sessions repeatedly
# jumped from about -10.3..-10.8 degrees to about -169 degrees, including one
# startup sample before this process had sent any command.  Stay clear of that
# robot/controller fault sector until its yaw zero/multi-turn state is serviced.
G2_HEAD_OPERATIONAL_YAW_MIN_RAD = math.radians(-8.0)
G2_HEAD_OPERATIONAL_YAW_MAX_RAD = math.radians(45.0)


class G2HeadFeedbackReader:
    def __init__(self, robot, *, clock_ns=time.monotonic_ns):
        self.robot, self.clock = robot, clock_ns
        self.timestamp, self.changed_ns = None, None
        self.schema = make_g2_joint_schema()
        self._diagnostics = {}
        self._raw_rows = None
        self._previous_valid_feedback = None
        self._last_valid_feedback = None
        self.last_positions, self.last_received_ns = None, None

    def diagnostics(self, *, include_history=False):
        """Latest read attempt, including rejected raw values; no extra SDK call."""
        report = dict(self._diagnostics)
        if include_history:
            report['raw_joint_rows'] = repr(self._raw_rows)
            previous = self._previous_valid_feedback
            report['previous_valid_feedback'] = (
                dict(previous[0], raw_joint_rows=repr(previous[1])) if previous else None)
        return report

    def get_joint_states(self):
        start = self.clock()
        self.last_positions, self.last_received_ns = None, None
        self._previous_valid_feedback = self._last_valid_feedback
        self._raw_rows = None
        self._diagnostics = {'read_started_ns': start}
        try:
            payload = self.robot.get_joint_states()
        except Exception as error:
            self._diagnostics.update(read_error=str(error), read_ms=(self.clock()-start)/1e6)
            raise
        now = self.clock()
        self._diagnostics.update(read_ms=(now-start)/1e6, host_received_ns=now)
        if not isinstance(payload, Mapping):
            self._diagnostics['raw_payload'] = repr(payload)
            raise ValueError('invalid GDK feedback')
        timestamp = payload.get('timestamp')
        raw_rows = payload.get('states')
        # Copy the returned rows; do not retain references to an SDK/replay buffer.
        # Full-row serialization is deferred until a fault, not every 100 Hz log.
        self._raw_rows = [dict(row) if isinstance(row, Mapping) else repr(row)
                          for row in raw_rows] if isinstance(raw_rows, (list, tuple)) else repr(raw_rows)
        head_rows = [dict(row) for row in raw_rows
                     if isinstance(row, Mapping) and row.get('name') in G2_HEAD_JOINTS
                     ] if isinstance(raw_rows, (list, tuple)) else []
        self._diagnostics.update(
            source_timestamp=timestamp if type(timestamp) is int else repr(timestamp),
            raw_head_rows=repr(head_rows),
            source_progress=('first' if self.timestamp is None else
                             'unchanged' if timestamp == self.timestamp else 'changed'),
        )
        if type(timestamp) is not int or timestamp <= 0:
            raise ValueError('invalid GDK source timestamp')
        if self.timestamp is not None and timestamp < self.timestamp:
            raise ValueError('GDK source timestamp regressed')
        if timestamp != self.timestamp:
            self.changed_ns = now
        if not 0 <= now - self.changed_ns <= 200_000_000:
            raise ValueError('GDK source timestamp did not advance for 200 ms')
        self.timestamp = timestamp
        rows = payload.get('states')
        if not isinstance(rows, (list, tuple)):
            raise ValueError('invalid GDK states')
        for name in G2_HEAD_JOINTS:
            matches = [r for r in rows if isinstance(r, Mapping) and r.get('name') == name]
            if len(matches) != 1 or type(matches[0].get('error_code')) is not int or matches[0]['error_code'] != 0:
                raise ValueError(f'head feedback missing/duplicated or motor fault: {name}; '
                                 f'rows={len(matches)} error_codes={[r.get("error_code") for r in matches]}')
            position = matches[0].get('motor_position')
            joint = self.schema.get(name)
            if (type(position) not in (int, float) or not math.isfinite(position)
                    or not joint.position_min <= position <= joint.position_max):
                raise ValueError(f'head feedback outside joint limits: {name} position={position!r}')
        self.last_positions = {row['name']: row['motor_position'] for row in rows
                               if isinstance(row, Mapping) and row.get('name') in G2_HEAD_JOINTS}
        self.last_received_ns = now
        self._last_valid_feedback = (dict(self._diagnostics), self._raw_rows)
        return payload


class G2HeadServoSender:
    """Three positions required by GDK. No low-latency or planning calls."""
    def __init__(self, robot, *, clock_ns=time.monotonic_ns, target_rate_rad_s=0.05):
        if (type(target_rate_rad_s) not in (int, float)
                or not math.isfinite(target_rate_rad_s) or not 0 < target_rate_rad_s <= 0.3):
            raise ValueError('head target rate must be within (0, 0.3] rad/s')
        self.target_rate = target_rate_rad_s
        self.robot, self.clock = robot, clock_ns
        self.previous, self.last_ns = None, None
        self.last_sent_targets = None
        self.schema = make_g2_joint_schema()

    def reanchor(self, state):
        now = self.clock()
        if not 0 <= now - state.timestamp_ns <= 200_000_000:
            raise ValueError('head anchor feedback stale')
        positions = {name: state.positions[name] for name in G2_HEAD_JOINTS}
        for name, pos in positions.items():
            joint = self.schema.get(name)
            if not math.isfinite(pos) or not joint.position_min <= pos <= joint.position_max:
                raise ValueError('head anchor outside limits')
        self.previous, self.last_ns = positions, now

    def send(self, request):
        if not isinstance(request, G2JointControlRequestPreview) or self.previous is None:
            raise ValueError('head sender requires a validated request and fresh anchor')
        if (len(request.joint_names) != 3 or set(request.joint_names) != set(G2_HEAD_JOINTS)
                or len(request.joint_positions) != 3 or len(request.joint_velocities) != 3):
            raise ValueError('head servo requires exactly the three head joints')
        if (type(request.life_time_s) not in (float, int) or not math.isfinite(request.life_time_s)
                or not 0.02 <= request.life_time_s <= 0.1):
            raise ValueError('head servo request expired or invalid TTL')
        now = self.clock()
        if now < self.last_ns:
            raise ValueError('head sender clock regressed')
        step = self.target_rate * min((now - self.last_ns) / 1e9, 0.01)
        targets = dict(zip(request.joint_names, request.joint_positions))
        for name, pos, speed in zip(request.joint_names, request.joint_positions, request.joint_velocities):
            joint = self.schema.get(name)
            if (type(pos) not in (float, int) or not math.isfinite(pos)
                    or not joint.position_min <= pos <= joint.position_max
                    or type(speed) not in (float, int) or not math.isfinite(speed) or not 0 <= speed <= self.target_rate
                    or abs(pos - self.previous[name]) > step + 1e-12):
                raise ValueError('head servo limit or target-rate violation')
        if abs(targets[G2_HEAD_ROLL] - self.previous[G2_HEAD_ROLL]) > 1e-12:
            raise ValueError('roll follow is disabled')
        result = self.robot.move_head_joint_servo(
            [targets[name] for name in G2_HEAD_JOINTS], 0.02, enable_low_latency=False)
        if result != 0:
            raise RuntimeError('head servo SDK returned failure')
        if self.clock() - now > 100_000_000:
            raise RuntimeError('head servo call exceeded 100 ms; stop, do not retry')
        self.previous, self.last_ns = targets, now
        self.last_sent_targets = dict(targets)


def normal_head_limits(state):
    yaw, roll, pitch = G2_HEAD_JOINTS
    schema = make_g2_joint_schema()
    for name in G2_HEAD_JOINTS:
        joint, value = schema.get(name), state.positions[name]
        if not math.isfinite(value) or not joint.position_min <= value <= joint.position_max:
            raise ValueError(f'initial head feedback outside limits: {name}')
    if not (G2_HEAD_OPERATIONAL_YAW_MIN_RAD <= state.positions[yaw]
            <= G2_HEAD_OPERATIONAL_YAW_MAX_RAD):
        raise ValueError(
            'initial yaw outside operational yaw envelope '
            '[-8, +45] degrees; no automatic centering')
    joint = schema.get(pitch)
    return MappingProxyType({
        yaw: (G2_HEAD_OPERATIONAL_YAW_MIN_RAD,
              G2_HEAD_OPERATIONAL_YAW_MAX_RAD),
        pitch: (max(joint.position_min, state.positions[pitch]-math.radians(20)),
                min(joint.position_max, state.positions[pitch]+math.radians(20))),
        roll: (state.positions[roll], state.positions[roll]),
    })


class G2NormalHeadSender(G2HeadServoSender):
    """Final fixed-envelope and source-health checks for continuous follow."""
    def __init__(self, robot, *, reader, limits, roll_hold, clock_ns=time.monotonic_ns,
                 target_rate_rad_s=.3, guard=lambda: None, audit=lambda *a, **k: None):
        super().__init__(robot, clock_ns=clock_ns, target_rate_rad_s=target_rate_rad_s)
        self.reader, self.guard = reader, guard
        self.audit = audit
        self.limits, self.roll_hold = MappingProxyType(dict(limits)), roll_hold

    def _check_feedback(self):
        now = self.clock()
        if (self.reader.changed_ns is None or self.reader.last_received_ns is None
                or not 0 <= now-self.reader.changed_ns <= 200_000_000
                or not 0 <= now-self.reader.last_received_ns <= 200_000_000):
            raise ValueError('head source feedback expired before send')
        positions = self.reader.last_positions
        if positions is None: raise ValueError('head feedback invalid')
        for name, (low, high) in self.limits.items():
            margin = .005 if name == G2_HEAD_ROLL else .003
            if not low-margin <= positions[name] <= high+margin:
                raise ValueError(f'head feedback outside fixed bounds: {name}')
        return positions

    def reanchor(self, state):
        self._check_feedback()
        super().reanchor(state)
        self.previous[G2_HEAD_ROLL] = self.roll_hold

    def send(self, request):
        entered_ns = self.clock()
        self.guard()
        positions = self._check_feedback()
        for name, target in zip(request.joint_names, request.joint_positions):
            if name not in self.limits: raise ValueError('unknown head target')
            low, high = self.limits[name]
            if not low <= target <= high or abs(target-positions[name]) > .01+1e-12:
                raise ValueError(f'head target outside envelope or lead bound: {name}')
        self.audit('send_begin', targets_rad=dict(zip(request.joint_names, request.joint_positions)),
                   source_timestamp=self.reader.timestamp)
        self.guard()
        self._check_feedback()
        start = self.clock()
        remaining = request.life_time_s - (start-entered_ns)/1e9
        if remaining < .02:
            raise ValueError('head command expired during pre-send audit')
        request = replace(request, life_time_s=remaining)
        try:
            result = super().send(request)
        except Exception as error:
            self.audit('send_error', error=str(error), call_ms=(self.clock()-start)/1e6)
            raise
        self.audit('send_return', sdk_accepted=True, call_ms=(self.clock()-start)/1e6)
        return result
