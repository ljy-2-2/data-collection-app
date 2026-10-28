"""Fail-closed runtime checks; these do not certify physical standstill."""

import math


class RuntimeMotionPaused(ValueError):
    """Explicit pre-publication pause, without declaring a transport fault.

    Used for observed base motion or a command lease expiring before enqueue.
    Both revoke arm authority and require the existing release/rearm sequence.
    Malformed/stale observations and unknown control results remain hard faults.
    """


COMMAND_TOPICS = ('/kuavo_arm_traj', '/ik/two_arm_hand_pose_cmd',
    '/mm/two_arm_hand_pose_cmd', '/cmd_vel', '/cmd_vel_world', '/cmd_pose',
    '/cmd_pose_world', '/cmd_lb_torso_pose', '/cmd_torso_vel', '/cmd_torso_delta', '/lb_leg_traj')
KNOWN_PUBLISHERS = {
    '/kuavo_arm_traj': {'/arms_ik_node', '/humanoid_plan_arm_trajectory_node',
                        '/autostart_arm_trajectory_bezier_demo'},
    '/cmd_vel': {'/websocket_sdk_start_node', '/humanoid_joy_control_auto_gait_with_vel',
                 '/mobile_manipulator_joy_command_node', '/joy_customize_config'},
    '/cmd_vel_world': {'/mobile_manipulator_joy_command_node'},
    '/cmd_pose': {'/joy_node'},
    '/cmd_lb_torso_pose': {'/mobile_manipulator_joy_command_node'},
    '/lb_leg_traj': {'/nodelet_manager'},
}
KNOWN_ARM_SUBSCRIBERS = {'/mobile_manipulator_sqp_mpc_node', '/nodelet_manager',
                         '/autostart_arm_trajectory_bezier_demo'}
MAX_SENSOR_CLOCK_OFFSET_NS = 2_000_000_000
MAX_UNCORROBORATED_SENSOR_CLOCK_OFFSET_NS = 1_000_000_000
MAX_SENSOR_HEADER_AGE_NS = 50_000_000
# Project allowance for bounded skew between the body and chassis clocks.
# This is not an offset calibration: larger future timestamps still refuse.
MAX_ODOMETRY_FUTURE_SKEW_NS = 10_000_000


class BaseYawTransientFilter:
    """Bounded tolerance for small yaw-rate transients, never for translation.

    This is a project candidate, not a manufacturer standstill guarantee.
    The original .02 rate gate becomes a pending observation only for yaw:
    <= .08 rad/s, <= 5 mm and <= .5 degree from the fixed session anchor.
    At most 100 ms may pass without a <= .01 quiet yaw sample. Receipt AND
    source progress must remain <= 50 ms and samples <= 50 ms old. Thus
    missing, delayed or replayed observations cannot manufacture recovery.
    At the rate ceiling the 100 ms window represents < .5 degree; the
    independent 20 mm/2 degree hard envelope is still checked by the caller.
    No commands, Grip state, clutch anchors or IK state are changed here.
    """
    policy = 'small_yaw_transient_100ms_v1'
    confirm_ns = 100_000_000
    max_gap_ns = 50_000_000
    yaw_ceiling_rad_s = .08
    small_distance_m = .005
    small_angle_rad = math.radians(.5)

    def __init__(self):
        self.pending_since_ns = None
        self.previous_received_ns = None
        self.previous_source_ns = None
        self.last_reason = None

    def deadline_reason(self, now_ns):
        if self.pending_since_ns is not None and now_ns-self.pending_since_ns >= self.confirm_ns:
            return 'yaw observation not quiet within 100ms'
        return None

    def observe(self, *, now_ns, source_ns, source_age_ns, vx, vy, wz,
                distance_m, angle_rad):
        timing_ok = (self.previous_received_ns is not None
            and 0 < now_ns-self.previous_received_ns <= self.max_gap_ns
            and 0 < source_ns-self.previous_source_ns <= self.max_gap_ns
            and -MAX_ODOMETRY_FUTURE_SKEW_NS <= source_age_ns <= self.max_gap_ns)
        self.previous_received_ns, self.previous_source_ns = now_ns, source_ns
        reason = self.deadline_reason(now_ns)
        if abs(vx) > .02 or abs(vy) > .02:
            reason = 'translation velocity exceeds stationary threshold'
        elif abs(wz) > self.yaw_ceiling_rad_s:
            reason = 'yaw velocity exceeds transient ceiling'
        elif self.pending_since_ns is not None or abs(wz) > .02:
            if distance_m > self.small_distance_m or angle_rad > self.small_angle_rad:
                reason = 'yaw transient exceeds small-motion allowance'
            elif not timing_ok:
                reason = 'yaw transient lacks consecutive fresh observations'
        if reason is not None:
            self.last_reason = reason
            self.pending_since_ns = None
            return reason
        if abs(wz) <= .01:
            self.pending_since_ns = None
        elif abs(wz) > .02 and self.pending_since_ns is None:
            self.pending_since_ns = now_ns
        return None

    def snapshot(self, now_ns):
        return dict(policy=self.policy, pending=self.pending_since_ns is not None,
            pending_since_ns=self.pending_since_ns,
            pending_duration_ms=(0. if self.pending_since_ns is None
                                 else max(0, now_ns-self.pending_since_ns)/1e6),
            confirmation_ms=self.confirm_ns/1e6, max_observation_gap_ms=self.max_gap_ns/1e6,
            yaw_ceiling_rad_s=self.yaw_ceiling_rad_s,
            small_distance_m=self.small_distance_m, small_angle_rad=self.small_angle_rad,
            last_pause_reason=self.last_reason)


