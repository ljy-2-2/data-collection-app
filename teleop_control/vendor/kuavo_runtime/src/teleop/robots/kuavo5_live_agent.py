"""User-started robot-local runner. Importing this module performs no hardware I/O."""

import hashlib
import json
import math
import re
from collections import deque
from pathlib import Path
from queue import Empty, Full, Queue
import signal
import sys
import threading
import time
import xmlrpc.client

from teleop.contracts.robot_state import RobotState
from teleop.control.kuavo5_live_controller import KuavoLiveController, NativeArmWorkSolver
from teleop.control.kuavo5_tracking_governor import KuavoTrackingGovernor
from teleop.retargeting.dual_arm import DualArmRetargeter
from teleop.retargeting.kuavo_target_progress import KuavoTargetProgress, FixedAnchorTargetProgress
from teleop.robots.kuavo5 import schema_from_urdf
from teleop.robots.kuavo5_arm_ros_transport import bind_ros
from teleop.robots.kuavo5_live_guard import COMMAND_TOPICS, SERVICE_OWNERS, FeedbackGuard, check_graph, MAX_ODOMETRY_FUTURE_SKEW_NS, RuntimeMotionPaused, BaseYawTransientFilter
from teleop.robots.kuavo5_live_protocol import (
    InputLease, StaleInputLease, DIAGNOSTIC_EVENTS, encode_robot_packet)
from teleop.robots.kuavo5_native_ik import (
    ARM_NAMES, KuavoNativeIKSolver, OFFICIAL_UPPER_BODY_IK_PARAMS)
from teleop.robots.kuavo5_native_rpc_backend import KuavoNativeRPCBackend
from teleop.robots.kuavo5_diagnostics import ComputeResponseRejected, SuccessTraceSampler, CollectionBuffer


URDF_SHA = 'fecef6f980806af7d2c4b1abe56d3fe1d0dbfbd69b3475303eb158bcc06c85dc'
IK_MODEL_SHA = '4d7a3819a4879602f8af56f1125212a27fa747c35a297cff98e7e45b504a44d1'
IK_BINARY_SHA = '211d4d39a8ec6e7f40610e3613431fc873bb4919bb547b2df158e6fe8cbfb793'
IK_BINARY = '/home/lab/kuavo-ros-control/devel/lib/motion_capture_ik/arms_ik_node'
# Acceptance policy informed by the vendor's initial 20 mm solve tolerance.
# The binary-search wrapper does not establish that its final q still meets
# that tolerance: independently check FK, limits and shape even on success.
LIVE_FK_POSITION_TOLERANCE_M = math.sqrt(3.) * .020
LIVE_FK_POSITION_AXIS_TOLERANCE_M = .020
LIVE_FK_ROTATION_TOLERANCE_DEG = math.degrees(.1)
# Project reference ceiling informed by the user's preferred frozen G2
# dual-arm profile: 0.5 rad/s at 100 Hz. This is not a v63 manufacturer speed
# certification. Each lower URDF joint limit and vendor WBC interpolation stay.
LIVE_MAX_VELOCITY_RAD_S = .5
LIVE_TRACKING_SOFT_LEAD_RAD = .15
# Preserve the prior nominal 0.1 s zero-to-cap ramp with the slower ceiling.
# This is planning smoothing, not a physical or vendor-certified acceleration
# cap; actual publication intervals and measured motor response can differ.
LIVE_PLANNING_ACCELERATION_RAD_S2 = 5.
LIVE_NUMERIC_SPEED_POLICY = 'bounded_1p0_10_v1'
LIVE_NUMERIC_ACCELERATION_POLICY = 'independent_bounded_10_v1'
LIVE_PER_ARM_MOTION_POLICY = 'per_arm_scale_speed_acceleration_v1'
LIVE_MOBILE_BASE_POLICY = 'owned_body_relative_v1'
# The existing recovery gate already permits at most 0.2 rad from both the
# last published reference and measured feedback. Reuse that bound before a
# mode3 candidate can enter the control session; this is not a new wider limit.
LIVE_IK_FALLBACK_CONSTRAINT_MODE = 3
LIVE_IK_FALLBACK_MAX_SEED_DELTA_RAD = .2
# The primary answer must meet the same continuity gate as a fallback.
LIVE_IK_PRIMARY_MAX_SEED_DELTA_RAD = .2
# Project command clearance, not a widened measured-feedback limit or a
# manufacturer-certified physical overshoot budget. FK checks the projected q.
LIVE_CANDIDATE_JOINT_MARGIN_RAD = math.radians(.5)



def tracking_soft_lead(target_recovery, speed_profile='steady'):
    if speed_profile == 'steady':
        return LIVE_TRACKING_SOFT_LEAD_RAD
    if speed_profile in ('responsive', 'responsive_1p0') and target_recovery == 'bounded_candidate':
        # 0.7^2/(2*5) + 0.7*0.01 = 0.056 rad reserve. Keep the 0.2
        # hard bound and leave 0.024 rad slack in the preserved 0.7 profile.
        # Explicit 1.0/10 candidate needs 0.060 rad reserve, leaving 0.020.
        return .12
    raise ValueError('responsive speed requires bounded_candidate; unknown speeds refused')


def motion_profile(target_recovery, speed_profile='steady', *, arm_velocity_rad_s=None,
                   arm_acceleration_rad_s2=None):
    """Validate coupled settings locally and remotely before hardware I/O."""
    lead = tracking_soft_lead(target_recovery, speed_profile)
    velocity = .7 if speed_profile == 'responsive' else LIVE_MAX_VELOCITY_RAD_S
    acceleration = LIVE_PLANNING_ACCELERATION_RAD_S2
    if speed_profile == 'responsive_1p0':
        # User-selected candidate, not a manufacturer speed certification.
        # Preserve responsive/steady values and unchanged .2 tracking bound.
        velocity, acceleration = 1., 10.
    if arm_velocity_rad_s is not None:
        if (target_recovery != 'bounded_candidate' or speed_profile != 'responsive'
                or type(arm_velocity_rad_s) not in (int, float)
                or not 0 < arm_velocity_rad_s <= 1. or not math.isfinite(arm_velocity_rad_s)):
            raise ValueError('numeric arm speed requires bounded_candidate/responsive and 0 < speed <= 1.0 rad/s')
        velocity = float(arm_velocity_rad_s)
        # Parameters are frozen before startup. Reuse the existing 1.0/10
        # profile for faster values; retain the original ramp at <= 0.7.
        if velocity > .7:
            acceleration = 10.
    if arm_acceleration_rad_s2 is not None:
        if (target_recovery != 'bounded_candidate' or speed_profile != 'responsive'
                or arm_velocity_rad_s is None
                or type(arm_acceleration_rad_s2) not in (int, float)
                or not math.isfinite(arm_acceleration_rad_s2)
                or not 0 < arm_acceleration_rad_s2 <= 10.):
            raise ValueError('explicit arm acceleration requires bounded_candidate/responsive, '
                             'numeric speed and 0 < acceleration <= 10 rad/s^2')
        acceleration = float(arm_acceleration_rad_s2)
    KuavoTrackingGovernor(lead_rad=lead).validate_profile(
        velocity=velocity, acceleration=acceleration, period=.01, hard_tracking=.2)
    return velocity, acceleration


def arm_motion_profile(target_recovery, speed_profile='steady', *, arm_motion_parameters=None,
                       arm_velocity_rad_s=None, arm_acceleration_rad_s2=None):
    """Validate each side before ROS imports, threads, or transport setup."""
    if arm_motion_parameters is None:
        velocity, acceleration = motion_profile(target_recovery, speed_profile,
            arm_velocity_rad_s=arm_velocity_rad_s, arm_acceleration_rad_s2=arm_acceleration_rad_s2)
        return velocity, acceleration, None
    if arm_velocity_rad_s is not None or arm_acceleration_rad_s2 is not None:
        raise ValueError('cannot mix scalar and per-arm motion settings')
    if not isinstance(arm_motion_parameters, dict) or set(arm_motion_parameters) != {'left', 'right'}:
        raise ValueError('exact left/right arm motion parameters required')
    checked = {}
    for side, values in arm_motion_parameters.items():
        if not isinstance(values, dict) or set(values) != {'scale', 'speed_rad_s', 'acceleration_rad_s2'}:
            raise ValueError('exact arm scale, speed and acceleration required')
        scale = values['scale']
        if type(scale) not in (int, float) or not math.isfinite(scale) or not .1 <= scale <= 2.:
            raise ValueError('arm position scale must remain within 0.1..2')
        velocity, acceleration = motion_profile(target_recovery, speed_profile,
            arm_velocity_rad_s=values['speed_rad_s'],
            arm_acceleration_rad_s2=values['acceleration_rad_s2'])
        checked[side] = dict(scale=float(scale), speed_rad_s=velocity,
                             acceleration_rad_s2=acceleration)
    return (max(p['speed_rad_s'] for p in checked.values()),
            max(p['acceleration_rad_s2'] for p in checked.values()), checked)


