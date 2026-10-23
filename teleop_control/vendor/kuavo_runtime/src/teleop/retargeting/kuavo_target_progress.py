"""Pure Kuavo target anti-windup; no IK, timing, or hardware authority.

The raw clutched hand target and the effective IK target are intentionally
different after a rejected increment. Only an explicitly committed successful
solve advances the accepted effective pose. Preparing a trial consumes its raw
cursor, so an unreachable outward movement cannot accumulate indefinitely.
"""

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

from teleop.contracts.intent import CartesianPose, EndEffectorPoseIntent
from teleop.retargeting.dual_arm import (
    _matmul, _matrix_to_quaternion, _quaternion_to_matrix, _transpose,
)


@dataclass(frozen=True, eq=False)
class TargetProgressTicket:
    """Single-use trial identity with immutable raw/effective diagnostic data."""

    token_id: int
    raw_intent: EndEffectorPoseIntent
    effective_intent: EndEffectorPoseIntent
    side_generations: tuple[tuple[str, int], ...]
    rebase_sources: Mapping


@dataclass(frozen=True)
class TargetProgressRebase:
    """Diagnostic provenance; neither a new input timestamp nor permission."""

    accepted_token_id: int
    failure_token_id: int
    qualified_pose: CartesianPose


class KuavoTargetProgress:
    """Single-owner bookkeeping, to be reset on each affected clutch epoch.

    ``prepare`` consumes raw input but never accepts an effective target.
    ``commit`` must only be called after the complete ordinary IK/FK/limit/time
    checks succeed; this class cannot establish any of those conditions.
    ``discard`` normally keeps the last accepted effective target. If requested,
    the first failure after a fully qualified solve may instead use that solved
    candidate's FK pose. Successful commits never accumulate the FK residual.
    Hard failures belong to the caller's stop/reset path, not retry permission.

    Targets are tracked independently by side. A multi-side ticket commits
    atomically; one rejected dual-arm solve discards both trial increments.
    Until a side has its first accepted solve, its effective target stays raw
    because no valid alternative baseline can be inferred here.
    """

    policy = 'discard_rejected_target_increments'
    failure_rebase_policy = 'last_validated_fk_once_per_success'

    def __init__(self):
        self._raw = {}
        self._accepted = {}
        self._pending = {}
        self._qualified = {}
        self._rebases = {}
        self._generations = {'left': 0, 'right': 0}
        self._next_token = 0

    def reset(self, sides=('left', 'right')):
        if isinstance(sides, str):
            raise ValueError('reset requires distinct left/right sides')
        try:
            selected = tuple(sides)
        except TypeError as exc:
            raise ValueError('reset requires distinct left/right sides') from exc
        if not selected or len(set(selected)) != len(selected) \
                or set(selected) - {'left', 'right'}:
            raise ValueError('reset requires distinct left/right sides')
        for side in selected:
            self._generations[side] += 1
            self._raw.pop(side, None)
            self._accepted.pop(side, None)
            self._pending.pop(side, None)
            self._qualified.pop(side, None)
            self._rebases.pop(side, None)

    def prepare(self, raw_intent):
        if not isinstance(raw_intent, EndEffectorPoseIntent):
            raise ValueError('EndEffectorPoseIntent required')
        effective = {}
        for side, raw in raw_intent.targets.items():
            previous = self._raw.get(side)
            accepted = self._accepted.get(side)
            if previous is not None and previous.frame_id != raw.frame_id:
                raise ValueError('target frame changed without resetting '+side)
            if accepted is None:
                effective[side] = raw
                continue
            # Match DualArmRetargeter's convention: a world/target-frame
            # rotation increment left-multiplies the last effective rotation.
            delta = _matmul(_quaternion_to_matrix(raw.orientation_xyzw),
                            _transpose(_quaternion_to_matrix(previous.orientation_xyzw)))
            rotation = _matmul(delta, _quaternion_to_matrix(accepted.orientation_xyzw))
            effective[side] = CartesianPose(raw.frame_id,
                tuple(a+r-p for a, r, p in zip(accepted.position_m,
                                              raw.position_m, previous.position_m)),
                _matrix_to_quaternion(rotation))
        return self._ticket(raw_intent, effective)

    def _ticket(self, raw_intent, effective):
        # Build/validate the entire trial before mutating either side's cursor.
        effective_intent = EndEffectorPoseIntent(effective, source=raw_intent.source)
        self._next_token += 1
        ticket = TargetProgressTicket(self._next_token, raw_intent, effective_intent,
            tuple((side, self._generations[side]) for side in raw_intent.targets),
            MappingProxyType({side: self._rebases[side] for side in raw_intent.targets
                              if side in self._rebases}))
        for side, raw in raw_intent.targets.items():
            self._raw[side] = raw
            self._pending[side] = ticket
        return ticket

    def diagnostic_fields(self, ticket):
        return {}

    def _current(self, ticket):
        return isinstance(ticket, TargetProgressTicket) \
            and tuple(side for side, _ in ticket.side_generations) == tuple(ticket.effective_intent.targets) \
            and all(self._pending.get(side) is ticket and self._generations[side] == generation
                    for side, generation in ticket.side_generations)

    def commit(self, ticket, *, qualified_poses=None):
        if not self._current(ticket):
            return False
        qualified = {}
        if qualified_poses is not None:
            if not isinstance(qualified_poses, Mapping) \
                    or set(qualified_poses) != set(ticket.effective_intent.targets):
                raise ValueError('qualified FK poses must match the active sides')
            for side, pose in qualified_poses.items():
                if not isinstance(pose, CartesianPose) \
                        or pose.frame_id != ticket.effective_intent.targets[side].frame_id:
                    raise ValueError('qualified FK pose frame/type mismatch for '+side)
                # Revalidate/copy the whole cache before any side mutates. It is
                # provided by the solver only after complete candidate checks.
                qualified[side] = CartesianPose(pose.frame_id, pose.position_m,
                                               pose.orientation_xyzw)
        self._accepted.update(ticket.effective_intent.targets)
        for side in ticket.effective_intent.targets:
            del self._pending[side]
            self._qualified.pop(side, None)
            if side in qualified:
                self._qualified[side] = (ticket.token_id, qualified[side])
        return True

    def discard(self, ticket, *, rebase_to_qualified=False):
        if not self._current(ticket):
            return False
        for side in ticket.effective_intent.targets:
            del self._pending[side]
            if rebase_to_qualified and side in self._qualified:
                accepted_token, pose = self._qualified.pop(side)
                self._accepted[side] = pose
                self._rebases[side] = TargetProgressRebase(accepted_token,
                                                         ticket.token_id, pose)
        return True


