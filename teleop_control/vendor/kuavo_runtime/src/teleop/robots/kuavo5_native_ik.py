"""No-motion native IK protocol boundary; ROS/service transport is not included.

An explicitly injected backend must use the same audited fixed-torso FK/IK
frame and end-effector definition. Never instantiate the full control SDK here.
"""

import math
from numbers import Real
import time
from types import MappingProxyType
from typing import Mapping, Optional, Protocol

from teleop.contracts.command import JointCommand
from teleop.contracts.intent import CartesianPose, EndEffectorPoseIntent
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointSchema
from teleop.robots.kuavo5_diagnostics import failure_snapshot, success_snapshot, poses as diagnostic_poses


ARM_NAMES = tuple(f"zarm_{s}{i}_joint" for s in ("l", "r") for i in range(1, 8))
# Project numerical correction budget, NOT a widened URDF/feedback limit.
# The final candidate must be inside the original bounds and pass FK again.
MAX_CANDIDATE_ROUNDOFF_RAD = 1e-4
OFFICIAL_UPPER_BODY_IK_PARAMS = MappingProxyType({
    'major_optimality_tol': 1e-3,
    'major_feasibility_tol': 1e-3,
    'minor_feasibility_tol': 1e-3,
    'major_iterations_limit': 50.,
    'oritation_constraint_tol': 11e-3,
    'pos_constraint_tol': 1e-3,
    'pos_cost_weight': 10.,
    'constraint_mode': 0,
    'elbow_cost_scale': 0.,
})


class IKCandidateRejected(ValueError):
    """A rejected candidate; not by itself permission for automatic recovery."""


class IKCandidateLimitRejected(IKCandidateRejected):
    """Finite complete official answer outside the unchanged hardware limits.

    This vector is discarded, never clipped beyond the existing roundoff
    allowance or used as a seed. An execution owner may keep an established
    valid reference while seeking a new answer; input/feedback/transport errors
    retain their separate hard failure semantics.
    """


class IKTargetUnreachable(IKCandidateRejected):
    """The official service explicitly returned success=False for this target."""


class IKPoseResidualRejected(IKCandidateRejected):
    """Finite, limit-valid candidate misses the independent pose tolerance.

    The candidate is never accepted. A live owner with an existing output may
    freeze that output and seek a new answer while its input/feedback stay valid.
    """
    def __init__(self, reason, metrics=None):
        super().__init__(reason)
        self.pose_metrics = metrics


class IKFallbackRejected(IKPoseResidualRejected):
    """The bounded official fallback could not produce an acceptable candidate."""


class IKContinuityRejected(IKPoseResidualRejected):
    """Accurate pose, but an excessive change of arm configuration.

    No pose_metrics: a continuous alternative need only pass the existing pose
    tolerance; it need not be more accurate than a discontinuous answer.
    """


class NativeArmBackend(Protocol):
    def forward(self, q_arm: tuple[float, ...]) -> Mapping[str, CartesianPose]: ...
    def inverse(self, request: dict) -> Mapping: ...


def _radians(values, length):
    raw = tuple(values)
    if len(raw) != length or any(isinstance(v, bool) or not isinstance(v, Real)
                                  or not math.isfinite(v) for v in raw):
        raise ValueError(f"expected {length} finite numeric radians")
    return tuple(map(float, raw))