def target_progress_profile(target_recovery):
    """Startup-only choice; shared by dry-run, bundle and robot ready evidence."""
    if target_recovery == 'bounded_candidate':
        from teleop.robots.kuavo5_bounded_profile import profile_fields
        return profile_fields()
    if target_recovery == 'baseline':
        return dict(target_recovery='baseline',
                    baseline_profile='kuavo5_grip_followup_20260920',
                    target_progress_policy=KuavoTargetProgress.policy,
                    target_progress_failure_rebase=KuavoTargetProgress.failure_rebase_policy)
    if target_recovery == 'fixed_anchor':
        progress = FixedAnchorTargetProgress()
        return dict(target_recovery='fixed_anchor',
                    baseline_profile='kuavo5_fixed_anchor_fk_waypoint_20260921',
                    target_progress_policy=progress.policy,
                    target_progress_failure_rebase=progress.failure_rebase_policy,
                    target_position_step_m=progress.max_position_step_m,
                    target_orientation_step_rad=progress.max_orientation_step_rad)
    raise ValueError('unknown target recovery policy')


def make_live_controller(schema, transport, *, solver, diagnostic,
                         clock=time.monotonic_ns, target_recovery='baseline', candidate_model=None,
                         speed_profile='steady', return_config=None, arm_velocity_rad_s=None,
                         arm_acceleration_rad_s2=None, arm_motion_parameters=None):
    """Build the reviewed live profile without ROS discovery or device I/O."""
    target_progress_profile(target_recovery)  # Reject before creating any threads.
    velocity, acceleration, arms = arm_motion_profile(target_recovery, speed_profile,
        arm_motion_parameters=arm_motion_parameters, arm_velocity_rad_s=arm_velocity_rad_s,
        arm_acceleration_rad_s2=arm_acceleration_rad_s2)
    progress = FixedAnchorTargetProgress() if target_recovery == 'fixed_anchor' else KuavoTargetProgress()
    reference_sync = 'all_joints'
    if target_recovery == 'bounded_candidate':
        from teleop.robots.kuavo5_bounded_profile import BoundedModel
        if not isinstance(candidate_model, BoundedModel):
            raise ValueError('validated candidate model required before controller construction')
        progress = FixedAnchorTargetProgress(workspace_bounds=candidate_model.bounds,
                                            workspace_soft_margin_m=.05)
        reference_sync = 'per_arm'
    return KuavoLiveController(schema, transport,
        solve=NativeArmWorkSolver(solver, DualArmRetargeter(
            **({'scales': {side: p['scale'] for side, p in arms.items()}} if arms else {})),
                                  target_progress=progress),
        clock=clock, control_period_s=.01, diagnostic=diagnostic,
        max_velocity_rad_s=velocity,
        max_acceleration_rad_s2=acceleration,
        **({'joint_velocity_limits': {name: arms[side]['speed_rad_s']
                for side, names in (('left', ARM_NAMES[:7]), ('right', ARM_NAMES[7:])) for name in names},
            'joint_acceleration_limits': {name: arms[side]['acceleration_rad_s2']
                for side, names in (('left', ARM_NAMES[:7]), ('right', ARM_NAMES[7:])) for name in names}}
           if arms else {}),
        reuse_valid_solution_seed=True,
        reference_sync=reference_sync,
        tracking_governor=KuavoTrackingGovernor(
            lead_rad=tracking_soft_lead(target_recovery, speed_profile)),
        **({'return_config': return_config} if return_config is not None else {}))


class PriorityPacketQueue:
    """Bounded two-lane queue: control traffic always precedes diagnostics.

    Old diagnostic snapshots may be replaced under backpressure, with the loss
    counted on the next delivered diagnostic. Optional coalescing replaces
    only adjacent unsent challenges, retaining the new challenge's original
    issue time. Other control packets are never silently discarded; exhausting their independent lane raises Full.
    """

    def __init__(self, *, max_control, max_diagnostic, coalesce_challenges=False):
        if type(max_control) is not int or type(max_diagnostic) is not int \
                or max_control <= 0 or max_diagnostic <= 0:
            raise ValueError('positive packet queue capacities required')
        self._coalesce_challenges = coalesce_challenges
        self._coalesced_challenges_total = 0
        self._max_control = max_control
        self._max_diagnostic = max_diagnostic
        self._control = deque()
        self._diagnostic = deque()
        self._dropped_diagnostics = 0
        self._dropped_diagnostics_total = 0
        self._condition = threading.Condition()

    def put_nowait(self, packet):
        diagnostic = isinstance(packet, dict) and packet.get('event') in DIAGNOSTIC_EVENTS
        with self._condition:
            if diagnostic:
                if len(self._diagnostic) >= self._max_diagnostic:
                    self._diagnostic.popleft()
                    self._dropped_diagnostics += 1
                    self._dropped_diagnostics_total += 1
                self._diagnostic.append(packet)
            else:
                # Only replace the trailing request, never move it across a
                # ready/closed/status barrier or alter its lease issue time.
                if self._coalesce_challenges and isinstance(packet, dict) \
                        and packet.get('event') == 'challenge' and self._control \
                        and isinstance(self._control[-1], dict) \
                        and self._control[-1].get('event') == 'challenge':
                    self._control.pop()
                    self._coalesced_challenges_total += 1
                if len(self._control) >= self._max_control:
                    raise Full
                self._control.append(packet)
            self._condition.notify()

    def get(self, timeout=None):
        deadline = None if timeout is None else time.monotonic()+timeout
        with self._condition:
            while not self._control and not self._diagnostic:
                if deadline is None:
                    self._condition.wait()
                else:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise Empty
                    self._condition.wait(remaining)
            if self._control:
                return self._control.popleft()
            packet = self._diagnostic.popleft()
            if self._dropped_diagnostics:
                packet = dict(packet,
                    diagnostics_dropped_before=self._dropped_diagnostics)
                self._dropped_diagnostics = 0
            return packet

    def discard_diagnostics(self):
        with self._condition:
            count = len(self._diagnostic)
            self._diagnostic.clear()
            self._dropped_diagnostics += count
            self._dropped_diagnostics_total += count
            return count

    @property
    def coalesced_challenges_total(self):
        with self._condition:
            return self._coalesced_challenges_total

    @property
    def dropped_diagnostics_total(self):
        with self._condition:
            return self._dropped_diagnostics_total


def close_report_failed(report):
    return not isinstance(report, dict) or bool(report.get('fault')) \
        or report.get('execution_status') == 'fault' \
        or (report.get('publish_attempts', 0) > 0 and report.get('hold_acknowledged') is not True)