def _rotation_distance(a, b):
    # abs(dot) treats q and -q as the same orientation.
    return 2. * math.acos(min(1., abs(sum(x*y for x, y in zip(a, b)))))


def _bounded_pose(start, goal, position_step, rotation_step):
    delta = tuple(b-a for a, b in zip(start.position_m, goal.position_m))
    distance = math.sqrt(sum(x*x for x in delta))
    fraction = min(1., position_step/distance) if distance else 1.
    position = (goal.position_m if fraction == 1. else
                tuple(a+fraction*d for a, d in zip(start.position_m, delta)))
    angle = _rotation_distance(start.orientation_xyzw, goal.orientation_xyzw)
    quaternion = goal.orientation_xyzw
    if angle > rotation_step:
        a, b = start.orientation_xyzw, goal.orientation_xyzw
        if sum(x*y for x, y in zip(a, b)) < 0.:
            b = tuple(-x for x in b)
        fraction = rotation_step/angle
        # SLERP follows the shortest arc even for noncommuting rotations.
        denominator = math.sin(angle/2.)
        left = math.sin((1.-fraction)*angle/2.)/denominator
        right = math.sin(fraction*angle/2.)/denominator
        quaternion = tuple(left*x+right*y for x, y in zip(a, b))
    return CartesianPose(goal.frame_id, position, quaternion)