class KuavoNativeIKSolver:
    """Fixed-torso double-arm candidate computation, never physical sending.

    This implements IKSolver, but is not an official numerical algorithm itself.
    The backend is intentionally mandatory: importing/constructing it cannot
    auto-discover, install, launch or connect an official solver.
    """

    kind = "native"

    def __init__(self, schema: JointSchema, backend: NativeArmBackend, *,
                 frame_id: str, frame_mode: int, position_tolerance_m: float = 0.005,
                 position_axis_tolerance_m: Optional[float] = None,
                 rotation_tolerance_deg: float = 2.0, clock=time.monotonic_ns,
                 diagnostic=None, model_identity=None,
                 candidate_roundoff_tolerance_rad: float = 0.0,
                 custom_ik_params=None, fallback_constraint_mode=None,
                 fallback_max_seed_delta_rad: float = 0.2,
                 primary_max_seed_delta_rad=None,
                 candidate_joint_margin_rad: float = 0.0):
        if not isinstance(schema, JointSchema) or not set(ARM_NAMES) <= set(schema.names):
            raise ValueError("schema must contain the 14 audited v6 arm joints")
        if not isinstance(frame_id, str) or not frame_id.strip():
            raise ValueError("explicit audited FK/IK frame_id is required")
        if type(frame_mode) is not int or frame_mode not in range(6):
            raise ValueError("official frame mode must be an explicit integer in [0, 5]")
        for value in (position_tolerance_m, rotation_tolerance_deg):
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
                raise ValueError("pose tolerances must be finite and positive")
        if position_axis_tolerance_m is not None and (
                isinstance(position_axis_tolerance_m, bool)
                or not isinstance(position_axis_tolerance_m, Real)
                or not math.isfinite(position_axis_tolerance_m)
                or position_axis_tolerance_m <= 0):
            raise ValueError("axis position tolerance must be finite and positive")
        if not callable(getattr(backend, "forward", None)) or not callable(getattr(backend, "inverse", None)):
            raise ValueError("explicit forward/inverse backend is required")
        if (isinstance(candidate_roundoff_tolerance_rad, bool)
                or not isinstance(candidate_roundoff_tolerance_rad, Real)
                or not math.isfinite(candidate_roundoff_tolerance_rad)
                or not 0 <= candidate_roundoff_tolerance_rad <= MAX_CANDIDATE_ROUNDOFF_RAD):
            raise ValueError("candidate roundoff tolerance must be finite in [0, 1e-4] radians")
        if custom_ik_params is None:
            audited_params = None
        else:
            try:
                audited_params = dict(custom_ik_params)
            except (TypeError, ValueError) as exc:
                raise ValueError('custom IK params must be the audited upper-body profile') from exc
            if audited_params != dict(OFFICIAL_UPPER_BODY_IK_PARAMS):
                raise ValueError('custom IK params must exactly match the audited upper-body mode0 profile')
        if fallback_constraint_mode is not None:
            if type(fallback_constraint_mode) is not int or fallback_constraint_mode != 3:
                raise ValueError('only audited official constraint mode3 is an allowed fallback')
            if audited_params is None:
                raise ValueError('constraint fallback requires explicit audited mode0 parameters')
        if (isinstance(fallback_max_seed_delta_rad, bool)
                or not isinstance(fallback_max_seed_delta_rad, Real)
                or not math.isfinite(fallback_max_seed_delta_rad)
                or not 0 < fallback_max_seed_delta_rad <= .2):
            raise ValueError('fallback seed delta must be finite in (0, 0.2] radians')
        self.candidate_roundoff_tol = float(candidate_roundoff_tolerance_rad)
        self.schema, self.backend = schema, backend
        self.frame_id, self.frame_mode = frame_id, frame_mode
        self.position_tol, self.rotation_tol = position_tolerance_m, rotation_tolerance_deg
        self.position_axis_tol = position_axis_tolerance_m
        self.clock = clock
        if diagnostic is not None and not callable(diagnostic):
            raise ValueError('diagnostic must be an explicit nonblocking callback')
        self.diagnostic = diagnostic
        self.model_identity = dict(model_identity or {})
        self.custom_ik_params = audited_params
        self.fallback_constraint_mode = fallback_constraint_mode
        self.fallback_max_seed_delta_rad = float(fallback_max_seed_delta_rad)
        if primary_max_seed_delta_rad is not None and (
                isinstance(primary_max_seed_delta_rad, bool)
                or not isinstance(primary_max_seed_delta_rad, Real)
                or not math.isfinite(primary_max_seed_delta_rad)
                or not 0 < primary_max_seed_delta_rad <= .2):
            raise ValueError('primary seed delta must be finite in (0, 0.2] radians')
        if (isinstance(candidate_joint_margin_rad, bool)
                or not isinstance(candidate_joint_margin_rad, Real)
                or not math.isfinite(candidate_joint_margin_rad)
                or not 0 <= candidate_joint_margin_rad <= math.radians(.5)):
            raise ValueError('candidate joint margin must be finite in [0, 0.5 degrees]')
        for name in ARM_NAMES:
            joint = schema.get(name)
            if 2*candidate_joint_margin_rad >= joint.position_max-joint.position_min:
                raise ValueError('candidate margin leaves no usable joint interval')
        self.primary_max_seed_delta_rad = primary_max_seed_delta_rad
        self.candidate_joint_margin_rad = float(candidate_joint_margin_rad)
        # The vendor node keeps custom IK parameters in shared process state.
        # A transport failure cannot prove whether a request reached that node.
        self._constraint_mode_dirty = False
        self._validated_command = None
        self._validated_poses = None

    def validated_poses_for(self, command):
        """Return already checked FK for this exact most recent result, or None.

        This numerical snapshot grants no execution permission and does not
        extend the command lease. No service or extra FK call is performed.
        """
        if command is None or command is not self._validated_command:
            return None
        return self._validated_poses

    def reset(self, sides=("left", "right")):
        self._validated_command = self._validated_poses = None
        if not sides or len(set(sides)) != len(sides) or set(sides) - {"left", "right"}:
            raise ValueError("invalid reset sides")
        # History belongs to the control owner, not this serial compute worker.

    def _state_q(self, state, now_ns):
        if not isinstance(state, RobotState):
            raise ValueError("RobotState is required")
        if type(now_ns) is not int or now_ns < 0 or not 0 <= now_ns-state.timestamp_ns <= 250_000_000:
            raise ValueError("joint feedback is invalid, future or stale")
        try:
            q = _radians((state.positions[n] for n in ARM_NAMES), 14)
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("invalid arm feedback") from exc
        self._check_limits(q)
        return q

    def _check_limits(self, q, *, candidate=False):
        for name, value in zip(ARM_NAMES, q):
            joint = self.schema.get(name)
            if not joint.position_min <= value <= joint.position_max:
                error = IKCandidateRejected if candidate else ValueError
                raise error(f"{name} is outside URDF limits: value_rad={value!r} "
                            f"lower_rad={joint.position_min!r} upper_rad={joint.position_max!r}")

    def _forward(self, q):
        poses = dict(self.backend.forward(q))
        if set(poses) != {"left", "right"} or any(
                not isinstance(p, CartesianPose) or p.frame_id != self.frame_id for p in poses.values()):
            raise ValueError("FK must return both poses in the audited IK frame")
        return poses

    def forward_kinematics(self, state):
        return self._forward(self._state_q(state, self.clock()))

    def _request(self, targets, seed, constraint_mode=None):
        hand_poses = {}
        for side, offset in (("left", 0), ("right", 7)):
            pose = targets[side]
            hand_poses[side+"_pose"] = dict(pos_xyz=list(pose.position_m),
                quat_xyzw=list(pose.orientation_xyzw), elbow_pos_xyz=[0., 0., 0.],
                joint_angles=list(seed[offset:offset+7]))
        request = dict(hand_poses=hand_poses, joint_angles_as_q0=True,
                       use_custom_ik_param=self.custom_ik_params is not None,
                       frame=self.frame_mode)
        if self.custom_ik_params is not None:
            mode = self.custom_ik_params['constraint_mode'] if constraint_mode is None else constraint_mode
            if type(mode) is not int or mode not in (0, 3):
                raise ValueError('official constraint mode must be 0 or 3')
            request['ik_param'] = dict(self.custom_ik_params, constraint_mode=mode)
        elif constraint_mode not in (None, 0):
            raise ValueError('constraint mode requires explicit audited IK parameters')
        return request

    def _response_q(self, response):
        if isinstance(response, Mapping) and response.get("success") is False:
            raise IKTargetUnreachable("official IK could not solve this target")
        if not isinstance(response, Mapping) or response.get("success") is not True:
            raise ValueError("official IK failed or returned an invalid success flag")
        if response.get("with_torso") is not False or response.get("q_torso") not in ([], ()):
            raise ValueError("torso IK result requires a separate upper-body route; never discard it")
        try:
            q = _radians(response["q_arm"], 14)
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("invalid official q_arm") from exc
        corrected = []
        for name, value in zip(ARM_NAMES, q):
            joint = self.schema.get(name)
            bounded = min(joint.position_max, max(joint.position_min, value))
            if abs(bounded-value) > self.candidate_roundoff_tol:
                # The complete finite numerical vector is unusable, but it is
                # distinct from malformed data or out-of-limit real feedback.
                raise IKCandidateLimitRejected(
                    f"{name} is outside URDF limits: value_rad={value!r} "
                    f"lower_rad={joint.position_min!r} upper_rad={joint.position_max!r}")
            corrected.append(bounded)
        self._check_limits(corrected, candidate=True)
        return tuple(corrected)

    def _pose_errors(self, targets, actual):
        metrics = {}
        for side in ("left", "right"):
            requested, reached = targets[side], actual[side]
            distance = math.dist(requested.position_m, reached.position_m)
            axis_distance = max(abs(a-b) for a, b in
                                zip(requested.position_m, reached.position_m))
            dot = abs(sum(a*b for a, b in zip(requested.orientation_xyzw, reached.orientation_xyzw)))
            angle = math.degrees(2*math.acos(min(1., dot)))
            metrics[side] = dict(position_m=distance, axis_m=axis_distance,
                                 rotation_deg=angle)
        return metrics

    def _check_pose_error(self, targets, actual, metrics=None):
        metrics = self._pose_errors(targets, actual) if metrics is None else metrics
        for side in ("left", "right"):
            distance = metrics[side]['position_m']
            axis_distance = metrics[side]['axis_m']
            angle = metrics[side]['rotation_deg']
            if distance > self.position_tol \
                    or (self.position_axis_tol is not None
                        and axis_distance > self.position_axis_tol) \
                    or angle > self.rotation_tol:
                raise IKPoseResidualRejected(
                    f"{side} FK check failed: position_m={distance:.6f} "
                    f"axis_m={axis_distance:.6f} rotation_deg={angle:.3f}", metrics)
        return metrics

    @staticmethod
    def _max_position_error(metrics, active_sides):
        return max(metrics[side]['position_m'] for side in active_sides)

    @staticmethod
    def _active_seed_delta(q, seed, active_sides):
        indices = [index for side in active_sides
                   for index in range(0 if side == 'left' else 7,
                                      7 if side == 'left' else 14)]
        return max(abs(q[index]-seed[index]) for index in indices)

    @staticmethod
    def _halfway_targets(targets, seed_poses, active_sides):
        """One bounded continuation trial; original targets remain the FK gate."""
        halfway = dict(targets)
        for side in active_sides:
            origin, goal = seed_poses[side], targets[side]
            a, b = origin.orientation_xyzw, goal.orientation_xyzw
            sign = 1. if sum(x*y for x, y in zip(a, b)) >= 0 else -1.
            # Normalized quaternion midpoint equals shortest-arc SLERP at 1/2.
            halfway[side] = CartesianPose(goal.frame_id,
                tuple((x+y)/2 for x, y in zip(origin.position_m, goal.position_m)),
                tuple(x+sign*y for x, y in zip(a, b)))
        return halfway

    def solve(self, intent, state, *, sequence_id, now_ns, ttl_ms,
              diagnostic_context=None, seed_q=None):
        self._validated_command = self._validated_poses = None
        # Validate command metadata before invoking any backend.
        JointCommand({ARM_NAMES[0]: 0.}, "kuavo_native_ik_preview", sequence_id, now_ns, ttl_ms)
        if not isinstance(intent, EndEffectorPoseIntent):
            raise ValueError("EndEffectorPoseIntent is required")
        seed = request = response = actual = targets = None
        q = None
        corrections = []
        interior_projection = []
        stage = 'input_validation'
        attempt_records = []
        selection = dict(policy='mode0_then_bounded_mode3_then_mode0_restore',
                         fallback_enabled=self.fallback_constraint_mode == 3,
                         attempt_modes=[], chosen=None,
                         mode_state_dirty_before=self._constraint_mode_dirty)
        try:
            measured = self._state_q(state, now_ns)
            seed = measured if seed_q is None else _radians(seed_q, 14)
            self._check_limits(seed)
            if any(p.frame_id != self.frame_id for p in intent.targets.values()):
                raise ValueError("target frame does not match audited IK frame")
            stage = 'target_fk'
            # A numerical initial guess is not feedback. Inactive-hand targets
            # and freshness checks must still use the actual measured posture.
            targets = self._forward(measured) if len(intent.targets) < 2 else {}
            targets.update(intent.targets)
            active_sides = tuple(intent.targets)

            def compute(mode, attempt_seed, label, wire_targets=None):
                nonlocal request, response, actual, q, corrections, stage, interior_projection
                entry = dict(label=label, constraint_mode=mode, outcome='started')
                attempt_records.append(entry)
                selection['attempt_modes'].append(mode)
                stage_prefix = ('' if self.custom_ik_params is None
                                and label == 'mode0_primary' else label+'_')
                try:
                    request = self._request(targets if wire_targets is None else wire_targets,
                                            attempt_seed, constraint_mode=mode)
                    stage = stage_prefix+'official_ik'
                    inverse_started = self.clock()
                    if mode == 3:
                        # Set before transport: an exception cannot establish
                        # whether the persistent vendor parameter was changed.
                        self._constraint_mode_dirty = True
                    response = self.backend.inverse(request)
                    if mode == 0:
                        # A returned mode0 call proves the vendor handled an
                        # explicit restore even if its numerical answer fails.
                        self._constraint_mode_dirty = False
                    inverse_completed = self.clock()
                    stage = stage_prefix+'candidate_validation'
                    corrections, interior_projection = [], []
                    q = self._response_q(response)
                    corrections = [dict(joint=name, original_rad=raw, corrected_rad=value,
                                        delta_rad=value-raw)
                                   for name, raw, value in zip(ARM_NAMES, response['q_arm'], q)
                                   if raw != value]
                    # An engineering command margin, distinct from roundoff.
                    # First validate the raw vendor vector; then move only the
                    # active joints inside the unchanged hardware limits and
                    # independently check FK of the vector actually returned.
                    if self.candidate_joint_margin_rad:
                        projected = list(q)
                        for side in active_sides:
                            offset = 0 if side == 'left' else 7
                            for index in range(offset, offset+7):
                                joint = self.schema.get(ARM_NAMES[index])
                                value = min(joint.position_max-self.candidate_joint_margin_rad,
                                    max(joint.position_min+self.candidate_joint_margin_rad, q[index]))
                                if value != q[index]:
                                    interior_projection.append(dict(joint=ARM_NAMES[index],
                                        original_rad=q[index], corrected_rad=value,
                                        delta_rad=value-q[index],
                                        margin_rad=self.candidate_joint_margin_rad))
                                    projected[index] = value
                        q = tuple(projected)
                    stage = stage_prefix+'candidate_fk'
                    actual = self._forward(q)
                    stage = stage_prefix+'freshness'
                    completed = self.clock()
                    if completed < now_ns or completed-now_ns >= ttl_ms*1_000_000:
                        raise IKCandidateRejected("IK candidate expired during computation")
                    self._state_q(state, completed)
                    metrics = self._pose_errors(targets, actual)
                    entry.update(outcome='candidate',
                        max_position_error_m=self._max_position_error(metrics, active_sides),
                        max_active_seed_delta_rad=self._active_seed_delta(q, seed, active_sides))
                    return dict(label=label, mode=mode, q=q, corrections=corrections,
                        interior_projection=interior_projection,
                        request=request, response=response, actual=actual, metrics=metrics,
                        inverse_started=inverse_started, inverse_completed=inverse_completed,
                        completed=completed, entry=entry)
                except Exception as exc:
                    # A numeric limit failure happens before normal post-call
                    # freshness checks. It cannot conceal an expired source or
                    # stale/invalid real feedback just because its q is discarded.
                    if isinstance(exc, IKCandidateLimitRejected):
                        completed = self.clock()
                        try:
                            if completed < now_ns or completed-now_ns >= ttl_ms*1_000_000:
                                raise IKCandidateRejected('IK candidate expired during computation')
                            self._state_q(state, completed)
                        except Exception as hard_error:
                            entry.update(outcome='error', error_type=type(hard_error).__name__,
                                         reason=str(hard_error)[:256])
                            raise hard_error from exc
                    entry.update(outcome='error', error_type=type(exc).__name__,
                                 reason=str(exc)[:256])
                    raise

            def qualifies(candidate, primary_metrics):
                nonlocal stage
                stage = candidate['label']+'_continuity_check'
                delta = self._active_seed_delta(candidate['q'], seed, active_sides)
                if delta > self.fallback_max_seed_delta_rad:
                    raise IKFallbackRejected(
                        'fallback continuity exceeds configured seed delta '
                        f'{self.fallback_max_seed_delta_rad:.9f} rad: {delta:.9f}')
                stage = candidate['label']+'_pose_check'
                self._check_pose_error(targets, candidate['actual'], candidate['metrics'])
                # The primary is already unusable: it failed pose, continuity,
                # or numerical solving. A candidate satisfying every unchanged
                # pose bound must not lose merely because its position norm is
                # slightly larger than a primary that violated an axis or
                # orientation bound. Rank only fully eligible alternatives.
                candidate['entry']['outcome'] = 'eligible'
                return candidate

            def joint_continuation(primary):
                nonlocal stage, q, actual, request, response, corrections, interior_projection
                # A raw/pose-valid official answer may change configuration too
                # far. Try one short point on its joint-space segment, after
                # all numerical retries restore mode0. This is FK only: the
                # original hand targets and source lease still decide validity.
                label = 'mode0_joint_continuation'
                entry = dict(label=label, constraint_mode=0, outcome='started',
                             computation='forward_kinematics_only')
                attempt_records.append(entry)
                cap = min(.1, self.fallback_max_seed_delta_rad,
                          self.primary_max_seed_delta_rad)
                delta = self._active_seed_delta(primary['q'], seed, active_sides)
                fraction = min(1., cap*(1.-1e-12)/delta)
                q = list(measured)
                projections = []
                for side in active_sides:
                    offset = 0 if side == 'left' else 7
                    for index in range(offset, offset+7):
                        value = seed[index]+fraction*(primary['q'][index]-seed[index])
                        joint = self.schema.get(ARM_NAMES[index])
                        bounded = min(joint.position_max-self.candidate_joint_margin_rad,
                            max(joint.position_min+self.candidate_joint_margin_rad, value))
                        if value != bounded:
                            projections.append(dict(joint=ARM_NAMES[index],
                                original_rad=value, corrected_rad=bounded,
                                delta_rad=bounded-value,
                                margin_rad=self.candidate_joint_margin_rad))
                        q[index] = bounded
                q = tuple(q)
                actual = None
                request, response = primary['request'], primary['response']
                corrections = primary['corrections']
                interior_projection = projections
                selection['joint_continuation'] = dict(fk_only=True,
                    fraction=fraction, max_joint_step_rad=cap,
                    primary_candidate_rad=list(primary['q']),
                    primary_interior_projection=primary['interior_projection'],
                    inactive_source='measured_feedback')
                try:
                    stage = label+'_candidate_validation'
                    self._check_limits(q, candidate=True)
                    if self._active_seed_delta(q, seed, active_sides) > cap:
                        raise IKFallbackRejected('joint continuation exceeds bounded step after margin')
                    stage = label+'_candidate_fk'
                    actual = self._forward(q)
                    completed = self.clock()
                    stage = label+'_freshness'
                    if completed < now_ns or completed-now_ns >= ttl_ms*1_000_000:
                        raise IKCandidateRejected('IK candidate expired during joint continuation')
                    self._state_q(state, completed)
                    metrics = self._pose_errors(targets, actual)
                    entry.update(outcome='candidate',
                        max_position_error_m=self._max_position_error(metrics, active_sides),
                        max_active_seed_delta_rad=self._active_seed_delta(q, seed, active_sides))
                    candidate = dict(primary, label=label, q=q, actual=actual,
                        metrics=metrics, interior_projection=projections,
                        completed=completed, entry=entry)
                    return qualifies(candidate, None)
                except Exception as exc:
                    entry.update(outcome='rejected', error_type=type(exc).__name__,
                                 reason=str(exc)[:256])
                    raise

            primary_error = None
            primary_metrics = None
            try:
                primary = compute(0, seed, 'mode0_primary')
                primary_metrics = primary['metrics']
                stage = ('pose_check' if self.custom_ik_params is None
                         else 'mode0_primary_pose_check')
                self._check_pose_error(targets, primary['actual'], primary_metrics)
                if (self.primary_max_seed_delta_rad is not None
                        and self._active_seed_delta(primary['q'], seed, active_sides)
                        > self.primary_max_seed_delta_rad):
                    primary['entry'].update(outcome='rejected',
                                            error_type='IKContinuityRejected')
                    raise IKContinuityRejected(
                        'primary continuity exceeds configured seed delta '
                        f'{self.primary_max_seed_delta_rad:.9f} rad: '
                        f'{self._active_seed_delta(primary["q"], seed, active_sides):.9f}')
                primary['entry']['outcome'] = 'eligible'
                chosen = primary
                selection['chosen'] = 'mode0_primary'
            except (IKTargetUnreachable, IKPoseResidualRejected, IKCandidateLimitRejected) as exc:
                primary_error = exc
                primary_metrics = getattr(exc, 'pose_metrics', None)
                if self.fallback_constraint_mode != 3:
                    raise
                mode3 = restore = None
                mode3_error = restore_error = None
                try:
                    mode3 = compute(3, seed, 'mode3_fallback')
                except Exception as exc2:
                    mode3_error = exc2
                restore_seed = (mode3['q'] if mode3 is not None and
                    self._active_seed_delta(mode3['q'], seed, active_sides)
                    <= self.fallback_max_seed_delta_rad else seed)
                restore_targets, restore_label, continuation_error = None, 'mode0_restore', None
                if isinstance(primary_error, IKContinuityRejected) and (
                        mode3_error is None or isinstance(mode3_error,
                            (IKTargetUnreachable, IKPoseResidualRejected))):
                    mode3_eligible = False
                    if mode3 is not None:
                        try:
                            qualifies(mode3, None)
                            mode3_eligible = True
                        except IKPoseResidualRejected:
                            pass
                    if not mode3_eligible:
                        try:
                            restore_targets = self._halfway_targets(
                                targets, self._forward(seed), active_sides)
                            restore_seed, restore_label = seed, 'mode0_continuation'
                            selection['continuation_fraction'] = .5
                        except Exception as exc2:
                            # Even failure while building the intermediate
                            # target must not skip restoring shared mode0 state.
                            continuation_error = exc2
                try:
                    restore = compute(0, restore_seed, restore_label, restore_targets)
                except Exception as exc2:
                    restore_error = exc2
                # A malformed/transport failure remains hard. The extra
                # mode0 call above is still required to restore the vendor's
                # persistent shared parameter state after every mode3 request.
                for error in (mode3_error, restore_error):
                    if error is not None and not isinstance(error,
                            (IKTargetUnreachable, IKPoseResidualRejected, IKCandidateLimitRejected)):
                        raise error
                if continuation_error is not None:
                    raise continuation_error
                eligible = []
                rejection_reasons = []
                for candidate in (mode3, restore):
                    if candidate is None:
                        continue
                    try:
                        eligible.append(qualifies(candidate, primary_metrics))
                    except IKPoseResidualRejected as exc2:
                        candidate['entry'].update(outcome='rejected',
                            error_type=type(exc2).__name__, reason=str(exc2)[:256])
                        rejection_reasons.append(str(exc2))
                if not eligible:
                    # A complete finite but out-of-limit answer is discarded,
                    # not a failed mode restoration. Another fully validated
                    # answer may still be used after the explicit mode0 call.
                    # All true hard failures were checked above first.
                    for error in (primary_error, mode3_error, restore_error):
                        if isinstance(error, IKCandidateLimitRejected):
                            raise error
                if not eligible and isinstance(primary_error, IKContinuityRejected):
                    try:
                        eligible.append(joint_continuation(primary))
                    except IKPoseResidualRejected as exc2:
                        rejection_reasons.append(str(exc2))
                if not eligible:
                    if rejection_reasons:
                        raise IKFallbackRejected(rejection_reasons[0], primary_metrics)
                    raise IKFallbackRejected(
                        'official mode3 fallback and mode0 restore had no valid candidate',
                        primary_metrics) from primary_error
                chosen = min(eligible, key=lambda candidate: (
                    self._max_position_error(candidate['metrics'], active_sides),
                    self._active_seed_delta(candidate['q'], seed, active_sides),
                    0 if candidate['mode'] == 0 else 1))
                selection['chosen'] = chosen['label']
                selection['primary_error_type'] = type(primary_error).__name__
                selection['primary_reason'] = str(primary_error)[:256]

            q, corrections = chosen['q'], chosen['corrections']
            interior_projection = chosen['interior_projection']
            request, response, actual = chosen['request'], chosen['response'], chosen['actual']
            inverse_started, inverse_completed = chosen['inverse_started'], chosen['inverse_completed']
            selection['attempts'] = attempt_records
            selection['mode_state_dirty_after'] = self._constraint_mode_dirty
            stage = 'freshness'
            completed = self.clock()
            if completed < now_ns or completed-now_ns >= ttl_ms*1_000_000:
                raise IKCandidateRejected("IK candidate expired during computation")
            self._state_q(state, completed)
            command = JointCommand(dict(zip(ARM_NAMES, q)), intent.source, sequence_id, now_ns, ttl_ms)
        except Exception as exc:
            selection['attempts'] = attempt_records
            selection['mode_state_dirty_after'] = self._constraint_mode_dirty
            if self.diagnostic is not None:
                try:
                    packet = failure_snapshot(JointSchema(tuple(self.schema.get(n) for n in ARM_NAMES)),
                        sequence_id=sequence_id, now_ns=now_ns, completed_ns=self.clock(), state=state,
                        context=diagnostic_context, identity=self.model_identity, seed=seed,
                        request=request, response=response, actual=actual, stage=stage, error=exc,
                        position_tol=self.position_tol,
                        position_axis_tol=self.position_axis_tol,
                        rotation_tol=self.rotation_tol)
                    packet.update(candidate_rad=None if q is None else list(q),
                                  candidate_limit_normalization=corrections,
                                  candidate_interior_projection=interior_projection,
                                  constraint_selection=selection)
                    packet['validation_targets'] = diagnostic_poses(targets)
                    self.diagnostic(packet)
                except Exception as diagnostic_error:
                    raise RuntimeError('IK diagnostic delivery failed after '+type(exc).__name__+
                                       ': '+str(exc)[:256]) from diagnostic_error
            raise
        if self.diagnostic is not None:
            try:
                packet = success_snapshot(ARM_NAMES,
                    sequence_id=sequence_id, now_ns=now_ns, completed_ns=completed, state=state,
                    context=diagnostic_context, identity=self.model_identity, seed=seed,
                    request=request, response=response, actual=actual,
                    inverse_started_ns=inverse_started, inverse_completed_ns=inverse_completed)
                packet.update(candidate_rad=list(q), candidate_limit_normalization=corrections,
                              candidate_interior_projection=interior_projection)
                packet['constraint_selection'] = selection
                packet['validation_targets'] = diagnostic_poses(targets)
                self.diagnostic(packet)
            except Exception as exc:
                raise RuntimeError('IK trace delivery failed; accepted result not returned') from exc
        self._validated_poses = MappingProxyType(dict(actual))
        self._validated_command = command
        return command