class DirectRosCompute:
    """No topic command: call official services, retaining their result side effects.

    FK/IK publishes /ik/result and IK mutates shared seed. This is an execution
    backend, never advertised or used as a read-only hardware probe.
    """
    def __init__(self, fk, ik, message_factory, *, check):
        self.fk, self.ik, self.message_factory, self.check = fk, ik, message_factory, check

    def call(self, operation, request):
        self.check()
        if operation == 'fk':
            response = self.fk(request['q'])
        elif operation == 'ik':
            command = self.message_factory()
            command.frame = request['frame']
            command.joint_angles_as_q0 = request['joint_angles_as_q0']
            command.use_custom_ik_param = request['use_custom_ik_param']
            if command.use_custom_ik_param:
                params = request.get('ik_param')
                names = ('major_optimality_tol', 'major_feasibility_tol',
                         'minor_feasibility_tol', 'major_iterations_limit',
                         'oritation_constraint_tol', 'pos_constraint_tol',
                         'pos_cost_weight', 'constraint_mode', 'elbow_cost_scale')
                if not isinstance(params, dict) or set(params) != set(names):
                    raise ValueError('exact audited custom IK parameters are required')
                if any(isinstance(params[name], bool)
                       or not isinstance(params[name], (int, float))
                       or not math.isfinite(params[name]) for name in names):
                    raise ValueError('custom IK parameters must be finite numeric')
                if type(params['constraint_mode']) is not int \
                        or params['constraint_mode'] not in (0, 3):
                    raise ValueError('custom IK constraint mode must be 0 or 3')
                expected = dict(OFFICIAL_UPPER_BODY_IK_PARAMS,
                                constraint_mode=params['constraint_mode'])
                if params != expected:
                    raise ValueError('custom IK parameters differ from audited v63 profile')
                for name in names:
                    setattr(command.ik_param, name, params[name])
            elif 'ik_param' in request:
                raise ValueError('unused custom IK parameters are forbidden')
            for side in ('left_pose', 'right_pose'):
                target, source = getattr(command.hand_poses, side), request['hand_poses'][side]
                for name in ('pos_xyz', 'quat_xyzw', 'elbow_pos_xyz', 'joint_angles'):
                    setattr(target, name, list(source[name]))
            response = self.ik(command)
        else:
            raise ValueError('only FK/IK service operations allowed')
        try:
            self.check()
            result = dict(success=response.success)
            if response.success:
                result['hand_poses'] = {name: dict(pos_xyz=list(getattr(response.hand_poses, name).pos_xyz),
                    quat_xyzw=list(getattr(response.hand_poses, name).quat_xyzw))
                    for name in ('left_pose', 'right_pose')}
            if operation == 'ik':
                result.update(q_arm=list(response.q_arm), q_torso=list(response.q_torso),
                              with_torso=response.with_torso, time_cost=response.time_cost)
            return result
        except Exception as exc:
            if operation == 'ik':
                # Preserve a received answer even if a guard or ROS conversion
                # rejects it; the exception still prevents control acceptance.
                fields = {name: getattr(response, name, None) for name in
                          ('success', 'with_torso', 'q_arm', 'q_torso', 'time_cost')}
                raise ComputeResponseRejected(type(exc).__name__+': '+str(exc), fields) from exc
            raise