class FixedAnchorTargetProgress(KuavoTargetProgress):
    """Opt-in bounded numerical pursuit; never shifts the raw clutch mapping.

    The waypoint is the checked candidate's FK when provided, otherwise the
    accepted request for pure callers without FK. Neither is measured/sent pose.
    Each fresh call pursues only its latest raw goal. A rejected trial reduces
    the next numerical step but cannot accumulate a mapping offset. The caller
    still owns IK validation, input expiry, release and physical execution.
    """

    policy = 'fixed_anchor_bounded_latest_target'
    failure_rebase_policy = 'qualified_fk_waypoint_preserve_raw_clutch_anchor'

    def __init__(self, *, max_position_step_m=.01, max_orientation_step_rad=.05,
                 workspace_bounds=None, workspace_soft_margin_m=0.):
        super().__init__()
        for name, value, maximum in (
                ('max_position_step_m', max_position_step_m, .05),
                ('max_orientation_step_rad', max_orientation_step_rad, .1)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or not 0. < value <= maximum:
                raise ValueError(name+' must be finite, positive and within '+str(maximum))
        self._workspace_bounds = None
        if workspace_bounds is not None:
            # This explicit offline candidate dependency is not needed by the
            # default/live bundle. No discovery, parsing or device access here.
            from teleop.retargeting.kuavo_workspace_bound import ArmReachBound
            if not isinstance(workspace_bounds, Mapping) or not workspace_bounds \
                    or set(workspace_bounds)-{'left','right'} \
                    or any(not isinstance(b,ArmReachBound) for b in workspace_bounds.values()):
                raise ValueError('named arm reach bounds required')
            self._workspace_bounds = MappingProxyType(dict(workspace_bounds))
        if (isinstance(workspace_soft_margin_m, bool)
                or not isinstance(workspace_soft_margin_m, (int, float))
                or not math.isfinite(workspace_soft_margin_m)
                or workspace_soft_margin_m < 0.
                or (workspace_soft_margin_m > 0. and self._workspace_bounds is None)
                or (self._workspace_bounds is not None and any(
                    workspace_soft_margin_m >= b.wrist_radius_m
                    for b in self._workspace_bounds.values()))):
            raise ValueError('soft margin requires matching bounds and must be below each radius')
        self.workspace_soft_margin_m = float(workspace_soft_margin_m)
        self._workspace_margins = {}
        self.max_position_step_m = float(max_position_step_m)
        self.max_orientation_step_rad = float(max_orientation_step_rad)
        self._step_scales = {'left': 1., 'right': 1.}
        self._trial_scales = {}

    def reset(self, sides=('left', 'right')):
        # Base validation runs before any state changes (including generators).
        selected = tuple(sides) if not isinstance(sides, str) and sides is not None else sides
        super().reset(selected)
        for side in selected:
            self._step_scales[side] = 1.
            self._trial_scales.pop(side, None)
            self._workspace_margins.pop(side, None)

    def prepare(self, raw_intent):
        if not isinstance(raw_intent, EndEffectorPoseIntent):
            raise ValueError('EndEffectorPoseIntent required')
        effective = {}
        margins = dict(self._workspace_margins)
        for side, raw in raw_intent.targets.items():
            previous = self._raw.get(side)
            if previous is not None and previous.frame_id != raw.frame_id:
                raise ValueError('target frame changed without resetting '+side)
            accepted = self._accepted.get(side)
            scale = self._step_scales[side]
            goal = raw
            if self._workspace_bounds is not None:
                if side not in self._workspace_bounds:
                    raise ValueError('missing workspace bound for '+side)
                bound = self._workspace_bounds[side]
                if side not in margins:
                    # The first raw target in a clutch is anchored to current
                    # FK by the mapper. Keep it unchanged if inside the bound;
                    # fix this width until that side resets. Do not learn a
                    # translating offset or consume later outward movement.
                    radius = bound.assess(raw)['wrist_distance_m']
                    margins[side] = min(self.workspace_soft_margin_m,
                                        max(0., bound.wrist_radius_m-radius))
                goal = bound.project(raw, soft_margin_m=margins[side])
            position_step, orientation_step = self._step_limits(side)
            effective[side] = goal if accepted is None else _bounded_pose(
                accepted, goal, position_step*scale, orientation_step*scale)
        ticket = self._ticket(raw_intent, effective)
        self._workspace_margins = margins
        for side in raw_intent.targets:
            self._trial_scales[side] = (ticket, self._step_scales[side])
        return ticket

    def _step_limits(self, side):
        return self.max_position_step_m, self.max_orientation_step_rad

    def commit(self, ticket, *, qualified_poses=None):
        if not super().commit(ticket, qualified_poses=qualified_poses):
            return False
        if qualified_poses is not None:
            # Actual FK of the checked answer is the numerical waypoint.
            # Raw goals remain immutable mapper output and never inherit this
            # residual. This does not assert the robot reached the waypoint.
            for side in ticket.effective_intent.targets:
                self._accepted[side] = self._qualified[side][1]
        for side in ticket.effective_intent.targets:
            self._step_scales[side] = min(1., 2.*self._step_scales[side])
        return True

    def discard(self, ticket, *, rebase_to_qualified=False):
        # A qualified FK is a feasible numerical waypoint, not a new clutch
        # anchor. The base consumes each qualified pose at most once; prepare
        # still pursues the unchanged latest raw goal rather than raw deltas.
        if not super().discard(ticket, rebase_to_qualified=rebase_to_qualified):
            return False
        for side in ticket.effective_intent.targets:
            self._step_scales[side] = max(1./64., .5*self._step_scales[side])
        return True

    def diagnostic_fields(self, ticket):
        position_remaining, orientation_remaining, scales = {}, {}, {}
        for side, raw in ticket.raw_intent.targets.items():
            target = ticket.effective_intent.targets[side]
            position_remaining[side] = math.sqrt(sum((a-b)**2 for a, b in
                zip(raw.position_m, target.position_m)))
            orientation_remaining[side] = math.degrees(_rotation_distance(
                raw.orientation_xyzw, target.orientation_xyzw))
            trial, scale = self._trial_scales.get(side, (None, None))
            if trial is not ticket:
                raise ValueError('diagnostics require the current target trial')
            scales[side] = scale
        workspace = {}
        if self._workspace_bounds is not None:
            for side,raw in ticket.raw_intent.targets.items():
                goal = self._workspace_bounds[side].project(
                    raw, soft_margin_m=self._workspace_margins[side])
                workspace[side] = dict(frame_id=goal.frame_id,position_m=list(goal.position_m),
                    orientation_xyzw=list(goal.orientation_xyzw))
        return dict(workspace_targets=workspace,
                    workspace_constraints=(dict(
                        raw_clutch_anchor_preserved=True,
                        full_ik_feasibility_proven=False,
                        collision_free_path_proven=False,
                        orientation_change_can_shift_projected_position=True,
                        far_outside_sensitivity_tends_to_zero=bool(self.workspace_soft_margin_m))
                        if workspace else None),
                    workspace_policy=('orientation_preserving_soft_boundary' if workspace and self.workspace_soft_margin_m
                                      else 'orientation_preserving_outer_bound' if workspace else 'none'),
                    workspace_soft_margin_m=self.workspace_soft_margin_m,
                    workspace_effective_margin_m={side: self._workspace_margins[side]
                        for side in workspace},
                    workspace_margin_origin='first_raw_pose_per_clutch',
                    max_position_step_m=self.max_position_step_m,
                    max_orientation_step_rad=self.max_orientation_step_rad,
                    position_remaining_m=position_remaining,
                    orientation_remaining_deg=orientation_remaining,
                    numerical_step_scales=scales)