SERVICE_OWNERS = {'/ik/fk_srv': '/arms_ik_node',
    '/ik/two_arm_hand_pose_cmd_srv': '/arms_ik_node',
    '/wheel_arm_change_arm_ctrl_mode': '/mobile_manipulator_sqp_mpc_node',
    **{name: '/nodelet_manager' for name in ('/change_arm_ctrl_mode',
       '/enable_lb_arm_quick_mode', '/enable_arm_traj_interpolator', '/change_lb_mpc_obs_update_mode')}}


def check_graph(publishers, subscribers, services, *, own_node, own_base_control=False):
    if type(own_base_control) is not bool:
        raise ValueError('explicit own-base-control boolean required')
    for name, owner in SERVICE_OWNERS.items():
        if services.get(name) != [owner]:
            raise ValueError('service owner mismatch: '+name)
    if set(subscribers.get('/ik/result', [])) != {'/nodelet_manager'}:
        raise ValueError('unreviewed IK result consumer')
    arm_subscribers = set(subscribers.get('/kuavo_arm_traj', []))
    required = {'/mobile_manipulator_sqp_mpc_node', '/nodelet_manager'}
    allowed = KNOWN_ARM_SUBSCRIBERS | {own_node}
    if not required <= arm_subscribers <= allowed:
        raise ValueError('unreviewed arm trajectory subscriber: '+repr(subscribers.get('/kuavo_arm_traj')))
    for name in COMMAND_TOPICS:
        owned = name == '/kuavo_arm_traj' or (own_base_control and name == '/cmd_vel')
        allowed = KNOWN_PUBLISHERS.get(name, set()) | ({own_node} if owned else set())
        extra = set(publishers.get(name, []))-allowed
        if extra:
            raise ValueError('unreviewed command publisher: '+name+' '+repr(sorted(extra)))
    if publishers.get('/sensors_data_raw') != ['/nodelet_manager']:
        raise ValueError('sensor source mismatch')
    if publishers.get('/odom') != ['/ukf_odom']:
        raise ValueError('odometry source mismatch')