class RuntimeGuard:
    """ROS callbacks copy state; no callback can enable, configure or publish."""
    def __init__(self, rospy, master, stop, *, base_mode='disabled'):
        if base_mode not in ('disabled', 'vendor_vr_x'):
            raise ValueError('unknown unified base mode')
        self.ros, self.master, self.stop = rospy, master, stop
        self.base_mode = base_mode
        self._odom_max_age_ns = 250_000_000
        self._odom_skew_budget_ns = 0
        if base_mode == 'vendor_vr_x':
            # Both channels consume the same chassis clock. Charge the same
            # uncertainty budget instead of accepting a sample on only one.
            from teleop.robots.kuavo5_base_ros import (
                ODOM_CLOCK_SKEW_BUDGET_NS, ODOM_FEEDBACK_MAX_AGE_NS)
            self._odom_max_age_ns = ODOM_FEEDBACK_MAX_AGE_NS
            self._odom_skew_budget_ns = ODOM_CLOCK_SKEW_BUDGET_NS
        self._base_check = None
        self._graph_stop = threading.Event()
        self._mobile_source_ns = None
        self._mobile_ros_ns = None
        self._mobile_quiet_since_ns = None
        self.lock = threading.RLock()
        self.error = None
        self.diagnostic = None
        self.feedback = None
        self.odom_ns = None
        self.odom_anchor = None
        self._motion_pause = None
        self._motion_pause_generation = 0
        self._motion_pause_evidence = None
        self._odom_quiet_since = None
        self._odom_received_ns = None
        self._odom_observation = None
        self._yaw_filter = BaseYawTransientFilter()
        self._odom_history = deque(maxlen=64)
        self.graph_ns = None
        self.arm_connections = None
        # The audited v63 HardwareNode publishes a logical loop counter in
        # sensor_time, and actual ROS publication time in header.stamp.
        self.feedback_guard = FeedbackGuard(timestamp_policy='vendor_cycle_counter')
        self.phase = 'startup_feedback'
        self.own_node = rospy.get_name()
        self.handles = []

    def fail(self, error, diagnostic=None):
        with self.lock:
            if self.error is None:
                self.error = str(error)
                self.diagnostic = diagnostic

    def set_base_check(self, callback):
        """Attach the same session's read-only base authority/freshness check.

        The callback must not call this arm guard, avoiding a lock cycle. It
        cannot create a publisher, switch modes or extend an input deadline.
        """
        if self.base_mode != 'vendor_vr_x' or not callable(callback):
            raise ValueError('unified base mode and callable base check required')
        with self.lock:
            if self._base_check is not None:
                raise ValueError('base authority check is already installed')
            self._base_check = callback

    def command(self, message, topic):
        caller = (getattr(message, '_connection_header', None) or {}).get('callerid')
        if self.base_mode == 'vendor_vr_x' and topic == '/cmd_vel' and caller == self.own_node:
            return
        if topic == '/cmd_vel' and caller == '/humanoid_joy_control_auto_gait_with_vel':
            try:
                values = (message.linear.x, message.linear.y, message.linear.z,
                          message.angular.x, message.angular.y, message.angular.z)
                if all(math.isfinite(value) and value == 0. for value in values):
                    return
            except (AttributeError, TypeError, ValueError):
                pass
        if topic != '/kuavo_arm_traj' or caller != self.own_node:
            self.fail('concurrent command observed: '+topic+' caller='+str(caller))

    def sensors(self, message):
        try:
            stamp = message.sensor_time.to_nsec()
            q, v = list(message.joint_data.joint_q), list(message.joint_data.joint_v)
            with self.lock:
                ros_now = self.ros.Time.now().to_nsec()
                now = time.monotonic_ns()  # Pair clocks after waiting for this lock.
                feedback_age = self.feedback_guard.observe(
                    q, v, stamp, ros_now, header_ns=message.header.stamp.to_nsec(),
                    monotonic_ns=now)
                self.feedback = RobotState(dict(zip(ARM_NAMES, q[4:18])),
                                           now-feedback_age)
        except Exception as exc:
            self.fail(exc, diagnostic=dict(event='guard_failure', source='sensors_data_raw',
                reason=str(exc), phase=self.phase, observed_monotonic_ns=time.monotonic_ns(),
                clock_timing=self.feedback_guard.last_timing))

    def sensor_timing_snapshot(self):
        with self.lock:
            return dict(self.feedback_guard.last_timing or {})

    def _latch_base_pause(self, evidence, decision_reason, now):
        # Caller holds self.lock. Diagnostics are bounded copies, never I/O.
        if self._motion_pause is not None:
            return
        self._motion_pause = 'base motion pause; await quiet base and new Grip release'
        self._motion_pause_generation += 1
        self._odom_quiet_since = None
        self._motion_pause_evidence = dict(evidence, event='guard_pause',
            reason=self._motion_pause, decision_reason=decision_reason,
            paused=True, quiet_duration_ms=0.,
            decision_monotonic_ns=now, yaw_filter=self._yaw_filter.snapshot(now),
            recent_observations=list(self._odom_history))

    def _check_yaw_deadline(self, now):
        if self.base_mode == 'vendor_vr_x':
            return
        if self._motion_pause is None:
            reason = self._yaw_filter.deadline_reason(now)
            if reason is not None and self._odom_observation is not None:
                self._latch_base_pause(self._odom_observation, reason, now)

    def odometry(self, message):
        from teleop.robots.kuavo5_diagnostics import scalar, vector
        evidence = dict(event='guard_failure', schema_version=1, source='/odom',
                        velocity_thresholds={'vx_m_s': .02, 'vy_m_s': .02, 'wz_rad_s': .02})
        try:
            now = time.monotonic_ns()
            stamp = message.header.stamp.to_nsec()
            ros_now = self.ros.Time.now().to_nsec()
            age = ros_now-stamp
            evidence.update(observed_monotonic_ns=now, source_ros_ns=stamp, source_age_ns=age)
            if self.base_mode == 'vendor_vr_x':
                age += self._odom_skew_budget_ns
                evidence.update(conservative_age_ns=age,
                    clock_skew_budget_ns=self._odom_skew_budget_ns,
                    max_feedback_age_ns=self._odom_max_age_ns)
                if not 0 <= age < self._odom_max_age_ns:
                    raise ValueError('odometry source stale/future with chassis clock budget')
            elif not -MAX_ODOMETRY_FUTURE_SKEW_NS <= age < 250_000_000:
                raise ValueError('odometry source stale/future')
            p, q, twist = message.pose.pose.position, message.pose.pose.orientation, message.twist.twist
            values = (p.x, p.y, p.z, q.x, q.y, q.z, q.w,
                      twist.linear.x, twist.linear.y, twist.angular.z)
            evidence.update(position_m=vector(values[:3]), orientation_xyzw=vector(values[3:7]),
                velocity=dict(zip(('vx_m_s', 'vy_m_s', 'wz_rad_s'), map(scalar, values[7:]))))
            if not all(math.isfinite(x) for x in values) or not .9 <= sum(x*x for x in values[3:7]) <= 1.1:
                raise ValueError('invalid base observation')
            if self.base_mode == 'vendor_vr_x':
                caller = (getattr(message, '_connection_header', None) or {}).get('callerid')
                if caller != '/ukf_odom' or message.header.frame_id != 'odom' \
                        or message.child_frame_id != 'base_footprint':
                    raise ValueError('unreviewed odometry source/frame')
                velocity = (twist.linear.x, twist.linear.y, twist.linear.z,
                            twist.angular.x, twist.angular.y, twist.angular.z)
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in velocity):
                    raise ValueError('invalid six-axis base velocity')
                self._mobile_odometry(values, velocity, evidence, now=now, stamp=stamp,
                                      ros_now=ros_now, age=age)
                return
            with self.lock:
                if self.odom_anchor is None:
                    self.odom_anchor = values[:7]
                distance = math.dist(values[:3], self.odom_anchor[:3])
                # The validity check permits small quaternion scale errors.
                # Angular distance needs unit quaternions; otherwise that scale
                # can both invent rotation and conceal a real envelope breach.
                # Keep the original unnormalised observations as evidence.
                quaternion_scale = math.sqrt(sum(x*x for x in values[3:7])
                    * sum(x*x for x in self.odom_anchor[3:7]))
                dot = abs(sum(a*b for a,b in zip(values[3:7], self.odom_anchor[3:7]))) / quaternion_scale
                angle = 2*math.acos(min(1., dot))
                evidence.update(anchor_pose=list(self.odom_anchor), distance_m=distance,
                                angle_rad=angle,
                                distance_limit_m=.02, angle_limit_rad=math.radians(2))
                if distance > .02 or angle > math.radians(2):
                    raise ValueError('base moved outside stationary envelope')
                # A slightly ahead chassis clock must never place the local
                # observation in the future or extend the stream-loss deadline.
                if self._odom_received_ns is not None \
                        and not 0 <= now-self._odom_received_ns < 250_000_000:
                    self._odom_quiet_since = None
                self._odom_received_ns = now
                self.odom_ns = now-max(0, age)
                exceeded = [name for name, value in
                    zip(('vx_m_s', 'vy_m_s', 'wz_rad_s'), values[7:]) if abs(value) > .02]
                evidence['exceeded_velocity_components'] = exceeded
                self._odom_history.append(dict(received_ns=now, source_ns=stamp,
                    source_age_ns=age, velocity=list(values[7:]),
                    distance_m=distance, angle_rad=angle))
                decision = self._yaw_filter.observe(now_ns=now, source_ns=stamp,
                    source_age_ns=age, vx=values[7], vy=values[8], wz=values[9],
                    distance_m=distance, angle_rad=angle)
                if self._motion_pause is None and decision is not None:
                    self._latch_base_pause(evidence, decision, now)
                elif self._motion_pause is not None and exceeded:
                    self._odom_quiet_since = None
                elif self._motion_pause is not None:
                    # Keep the original .02 motion cutoff. Recovery requires a
                    # stricter .01 quiet threshold for 500 ms; a single quiet
                    # sample cannot authorize a new arm-control segment.
                    if max(map(abs, values[7:])) > .01:
                        self._odom_quiet_since = None
                    elif self._odom_quiet_since is None:
                        self._odom_quiet_since = now
                    elif now-self._odom_quiet_since >= 500_000_000:
                        self._motion_pause = None
                self._odom_observation = dict(evidence, event='guard_trace',
                    paused=self._motion_pause is not None,
                    yaw_filter=self._yaw_filter.snapshot(now),
                    quiet_duration_ms=(0. if self._odom_quiet_since is None
                                       else (now-self._odom_quiet_since)/1e6))
        except Exception as exc:
            evidence.update(reason=str(exc)[:512], error_type=type(exc).__name__)
            self.fail(exc, evidence)

    def _mobile_odometry(self, values, velocity, evidence, *, now, stamp, ros_now, age):
        """Body-relative arm mode permits planar motion only, never waist motion."""
        with self.lock:
            if type(stamp) is not int or stamp <= 0 or (
                    self._mobile_source_ns is not None and stamp <= self._mobile_source_ns):
                raise ValueError('odometry source repeated or reordered')
            if self._mobile_ros_ns is not None and ros_now < self._mobile_ros_ns:
                raise ValueError('odometry ROS clock moved backwards')
            if self._odom_received_ns is not None and now <= self._odom_received_ns:
                raise ValueError('odometry receipt clock did not advance')
            if self.odom_anchor is None:
                self.odom_anchor = values[:7]
            anchor = self.odom_anchor
            # Gravity expressed in the chassis frame ignores world yaw. Using
            # the full quaternion difference here would reject every turn.
            def gravity(quaternion):
                norm = math.sqrt(sum(v*v for v in quaternion))
                x, y, z, w = (v/norm for v in quaternion)
                return (2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y))
            tilt_dot = sum(a*b for a, b in zip(gravity(values[3:7]), gravity(anchor[3:7])))
            tilt = math.acos(max(-1., min(1., tilt_dot)))
            height = abs(values[2]-anchor[2])
            evidence.update(base_motion_policy='body_relative_planar_v1',
                height_change_m=height, tilt_change_rad=tilt,
                height_limit_m=.02, tilt_limit_rad=math.radians(2),
                six_axis_velocity=list(velocity), anchor_pose=list(anchor))
            if height > .02 or tilt > math.radians(2):
                raise ValueError('base height or tilt outside planar-motion envelope')
            if max(abs(velocity[i]) for i in (2, 3, 4)) > .02:
                raise ValueError('non-planar base velocity outside envelope')
            if self.phase != 'runtime':
                distance = math.dist(values[:3], anchor[:3])
                scale = math.sqrt(sum(x*x for x in values[3:7])*sum(x*x for x in anchor[3:7]))
                dot = abs(sum(a*b for a, b in zip(values[3:7], anchor[3:7])))/scale
                angle = 2*math.acos(min(1., dot))
                if distance > .02 or angle > math.radians(2):
                    raise ValueError('base moved outside startup stationary envelope')
            consecutive = (self._odom_received_ns is not None
                and 0 < now-self._odom_received_ns <= 100_000_000
                and 0 < stamp-self._mobile_source_ns <= 100_000_000)
            if not consecutive or max(map(abs, velocity)) > .01:
                self._mobile_quiet_since_ns = None
            if max(map(abs, velocity)) <= .01 and self._mobile_quiet_since_ns is None:
                self._mobile_quiet_since_ns = stamp
            self._mobile_source_ns, self._mobile_ros_ns = stamp, ros_now
            self._odom_received_ns = now
            self.odom_ns = now-max(0, age)
            quiet_span = (0 if self._mobile_quiet_since_ns is None
                          else stamp-self._mobile_quiet_since_ns)
            self._odom_observation = dict(evidence, event='guard_trace', paused=False,
                                         quiet_duration_ms=quiet_span/1e6)

    def graph(self):
        graph_stop = self._graph_stop if self.base_mode == 'vendor_vr_x' else self.stop
        while not graph_stop.is_set():
            try:
                pub, sub, srv = self.master.getSystemState()
                pub, sub, srv = dict(pub), dict(sub), dict(srv)
                check_graph(pub, sub, srv, own_node=self.own_node,
                            own_base_control=self.base_mode == 'vendor_vr_x')
                arm_connections = len(set(sub.get('/kuavo_arm_traj', [])))
                with self.lock:
                    self.graph_ns = time.monotonic_ns()
                    self.arm_connections = arm_connections
            except Exception as exc:
                self.fail('ROS graph: '+str(exc))
                return
            graph_stop.wait(.2)

    def motion_pause_snapshot(self):
        with self.lock:
            return dict(active=self._motion_pause is not None,
                        reason=self._motion_pause,
                        generation=self._motion_pause_generation,
                        evidence=self._motion_pause_evidence,
                        observation=self._odom_observation)

    def check_preparation(self):
        """Pure math preparation only; cannot authorize control or supply feedback.

        Command ownership, graph freshness and cancellation remain guarded.
        Full observation checks start after compute construction, before FK or
        any publisher/control service construction. Never clear a latched error.
        """
        with self.lock:
            if self.stop.is_set():
                raise ValueError('input connection closed or operator exit')
            if self.error:
                raise ValueError(self.error)
            if self.graph_ns is None or not 0 <= time.monotonic_ns()-self.graph_ns < 1_000_000_000:
                raise ValueError('graph missing or stale during compute preparation')

    def check_observations(self):
        """Keep receiving input while paused; grants no publication permission."""
        with self.lock:
            now = time.monotonic_ns()
            if self.stop.is_set():
                raise ValueError('input connection closed or operator exit')
            if self.error:
                raise ValueError(self.error)
            for name, stamp, age in (('feedback', None if self.feedback is None else self.feedback.timestamp_ns, 250_000_000),
                                      ('odometry', self.odom_ns, self._odom_max_age_ns), ('graph', self.graph_ns, 1_000_000_000)):
                if stamp is None or not 0 <= now-stamp < age:
                    raise ValueError(name+' missing or stale')
            self._check_yaw_deadline(now)
            if self.base_mode == 'vendor_vr_x' and self.phase == 'runtime':
                if self._base_check is None:
                    raise ValueError('unified base authority check unavailable')
                self._base_check()
            return self.feedback

    def check(self):
        with self.lock:
            feedback = self.check_observations()
            if self._motion_pause is not None:
                raise RuntimeMotionPaused(self._motion_pause)
            if self.base_mode == 'vendor_vr_x' and self.phase != 'runtime':
                if self._mobile_quiet_since_ns is None or self._mobile_source_ns is None \
                        or self._mobile_source_ns-self._mobile_quiet_since_ns < 500_000_000:
                    raise RuntimeMotionPaused('startup requires 500ms fresh quiet base observations')
            if self.phase != 'runtime' and self._yaw_filter.pending_since_ns is not None:
                # The transient allowance applies only to an established arm
                # session. Startup FK/profile checks cannot treat pending
                # motion as proof that the base is quiet.
                raise RuntimeMotionPaused('startup requires quiet base; yaw observation pending')
            return feedback

    def check_exit(self):
        """Allow cleanup after EOF while still requiring fresh robot observations."""
        with self.lock:
            now = time.monotonic_ns()
            for name, stamp, age in (('feedback', None if self.feedback is None else self.feedback.timestamp_ns, 250_000_000),
                                      ('odometry', self.odom_ns, self._odom_max_age_ns), ('graph', self.graph_ns, 1_000_000_000)):
                if stamp is None or not 0 <= now-stamp < age:
                    raise ValueError(name+' missing or stale during vendor exit')
            return self.feedback

    def expected_arm_connections(self):
        self.check()
        with self.lock:
            if type(self.arm_connections) is not int or self.arm_connections < 3:
                raise ValueError('audited arm subscriber count missing')
            return self.arm_connections

    def close(self):
        self._graph_stop.set()
        for handle in self.handles:
            handle.unregister()

    def check_startup(self):
        self.check()
        with self.lock:
            self.feedback_guard.require_quiet()


def _identity_and_types(rospy, master, rosservice):
    version = rospy.get_param('/robot_version', None)
    if version is None:
        raise ValueError('厂家主控尚未就绪：缺少 /robot_version；SSH 登录成功不代表主控已启动。'
                         '请核对厂家启动与初始化流程，不要重复启动已有主控。')
    if str(version) != '63' or rospy.get_param('/use_sim_time', False):
        raise ValueError('requires physical v63 and wall-clock ROS time')
    if rospy.get_param('/use_external_mpc', False) is not True:
        raise ValueError('requires vendor external MPC route already running')
    ready = rospy.get_param('/hardware/is_ready', None)
    if type(ready) is not int or ready != 1:
        raise ValueError('hardware not ready: /hardware/is_ready must be integer 1; operator preparation required')
    # arms_ik_node ignores cmd.frame. These startup parameters instead change
    # which arm it returns and whether it rotates targets using a waist sensor.
    # Match its audited defaults; never relabel those variants as fixed-base IK.
    for name, default in (('/control_hand_side', 2), ('/mpc/mpcWaistDof', 0)):
        value = rospy.get_param(name, default)
        if type(value) is not int or value != default:
            raise ValueError('incompatible fixed-base dual-arm IK: '+name+' must be integer '+str(default))
    xml = Path(rospy.get_param('/urdfFile')).read_bytes()
    if hashlib.sha256(xml).hexdigest() != URDF_SHA:
        raise ValueError('full robot model hash mismatch')
    model = Path(rospy.get_param('/model_path')).read_bytes()
    if hashlib.sha256(model).hexdigest() != IK_MODEL_SHA:
        raise ValueError('official IK model hash mismatch')
    import rosnode
    # Always ask the master again: a registered parameter or a cached node URI
    # can survive the controller exiting. Missing nodes return None in rosnode.
    uri = rosnode.get_api_uri(master, '/arms_ik_node', skip_cache=True)
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError('厂家控制链尚未就绪：找不到 /arms_ik_node 节点地址；'
                         '请检查厂家主控是否仍运行、初始化是否完成。'
                         '网络服务在线不代表厂家主控已就绪；本次尚未配置控制模式或发送运动目标。')
    code, _, pid = xmlrpc.client.ServerProxy(uri).getPid(rospy.get_name())
    if code != 1 or type(pid) is not int or pid <= 0:
        raise ValueError('official IK process unavailable')
    argv = Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\0')
    if argv[0].decode() != IK_BINARY or hashlib.sha256(Path(IK_BINARY).read_bytes()).hexdigest() != IK_BINARY_SHA:
        raise ValueError('official IK executable differs from audited version')
    expected = {'/ik/fk_srv': 'kuavo_msgs/fkSrv',
        '/ik/two_arm_hand_pose_cmd_srv': 'kuavo_msgs/twoArmHandPoseCmdSrv',
        '/change_arm_ctrl_mode': 'kuavo_msgs/changeArmCtrlMode',
        '/wheel_arm_change_arm_ctrl_mode': 'kuavo_msgs/changeArmCtrlMode',
        '/enable_lb_arm_quick_mode': 'kuavo_msgs/changeLbQuickModeSrv',
        '/change_lb_mpc_obs_update_mode': 'kuavo_msgs/changeLbMpcObsUpdateModeSrv',
        '/enable_arm_traj_interpolator': 'std_srvs/SetBool'}
    for name, kind in expected.items():
        if rosservice.get_service_type(name) != kind:
            raise ValueError('ROS service type mismatch: '+name)
    return schema_from_urdf(xml.decode(), ARM_NAMES, require_velocity=True)