class FeedbackGuard:
    def __init__(self, *, timestamp_policy='legacy_offset'):
        if timestamp_policy not in ('legacy_offset', 'vendor_cycle_counter'):
            raise ValueError('unknown sensor timestamp policy')
        self.timestamp_policy = timestamp_policy
        self._stamp = 0
        self._ros_stamp = None
        self._monotonic_stamp = None
        self._progress_stamp = None
        self._header_stamp = None
        self._header_mode = None
        self._minimum_offset = None
        self._anchor = None
        self._unchanged_since = None
        self._last_qv = None
        self._first_stamp = None
        self._arm_span = 0.
        self._arm_speed = 0.
        self.last_timing = None

    def observe(self, q, v, source_ns, ros_now_ns, *, header_ns=None, monotonic_ns=None):
        # Evidence only: never use these fields to relax or reset a guard.
        self.last_timing = dict(
            source_ns=source_ns if type(source_ns) is int else None,
            ros_now_ns=ros_now_ns if type(ros_now_ns) is int else None,
            header_ns=header_ns if type(header_ns) is int else None,
            previous_source_ns=self._stamp, previous_ros_now_ns=self._ros_stamp,
            previous_header_ns=self._header_stamp,
            timestamp_policy=self.timestamp_policy,
            monotonic_ns=monotonic_ns, previous_monotonic_ns=self._monotonic_stamp)
        if len(q) != 20 or len(v) != 20 or any(
                type(x) not in (int, float) or not math.isfinite(x) for x in list(q)+list(v)):
            raise ValueError('invalid 20-joint measured feedback')
        if type(source_ns) is not int or type(ros_now_ns) is not int \
                or source_ns <= self._stamp:
            raise ValueError('sensor source timestamp repeated or moved backwards')
        offset = ros_now_ns-source_ns
        cycle_counter = self.timestamp_policy == 'vendor_cycle_counter'
        if cycle_counter:
            # Audited HardwareNode::sensorThreadFunc: sensor_time is start_time
            # incremented by dt per successful loop, NOT an acquisition clock.
            # header.stamp is Time::now() after readSensor. Never subtract the
            # cycle counter from wall time to infer packet age for this source.
            if header_ns is None:
                raise ValueError('vendor cycle counter requires a fresh ROS header')
            if type(monotonic_ns) is not int or monotonic_ns < 0 or (
                    self._monotonic_stamp is not None and monotonic_ns <= self._monotonic_stamp):
                raise ValueError('sensor monotonic receipt time missing or moved backwards')
        minimum_offset = offset if self._minimum_offset is None else min(self._minimum_offset, offset)
        self.last_timing.update(offset_ns=offset, minimum_offset_ns=minimum_offset,
            relative_age_ns=offset-minimum_offset,
            header_age_ns=ros_now_ns-header_ns if type(header_ns) is int else None,
            source_delta_ns=source_ns-self._stamp if self._ros_stamp is not None else None,
            ros_delta_ns=ros_now_ns-self._ros_stamp if self._ros_stamp is not None else None,
            header_delta_ns=header_ns-self._header_stamp
                if type(header_ns) is int and self._header_stamp is not None else None)
        uses_header = header_ns is not None
        if self._header_mode is not None and uses_header != self._header_mode:
            raise ValueError('sensor ROS header corroboration mode changed')
        offset_limit = MAX_SENSOR_CLOCK_OFFSET_NS
        if header_ns is None:
            offset_limit = MAX_UNCORROBORATED_SENSOR_CLOCK_OFFSET_NS
        elif type(header_ns) is not int or header_ns <= (self._header_stamp or 0) \
                or not 0 <= ros_now_ns-header_ns <= MAX_SENSOR_HEADER_AGE_NS:
            raise ValueError('sensor ROS header is stale, future or reordered')
        if not cycle_counter and abs(offset) > offset_limit:
            raise ValueError('sensor clock offset exceeds corroborated envelope: offset_ns='+
                             str(offset))
        if self._ros_stamp is not None:
            ros_delta = ros_now_ns-self._ros_stamp
            source_delta = source_ns-self._stamp
            if cycle_counter:
                mono_delta = monotonic_ns-self._monotonic_stamp
                self.last_timing.update(monotonic_delta_ns=mono_delta,
                    ros_monotonic_delta_error_ns=ros_delta-mono_delta)
                if abs(ros_delta-mono_delta) > MAX_SENSOR_HEADER_AGE_NS:
                    raise ValueError('sensor clock alignment changed against monotonic time')
            if ros_delta < 0 or abs(source_delta-ros_delta) > 50_000_000:
                raise ValueError('sensor clock alignment unstable or delayed')
            if header_ns is not None and abs((header_ns-self._header_stamp)-source_delta) > 50_000_000:
                raise ValueError('sensor source/header clocks diverged')
        minimum_offset = (offset if self._minimum_offset is None
                          else min(self._minimum_offset, offset))
        relative_age = offset-minimum_offset
        if not cycle_counter and not 0 <= relative_age <= 50_000_000:
            raise ValueError('sensor clock alignment unstable or delayed')
        feedback_age = ros_now_ns-header_ns if cycle_counter else relative_age
        self.last_timing.update(feedback_age_ns=feedback_age,
            feedback_age_basis='ros_header' if cycle_counter else 'minimum_source_offset')
        progress_stamp = monotonic_ns if cycle_counter else source_ns
        self._ros_stamp = ros_now_ns
        self._monotonic_stamp = monotonic_ns
        self._progress_stamp = progress_stamp
        self._header_stamp = header_ns
        self._header_mode = uses_header
        self._minimum_offset = minimum_offset
        self._stamp = source_ns
        if self._anchor is None:
            self._anchor = tuple(q)
            self._first_stamp = progress_stamp
        self._arm_span = max(self._arm_span, max(abs(q[i]-self._anchor[i]) for i in range(4,18)))
        self._arm_speed = max(self._arm_speed, max(map(abs, v[4:18])))
        for i in (0, 1, 2, 3, 18, 19):
            delta = q[i]-self._anchor[i]
            if abs(delta) > .02:
                raise ValueError('non-arm joint moved outside stationary envelope: '
                    f'index={i} delta_rad={delta:.6f} anchor_rad={self._anchor[i]:.6f} '
                    f'measured_rad={q[i]:.6f}')
        sample = tuple(q[4:18])+tuple(v[4:18])
        if self._last_qv is not None and max(abs(a-b) for a, b in zip(sample, self._last_qv)) < 1e-10:
            if progress_stamp-self._unchanged_since >= 500_000_000 and max(map(abs, v[4:18])) > .03:
                raise ValueError('arm positions and nonzero velocities unchanged; hardware freshness unverified')
        else:
            self._unchanged_since = progress_stamp
        self._last_qv = sample
        return feedback_age

    def require_quiet(self):
        if self._first_stamp is None or self._progress_stamp-self._first_stamp < 500_000_000:
            raise ValueError('startup feedback observation window is insufficient')
        if self._arm_span > .01 or self._arm_speed > .1:
            raise ValueError('arms are moving; refuse controller profile takeover')