def run_robot(*, motion_enabled=False, configure_vendor_profile=False, reader=None, writer=None,
              target_recovery='baseline', speed_profile='steady', gripper_mode='off', arm_posture='baseline', gripper_config=None,
              return_home_config=None, standalone_return=False, arm_velocity_rad_s=None,
              arm_acceleration_rad_s2=None, arm_motion_parameters=None, settings_identity=None,
              base_mode='disabled'):
    if base_mode not in ('disabled', 'vendor_vr_x'):
        raise ValueError('unknown base mode')
    if base_mode != 'disabled' and (target_recovery != 'bounded_candidate' or standalone_return):
        raise ValueError('mobile base requires shared bounded arm input session')
    settings_fields = {}
    if settings_identity is not None:
        if (not isinstance(settings_identity, dict)
                or set(settings_identity) != {'revision', 'settings_sha256'}
                or type(settings_identity['revision']) is not int or settings_identity['revision'] < 0
                or not isinstance(settings_identity['settings_sha256'], str)
                or re.fullmatch('[a-f0-9]{64}', settings_identity['settings_sha256']) is None):
            raise ValueError('valid session settings revision and digest required before I/O')
        settings_fields = dict(session_settings_revision=settings_identity['revision'],
                               session_settings_sha256=settings_identity['settings_sha256'])
    from teleop.robots.kuavo5_posture import posture_profile, KuavoElbowPreferenceSolver, validate_geometry
    posture = posture_profile(arm_posture, target_recovery)
    from teleop.control.kuavo5_gripper_teleop import KuavoGripperTeleop, validate_gripper_mode
    validate_gripper_mode(gripper_mode)  # Refuse before ROS imports or arm configuration.
    gripper_settings = None
    gripper_digest = None
    if gripper_mode == 'damiao':
        from teleop.control.kuavo5_gripper_calibration import load_runtime_config
        import hashlib
        if gripper_config is None:
            raise ValueError('gripper not commissioned: measured configuration required before I/O')
        gripper_settings = load_runtime_config(gripper_config)
        gripper_digest = hashlib.sha256(json.dumps(gripper_settings.document,
            sort_keys=True, allow_nan=False).encode()).hexdigest()
    elif gripper_config is not None:
        raise ValueError('gripper configuration requires damiao mode')
    return_settings = None
    if return_home_config is not None:
        from teleop.robots.kuavo5_return_config import (
            load_return_config, return_config_digest, resolve_vendor_return, RETURN_BUTTONS)
        return_settings = load_return_config(return_home_config)
        if gripper_mode != 'damiao':
            raise ValueError('combined return-home requires commissioned damiao grippers')
    if type(standalone_return) is not bool or (standalone_return and return_settings is None):
        raise ValueError("standalone return requires combined return configuration")
    if motion_enabled is not True or configure_vendor_profile is not True:
        raise ValueError('robot execution requires explicit motion and vendor profile permissions')
    progress_profile = target_progress_profile(target_recovery)
    velocity, acceleration, arms = arm_motion_profile(target_recovery, speed_profile,
        arm_motion_parameters=arm_motion_parameters, arm_velocity_rad_s=arm_velocity_rad_s,
        arm_acceleration_rad_s2=arm_acceleration_rad_s2)
    import rospy
    import rosgraph
    import rosservice
    from kuavo_msgs.msg import sensorsData, twoArmHandPoseCmd
    from kuavo_msgs.srv import fkSrv, twoArmHandPoseCmdSrv
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry

    reader, writer = reader or sys.stdin, writer or sys.stdout
    stop, incoming = threading.Event(), Queue(maxsize=32)
    outgoing = PriorityPacketQueue(max_control=32, max_diagnostic=64,
                                   **({'coalesce_challenges': True} if target_recovery == 'bounded_candidate' else {}))
    trace_sampler = SuccessTraceSampler() if target_recovery == 'bounded_candidate' else None
    collection_buffer = CollectionBuffer()
    last_collection = 0
    transport = live = guard = numeric = None
    gripper_owner = None
    grippers = KuavoGripperTeleop() if gripper_mode == 'preview' else None
    candidate_model = None
    input_timing = None
    return_coordinator = None
    arm_return_config = None
    return_fields = {}
    base = None
    base_report = None
    base_fields = {} if base_mode == 'disabled' else dict(
        base_mode=base_mode, base_lock_button='left_x', base_reference_frame='body_relative',
        base_speed_profile='vendor_vr', base_motion_policy=LIVE_MOBILE_BASE_POLICY)
    exit_code = 1
    def read():
        try:
            while not stop.is_set():
                line = reader.readline(65538)
                if not line:
                    break
                if len(line) > 65536:
                    raise ValueError('input packet exceeds 64 KiB')
                incoming.put_nowait((json.loads(line), time.monotonic_ns()))
        except Exception as exc:
            if guard is not None:
                guard.fail('input stream: '+str(exc))
        finally:
            stop.set()
    def write():
        try:
            while True:
                packet = outgoing.get()
                if packet is None:
                    return
                if target_recovery == 'bounded_candidate' and packet.get('event') == 'challenge':
                    packet = dict(packet, robot_write_started_ns=time.monotonic_ns())
                writer.write(encode_robot_packet(packet,
                    compress_diagnostics=target_recovery == 'bounded_candidate'))
                writer.flush()
        except Exception as exc:
            if guard is not None:
                guard.fail('output writer failed: '+str(exc))
            stop.set()
    def emit(packet):
        # Capture every accepted publication before reducing large UI diagnostics.
        collection_buffer.accept(packet)
        if trace_sampler is not None:
            packet = trace_sampler.select(packet)
            if packet is None:
                return
            if packet.get('event') in ('ready', 'status'):
                packet = dict(packet, diagnostic_sampling=trace_sampler.status())
        if target_recovery == 'bounded_candidate' and packet.get('event') == 'control_trace':
            packet = dict(packet, input_transport_timing=input_timing)
        try:
            outgoing.put_nowait(packet)
        except Full:
            if guard is not None:
                label = 'diagnostic ' if packet.get('event') in DIAGNOSTIC_EVENTS else ''
                guard.fail(label+'output backpressure; input lease must expire')
            stop.set()
            raise ValueError('output backpressure; input lease must expire')
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    output_thread = threading.Thread(target=write, daemon=True)
    output_thread.start()
    threading.Thread(target=read, daemon=True).start()
    try:
        rospy.init_node('galaxis_kuavo_arms', anonymous=True, disable_signals=True, disable_rosout=True)
        master = rosgraph.Master(rospy.get_name())
        schema = _identity_and_types(rospy, master, rosservice)
        if return_settings is not None:
            # Read the actual wheel controller's initialization parameter. No
            # whole-body reset, AUTO_SWING request, waist/head/base command or
            # guessed zero pose is used for this arms-only return.
            arm_return_config = resolve_vendor_return(return_settings,
                rospy.get_param('/robot_init_state_param'), schema)
        guard = RuntimeGuard(rospy, master, stop, **({'base_mode': base_mode} if base_mode != 'disabled' else {}))
        pub, sub, srv = master.getSystemState()
        check_graph(dict(pub), dict(sub), dict(srv), own_node=guard.own_node,
                    **({'own_base_control': True} if base_mode != 'disabled' else {}))
        guard.graph_ns = time.monotonic_ns()  # Initial graph above was checked synchronously.
        for topic in COMMAND_TOPICS:
            message_type = Twist if topic == '/cmd_vel' else rospy.AnyMsg
            guard.handles.append(rospy.Subscriber(topic, message_type,
                lambda msg, name=topic: guard.command(msg, name), queue_size=1, tcp_nodelay=True))
        threading.Thread(target=guard.graph, name='kuavo-graph-guard', daemon=True).start()
        if target_recovery == 'bounded_candidate':
            from teleop.robots.kuavo5_bounded_profile import prepare_model
            from teleop.robots.kuavo5_numeric_process import RobotNumericProcess
            control_bytes = Path(rospy.get_param('/urdfFile')).read_bytes()
            candidate_model = prepare_model(Path(rospy.get_param('/model_path')).read_bytes(), control_bytes)
            guard.phase = 'compute_preparation'
            emit(dict(event='compute_preparing', reason='building private official math before feedback observation and control configuration'))
            numeric = RobotNumericProcess(candidate_model, control_bytes, check=guard.check_preparation,
                                          arm_posture=arm_posture)
        # Establish the full feedback window only after the heavy compiler and
        # mathematics constructor finish. No timing error is cleared/rebased.
        # Every control route must pass this unchanged quiet/freshness gate.
        guard.phase = 'startup_feedback'
        guard.handles.append(rospy.Subscriber('/sensors_data_raw', sensorsData, guard.sensors, queue_size=1, tcp_nodelay=True))
        guard.handles.append(rospy.Subscriber('/odom', Odometry, guard.odometry, queue_size=1, tcp_nodelay=True))
        if base_mode != 'disabled':
            from teleop.robots.kuavo5_mobile_session import JointBaseRuntime
            base = JointBaseRuntime()
            base.start(rospy, master)
            guard.set_base_check(base.check)
        if stop.wait(1.):
            raise ValueError('operator disconnected during preflight')
        if base is not None:
            base.preflight(stop)
        guard.check_startup()
        if numeric is not None:
            numeric.check = guard.check  # All FK/IK calls now require full observations.
            # Initialize FK and verify its shape before constructing a publisher
            # or changing the vendor control profile. No IK/result ROS side effect.
            feedback = guard.check_observations()
            poses = numeric.forward(tuple(feedback.positions[n] for n in ARM_NAMES))
            if set(poses) != {'left','right'} or any(p.frame_id != 'base_link' for p in poses.values()):
                raise ValueError('private numeric FK preflight failed')
            if arm_posture in ('elbow_preference', 'elbow_preference_v2'):
                geometry = validate_geometry(numeric.geometry(tuple(feedback.positions[n] for n in ARM_NAMES)))
                if any(math.dist(geometry['arms'][s]['hand'], poses[s].position_m) > 1e-9 for s in poses):
                    raise ValueError('private elbow geometry differs from official hand FK')
            guard.check_startup()
        if gripper_settings is not None:
            from teleop.robots.kuavo5_gripper_owner import OwnerGripperSession
            gripper_owner = OwnerGripperSession(
                gripper_settings.owner_directory, gripper_settings.profiles,
                gripper_settings.occupied_ids,
                lambda event, **fields: emit(dict(event='gripper_trace', kind=event, **fields)),
                motor_records=gripper_settings.document['motors'])
            gripper_owner.start()
            grippers = KuavoGripperTeleop(gripper_owner.driver)
            guard.check_startup()
            if return_settings is not None:
                # Validate the measured preset before configuring any vendor
                # control profile. This reads the owner's cached feedback only.
                initial_gripper_targets = grippers.capture_return_targets()
        guard.phase = 'control_configuration'
        transport = bind_ros(schema, motion_enabled=True, configure_vendor_profile=True,
                             max_velocity_rad_s=velocity)
        backend = numeric if numeric is not None else KuavoNativeRPCBackend(DirectRosCompute(
            rospy.ServiceProxy('/ik/fk_srv', fkSrv, persistent=False),
            rospy.ServiceProxy('/ik/two_arm_hand_pose_cmd_srv', twoArmHandPoseCmdSrv, persistent=False),
            twoArmHandPoseCmd, check=guard.check), frame_id='base_link')
        solver_class = KuavoElbowPreferenceSolver if arm_posture in ('elbow_preference', 'elbow_preference_v2') else KuavoNativeIKSolver
        solver = solver_class(
            schema, backend, frame_id='base_link', frame_mode=0,
            position_tolerance_m=LIVE_FK_POSITION_TOLERANCE_M,
            position_axis_tolerance_m=LIVE_FK_POSITION_AXIS_TOLERANCE_M,
            rotation_tolerance_deg=LIVE_FK_ROTATION_TOLERANCE_DEG,
            candidate_roundoff_tolerance_rad=1e-4,
            custom_ik_params=OFFICIAL_UPPER_BODY_IK_PARAMS,
            fallback_constraint_mode=LIVE_IK_FALLBACK_CONSTRAINT_MODE,
            fallback_max_seed_delta_rad=LIVE_IK_FALLBACK_MAX_SEED_DELTA_RAD,
            primary_max_seed_delta_rad=LIVE_IK_PRIMARY_MAX_SEED_DELTA_RAD,
            candidate_joint_margin_rad=LIVE_CANDIDATE_JOINT_MARGIN_RAD,
            diagnostic=emit, model_identity=numeric.identity if numeric is not None else {
                'urdf_sha256': URDF_SHA, 'ik_sha256': IK_MODEL_SHA, 'ik_binary_sha256': IK_BINARY_SHA},
            **({'cache_solve_geometry': True} if arm_posture == 'elbow_preference_v2' else {}))
        live = make_live_controller(schema, transport, solver=solver,
                                    diagnostic=emit, target_recovery=target_recovery,
                                    candidate_model=candidate_model, speed_profile=speed_profile,
                                    **({'arm_velocity_rad_s': arm_velocity_rad_s} if arm_velocity_rad_s is not None else {}),
                                    **({'arm_acceleration_rad_s2': arm_acceleration_rad_s2}
                                       if arm_acceleration_rad_s2 is not None else {}),
                                    **({'arm_motion_parameters': arms} if arms is not None else {}),
                                    **({'return_config': arm_return_config} if arm_return_config is not None else {}))
        # Construct the real solver/session before any control configuration.
        transport.prepare_profile(check=guard.check, exit_check=guard.check_exit,
                                  expected_connections=guard.expected_arm_connections())
        guard.check_startup()
        if return_settings is not None:
            from teleop.control.kuavo5_return_coordinator import KuavoReturnCoordinator
            if base is not None:
                from teleop.control.kuavo5_mobile_return import MobileReturnCoordinator
                return_coordinator = MobileReturnCoordinator(live, grippers,
                    initial_gripper_targets, base=base.session, emit=emit, timeout_s=return_settings['timeout_s'])
            else:
                return_coordinator = KuavoReturnCoordinator(live, grippers,
                    initial_gripper_targets, emit=emit, timeout_s=return_settings['timeout_s'])
            return_fields = dict(return_home_enabled=True,
                return_home_config_sha256=return_config_digest(return_settings),
                return_home_buttons=dict(RETURN_BUTTONS),
                return_home_arm_targets_rad=arm_return_config['targets'],
                return_home_gripper_targets_rad=initial_gripper_targets,
                return_home_gripper_capture=getattr(grippers, 'return_capture', {}),
                return_home_collision_avoidance=False)
        if standalone_return:
            from teleop.robots.kuavo5_live_protocol import ReturnCommandLease
            lease = ReturnCommandLease()
            return_fields["standalone_return"] = True
        else:
            lease = InputLease()
        request_deadline = time.monotonic_ns()+3_000_000_000
        if base is not None:
            base.activate()
        guard.phase = 'runtime'
        emit(dict(event='ready', robot_version='63', urdf_sha256=URDF_SHA,
                  **(base_fields or {'base_motion_policy': BaseYawTransientFilter.policy}),
                  sensor_timestamp_policy='vendor_cycle_counter',
                  sensor_feedback_age_basis='ros_header',
                  ik_model_sha256=numeric.identity['ik_sha256'] if numeric is not None else IK_MODEL_SHA,
                  ik_binary_sha256=IK_BINARY_SHA,
                  compute_identity=numeric.identity if numeric is not None else {'compute_kind':'official_ros_service'},
                  compute_model_limit_changes=list(candidate_model.changes) if candidate_model is not None else [],
                  profile_configured=True, physical_stop_verified=False,
                  gripper_mode=gripper_mode, gripper_can_send_enabled=gripper_mode == 'damiao',
                  gripper_config_sha256=gripper_digest,
                  max_velocity_rad_s=velocity, speed_profile=speed_profile,
                  planning_acceleration_rad_s2=acceleration,
                  tracking_soft_lead_rad=tracking_soft_lead(target_recovery, speed_profile),
                  tracking_stall_policy='limit_affected_arm_keep_grip_reverse_to_recover',
                  candidate_limit_failure_policy='discard_and_retry_fresh_input',
                  candidate_limit_retry_modes=[0, 3, 0],
                  ik_primary_constraint_mode=0,
                  ik_fallback_constraint_mode=LIVE_IK_FALLBACK_CONSTRAINT_MODE,
                  ik_fallback_max_seed_delta_rad=LIVE_IK_FALLBACK_MAX_SEED_DELTA_RAD,
                  ik_primary_max_seed_delta_rad=LIVE_IK_PRIMARY_MAX_SEED_DELTA_RAD,
                  candidate_joint_margin_deg=math.degrees(LIVE_CANDIDATE_JOINT_MARGIN_RAD),
                  ik_fallback_restores_mode0=True, **progress_profile, **posture, **return_fields,
                  **({'arm_motion_parameters': arms} if arms is not None else {}),
                  **settings_fields))
        last_challenge, last_report = 0, 0
        pause_generation, base_was_paused = 0, False
        while not stop.is_set() and not rospy.is_shutdown():
            started = time.monotonic_ns()
            feedback = guard.check_observations()
            accepted_this_tick = False
            while True:
                try:
                    packet, received_ns = incoming.get_nowait()
                except Empty:
                    break
                try:
                    frame = lease.accept(packet, now_ns=time.monotonic_ns())
                except StaleInputLease as exc:
                    # The packet has no authority and accept() has not changed
                    # the live input state.  A later fresh challenge can keep
                    # the same Grip session alive.
                    emit(dict(event='input_drop', reason=str(exc),
                              challenge_id=packet.get('challenge_id'),
                              input_transport_timing=lease.last_timing, robot_received_ns=received_ns))
                    continue
                if frame is not None:
                    accepted_this_tick = True
                    input_timing = dict(lease.last_timing, robot_received_ns=received_ns)
                    live.observe(frame)
                    if grippers is not None:
                        grippers.observe(frame)
                    if standalone_return:
                        if lease.request_now:
                            return_coordinator.request(frame,
                                allowed=not guard.motion_pause_snapshot()['active'])
                            if not return_coordinator.status()['active']:
                                raise ValueError('standalone return request refused')
                    elif return_coordinator is not None:
                        return_coordinator.observe(frame,
                            allowed=not guard.motion_pause_snapshot()['active'])
                    if base is not None:
                        base.observe(frame)
                        # Consume every leased button edge, including a press
                        # and release received in the same stream batch.
                        base_report = base.tick(inhibit=bool(return_coordinator and return_coordinator.status()['active']))
                        if base_report['error']:
                            raise ValueError('base execution fault: '+base_report['error'])
            if base is not None:
                base_report = base.tick(inhibit=bool(return_coordinator and return_coordinator.status()['active']))
                if base_report['error']:
                    raise ValueError('base execution fault: '+base_report['error'])
            pause = guard.motion_pause_snapshot()
            if pause['active']:
                # Continue input/diagnostics, but grant no IK or publication
                # permission. Even a release received during the pause cannot
                # authorize a segment after the base later becomes quiet.
                live.invalidate(pause['reason'])
                if pause['generation'] != pause_generation:
                    emit(dict(pause['evidence'], generation=pause['generation']))
                    pause_generation = pause['generation']
            elif base_was_paused:
                live.invalidate('base settled; release both Grips before resuming')
                emit(dict(event='guard_ready', generation=pause_generation,
                          observed_monotonic_ns=time.monotonic_ns(),
                          reason='base quiet for 500ms; new Grip release/press required'))
            base_was_paused = pause['active']
            if return_coordinator is not None:
                return_coordinator.update(allowed=not pause['active'], reason=pause['reason'],
                    **({'handoff_allowed': accepted_this_tick} if base is not None else {}))
            waiting_base = bool(base is not None and return_coordinator is not None
                and return_coordinator.status()['state'] == 'waiting_base_stop')
            if waiting_base:
                live.invalidate('waiting for base stop before return')
            # Same leased input, independent of arm Grip and pending IK.
            if grippers is not None:
                if gripper_owner is not None:
                    for side, error in gripper_owner.status()['errors'].items():
                        grippers.transport_failed(side, error)
                grippers.tick(allowed=not pause['active'] and not waiting_base,
                              reason='waiting for base stop before return' if waiting_base else pause['reason'])
            if return_coordinator is not None:
                return_coordinator.update(allowed=not pause['active'], reason=pause['reason'],
                    **({'handoff_allowed': accepted_this_tick} if base is not None else {}))
            result = live.tick(feedback)
            if return_coordinator is not None:
                return_coordinator.update(allowed=not pause['active'], reason=pause['reason'],
                    **({'handoff_allowed': accepted_this_tick} if base is not None else {}))
            if result.status == 'fault':
                raise ValueError('execution session fault: '+result.reason)
            if transport.status()['fault']:
                raise ValueError('arm transport fault: '+str(transport.status()))
            if standalone_return:
                state = return_coordinator.status()['state']
                if state == 'completed':
                    break  # Success still requires the ordinary shutdown reports.
                if state == 'cancelled':
                    raise ValueError('standalone return cancelled: '+return_coordinator.status()['reason'])
                if not lease.requested and time.monotonic_ns() >= request_deadline:
                    raise ValueError('standalone return request was not received in time')
            if started-last_challenge >= 10_000_000:
                issued_ns = time.monotonic_ns()
                challenge = lease.challenge(issued_ns)
                if target_recovery == 'bounded_candidate':
                    challenge['robot_issued_ns'] = issued_ns
                emit(challenge)
                last_challenge = started
            if started-last_collection >= 50_000_000:
                snapshot = grippers.status() if grippers is not None else {}
                samples = gripper_owner.driver.drain_feedback() if gripper_owner is not None else {}
                emit(collection_buffer.drain(now_ns=time.monotonic_ns(), samples=samples,
                    dropped_feedback=gripper_owner.driver.dropped_feedback if gripper_owner is not None else {},
                    motors=(snapshot or {}).get('motors', {})))
                last_collection = started
            if started-last_report >= 200_000_000:
                observation = guard.motion_pause_snapshot()['observation']
                if observation is not None:
                    emit(observation)
                gripper_snapshot = grippers.status() if grippers is not None else None
                if gripper_owner is not None:
                    gripper_snapshot['owner'] = gripper_owner.status()
                    gripper_snapshot['calibration'] = {
                        side: dict(commissioned=record['commissioned'],
                                   source=record['calibration']['source'])
                        for side, record in gripper_settings.document['motors'].items()}
                    # Preserve actual measured samples for collection. The UI
                    # status cadence must not manufacture 100 Hz feedback.
                    # Full samples travel in collection_batch, separately from
                    # the 5 Hz display snapshot below.
                emit(dict(event='status', status='paused_base' if pause['active'] else result.status,
                          reason=pause['reason'] if pause['active'] else result.reason,
                          max_step_rad=result.max_step_rad, transport=transport.status(),
                          sensor_timing=guard.sensor_timing_snapshot(),
                          tracking_governor=live.tracking_diagnostics,
                          grippers=gripper_snapshot,
                          **({'base': base_report} if base is not None else {}),
                          **({'return_home': return_coordinator.status()} if return_coordinator is not None else {})))
                last_report = started
            stop.wait(max(0., .01-(time.monotonic_ns()-started)/1e9))
        if guard.error:
            raise ValueError('robot guard/input failure: '+guard.error)
        exit_code = 0
    except Exception as exc:
        try:
            if guard is not None and guard.diagnostic is not None:
                emit(dict(guard.diagnostic, model_identity={'urdf_sha256': URDF_SHA,
                                                            'ik_sha256': IK_MODEL_SHA}))
            emit(dict(event='refused', reason=type(exc).__name__+': '+str(exc)))
        except ValueError:
            pass
    finally:
        stop.set()
        base_transport_report = None
        base_begin_error = None
        early_arm_report = None
        if base is not None:
            try:
                base.begin_close('unified input/session ended')
            except Exception as exc:
                base_begin_error = str(exc)
                exit_code = 1
            # Revoke arm work before any gripper/network cleanup may wait.
            # ROS observers remain alive until both channels finish cleanup.
            try:
                if live is not None:
                    early_arm_report = live.close()
                elif transport is not None:
                    early_arm_report = transport.close()
            except Exception as exc:
                early_arm_report = dict(closed=False, cleanup_error=str(exc),
                                        physical_stop_verified=False)
                exit_code = 1
        gripper_report = None
        gripper_transport_report = None
        try:
            if grippers is not None:
                gripper_report = grippers.close()
        except Exception as exc:
            gripper_report = {'closed': False, 'error': str(exc)}
            exit_code = 1
        finally:
            if gripper_owner is not None:
                try:
                    gripper_transport_report = gripper_owner.close()
                    if gripper_transport_report.get('closed') is not True or gripper_transport_report.get('errors'):
                        exit_code = 1
                except Exception as exc:
                    gripper_transport_report = {'closed': False, 'errors': [str(exc)]}
                    exit_code = 1
        try:
            report = early_arm_report if early_arm_report is not None else live.close() if live is not None else transport.close() if transport is not None else {
                'physical_stop_verified': False, 'publish_attempts': 0, 'profile_configured': False}
        except Exception as exc:
            report = dict(closed=False, cleanup_error=str(exc), physical_stop_verified=False)
            exit_code = 1
        if numeric is not None:
            try:
                numeric.close()
            except Exception as exc:
                report['compute_cleanup_error'] = str(exc)
                exit_code = 1
        # Do not abandon a late external-mode service: remain alive so its serial
        # worker can follow it with mode0. A failed hold is reported, never retried.
        if transport is not None:
            try:
                while transport.status()['mode_request_inflight'] is not None or transport.status()['mode_request_pending'] is not None:
                    try:
                        print('Awaiting arm mode reply; physical stop unverified. Use hardware emergency stop if needed.', file=sys.stderr, flush=True)
                    except (OSError, ValueError) as exc:
                        # A closed console must not abandon the serial mode
                        # worker before its late external/hold request resolves.
                        report['cleanup_console_error'] = str(exc)
                    time.sleep(.2)
                report.update(transport.status())
            except Exception as exc:
                report['cleanup_error'] = str(exc)
                exit_code = 1
        if close_report_failed(report):
            exit_code = 1
        if base is not None:
            try:
                base_transport_report = base.close()
            except Exception as exc:
                base_transport_report = dict(closed=False, errors=[str(exc)],
                    publish_attempts=0, stop_observed=False, physical_stop_verified=False)
            if base_begin_error:
                base_transport_report['stop_error'] = base_begin_error
            if (base_transport_report['errors'] or base_transport_report.get('stop_error')
                    or (base_transport_report['publish_attempts'] and not base_transport_report['stop_observed'])):
                exit_code = 1
        elif base_mode != 'disabled':
            base_transport_report = dict(closed=True,errors=[],publish_attempts=0,
                stop_observed=False,physical_stop_verified=False)
        if guard is not None:
            try:
                guard.close()
            except Exception as exc:
                report['guard_cleanup_error'] = str(exc)
                exit_code = 1
        try:
            outgoing.discard_diagnostics()
            report['stream_diagnostics_dropped'] = outgoing.dropped_diagnostics_total
            report['stream_challenges_coalesced'] = outgoing.coalesced_challenges_total
            if trace_sampler is not None:
                report['diagnostic_sampling'] = trace_sampler.status()
            outgoing.put_nowait(dict(event='closed', exit_code=exit_code, transport=report,
                                    grippers=gripper_report, gripper_transport=gripper_transport_report,
                                    **({'base_transport': base_transport_report} if base_mode != 'disabled' else {})))
            outgoing.put_nowait(None)
            output_thread.join(timeout=.5)
        except Full:
            pass
        rospy.signal_shutdown('user arm session finished')
    return exit_code
