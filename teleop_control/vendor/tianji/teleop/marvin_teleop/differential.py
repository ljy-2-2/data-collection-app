"""Bounded TCP steps using the vendor Jacobian, independently checked by URDF.

All joint increments here are radians; ArmModel and the vendor API use degrees.
The controller preserves the elbow branch present at clutch engagement and only
adds a bounded redundancy correction when the measured Jacobian becomes weak.
"""

import math
import numpy as np
from .constrained_ik import DAMPING_SQ

ERROR_GAIN_S = None  # Full Newton task step, as in the reference control flow.
ROTATION_WEIGHT_M = 0.35


class ElbowBranchDomain:
    """Keep a 7-DoF arm on the elbow branch present when control starts."""

    def __init__(self, arm, reference, margin_deg):
        self.arm = arm
        distance = float(reference[3] - arm.elbow_singularity_deg)
        self.sign = 1.0 if distance >= 0 else -1.0
        # A pose already closer than the requested margin remains valid, but it
        # may only move away from the singular surface rather than cross it.
        self.clearance = min(float(margin_deg), abs(distance))

    def __getattr__(self, name):
        return getattr(self.arm, name)

    def valid_joints(self, q, margin=0.2):
        return (
            self.arm.valid_joints(q, margin)
            and self.sign * (q[3] - self.arm.elbow_singularity_deg)
            >= self.clearance - 1e-10
        )

    def joint_halfspaces(self, margin=0.2):
        faces, bounds = self.arm.joint_halfspaces(margin)
        face = np.zeros(7)
        face[3] = -self.sign
        bound = -self.sign * self.arm.elbow_singularity_deg - self.clearance
        return np.vstack((faces, face)), np.r_[bounds, bound]


def rotation_vector(r):
    theta = math.acos(float(np.clip((np.trace(r) - 1) / 2, -1, 1)))
    skew = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])
    if theta < 1e-7:
        return skew / 2
    if math.pi - theta < 1e-5:
        _, vectors = np.linalg.eigh((r + r.T) / 2)
        axis = vectors[:, -1]
        if axis @ skew < 0:
            axis = -axis
        return theta * axis
    return theta / (2 * math.sin(theta)) * skew


def jacobian(arm, q, flange):
    """World-frame [linear; angular] velocity per radian of each URDF joint."""
    columns = []
    for t, axis in zip(arm.chain(q), arm.axes):
        world_axis = t[:3, :3] @ axis
        world_axis /= np.linalg.norm(world_axis)
        columns.append(
            np.r_[np.cross(world_axis, flange[:3, 3] - t[:3, 3]), world_axis]
        )
    return np.column_stack(columns)


def step(
    arm,
    target,
    reference,
    dt,
    max_speed_deg_s,
    velocity=None,
    max_accel=None,
    motion_limiter=None,
    elbow_reference=None,
    elbow_branch_margin_deg=0.0,
    rotation_weight_m=ROTATION_WEIGHT_M,
    singular_rotation_weight_m=ROTATION_WEIGHT_M,
    singular_sigma_low=0.02,
    singular_sigma_high=0.08,
    elbow_posture_offset_deg=58.0,
    elbow_posture_gain_s=0.0,
    joint_limit_soft_margin_deg=0.0,
    joint_limit_reserve_deg=0.0,
    joint_limit_escape_speed_deg_s=0.0,
    shoulder_motion_weight=1.0,
):
    q = np.asarray(reference, dtype=float)
    target = np.asarray(target, dtype=float)
    if (
        not arm.valid_joints(q)
        or target.shape != (4, 4)
        or not np.isfinite(target).all()
        or not 0 < dt <= 0.1
        or not math.isfinite(max_speed_deg_s)
        or max_speed_deg_s <= 0
        or not 0 < singular_rotation_weight_m <= rotation_weight_m
        or not 0 < singular_sigma_low < singular_sigma_high
        or elbow_branch_margin_deg < 0
        or not 0 < elbow_posture_offset_deg <= 65
        or elbow_posture_gain_s < 0
        or not 0 <= joint_limit_soft_margin_deg <= 45
        or not 0 <= joint_limit_reserve_deg < max(joint_limit_soft_margin_deg, 1e-12)
        or not math.isfinite(shoulder_motion_weight)
        or not 1 <= shoulder_motion_weight <= 10
        or not 0 <= joint_limit_escape_speed_deg_s <= 30
    ):
        raise ValueError("invalid differential IK input")
    current = arm.fk(q)
    error = np.r_[
        target[:3, 3] - current[:3, 3],
        rotation_vector(target[:3, :3] @ current[:3, :3].T),
    ]
    # Weight rotation in metre-equivalent units. Damping bounds singular modes;
    # a zero task error produces exactly zero joint motion, even at full stretch.
    # Use the same exact manufacturer model as FK. The literal URDF Jacobian
    # above remains an independent audit; its rounded pi/2 origins can amplify
    # left/right differences when active limit faces change near a singularity.
    raw_jacobian = arm.world_jacobian(q, current)
    singular_values = np.linalg.svd(raw_jacobian, compute_uv=False)
    sigma_min = float(singular_values[-1])
    singular_blend = float(
        np.clip(
            (sigma_min - singular_sigma_low)
            / max(1e-12, singular_sigma_high - singular_sigma_low),
            0.0,
            1.0,
        )
    )
    domain = (
        ElbowBranchDomain(arm, np.asarray(elbow_reference), elbow_branch_margin_deg)
        if elbow_reference is not None and elbow_branch_margin_deg > 0
        else arm
    )
    faces, bounds = domain.joint_halfspaces()
    joint_slack_deg = bounds - faces @ q
    minimum_slack_deg = float(np.min(joint_slack_deg))
    limit_blend = (
        float(np.clip(minimum_slack_deg / joint_limit_soft_margin_deg, 0.0, 1.0))
        if joint_limit_soft_margin_deg > 0
        else 1.0
    )
    quality_blend = min(singular_blend, limit_blend)
    rotation_weight = singular_rotation_weight_m + quality_blend * (
        rotation_weight_m - singular_rotation_weight_m
    )
    weight = np.array([1.0] * 3 + [rotation_weight] * 3)
    j = weight[:, None] * raw_jacobian
    # Equal joint weights by default. An optional shoulder preference uses
    # the same metric in both arms and in the bounded QP below.
    joint_metric = np.ones(7)
    joint_metric[0] = shoulder_motion_weight**2
    inverse_metric = 1.0 / joint_metric
    weighted_transpose = inverse_metric[:, None] * j.T
    weighted_error = error * weight
    inverse_task = np.linalg.solve(
        j @ weighted_transpose + DAMPING_SQ * np.eye(6), np.eye(6)
    )
    pseudo_inverse = weighted_transpose @ inverse_task
    correction = pseudo_inverse @ weighted_error
    assist = 0.0
    if (
        elbow_posture_gain_s > 0
        and singular_blend < 1.0
        and isinstance(domain, ElbowBranchDomain)
    ):
        # Only assist while tracking a nonzero task. Holding a still target must
        # never make the arm quietly reconfigure itself.
        activity = min(1.0, float(np.linalg.norm(weighted_error)) / 0.01)
        assist = (1.0 - singular_blend) * activity
        preferred = arm.elbow_singularity_deg + domain.sign * elbow_posture_offset_deg
        posture = np.zeros(7)
        posture[3] = np.radians(preferred - q[3])
        correction += (
            (np.eye(7) - pseudo_inverse @ j)
            @ posture
            * elbow_posture_gain_s
            * dt
            * assist
        )
    limit_assist = 0.0
    if (
        joint_limit_escape_speed_deg_s > 0
        and limit_blend < 1.0
        and float(np.linalg.norm(weighted_error)) > 1e-12
    ):
        # Near a joint/wrist boundary, preserve TCP position while spending
        # redundant motion on recovering joint margin. Using the position
        # nullspace provides four escape directions; the full 6-D nullspace
        # has only one and cannot leave simultaneous J3/J6 boundaries.
        activity = min(1.0, float(np.linalg.norm(weighted_error)) / 0.01)
        position_j = raw_jacobian[:3]
        position_inverse = np.linalg.solve(
            position_j @ (inverse_metric[:, None] * position_j.T) + 0.01**2 * np.eye(3),
            np.eye(3),
        )
        position_null = (
            np.eye(7)
            - (inverse_metric[:, None] * position_j.T) @ position_inverse @ position_j
        )
        face_weights = np.square(
            np.clip(1.0 - joint_slack_deg / joint_limit_soft_margin_deg, 0.0, 1.0)
        )
        escape = -position_null @ (inverse_metric * (faces.T @ face_weights))
        maximum = float(np.max(np.abs(escape)))
        if maximum > 1e-12:
            limit_assist = (1.0 - limit_blend) * activity
            correction += (
                escape
                / maximum
                * math.radians(joint_limit_escape_speed_deg_s)
                * dt
                * limit_assist
            )
    arm.last_shoulder_motion_weight = float(shoulder_motion_weight)
    arm.last_jacobian_sigma_min = sigma_min
    arm.last_jacobian_condition = float(singular_values[0] / max(sigma_min, 1e-15))
    arm.last_rotation_weight_m = float(rotation_weight)
    arm.last_elbow_assist = bool(assist > 0)
    arm.last_limit_assist = bool(limit_assist > 0)
    arm.last_joint_limit_margin_deg = minimum_slack_deg
    arm.last_elbow_branch_sign = (
        domain.sign if isinstance(domain, ElbowBranchDomain) else None
    )
    from .constrained_ik import bounded_correction

    correction, arm.last_ik_constrained = bounded_correction(
        j,
        correction,
        domain,
        q,
        soft_margin_deg=(
            joint_limit_soft_margin_deg if joint_limit_reserve_deg > 0 else 0.0
        ),
        reserve_deg=joint_limit_reserve_deg,
        dt=dt,
        max_outward_speed_deg_s=max_speed_deg_s,
        joint_metric=joint_metric,
    )
    dq = np.degrees(correction) * (1.0 if ERROR_GAIN_S is None else ERROR_GAIN_S * dt)
    if not np.isfinite(dq).all():
        raise ValueError("non-finite differential IK step")
    if velocity is not None:
        from .motion import advance

        result, arm.last_motion_constrained = (motion_limiter or advance)(
            domain, q, q + dq, np.asarray(velocity), dt, max_speed_deg_s, max_accel
        )
        return result
    maximum = max_speed_deg_s * dt
    dq *= min(1.0, maximum / max(1e-15, float(np.max(np.abs(dq)))))
    # Retain joint AND coupled wrist limits. Backtracking also prevents a large
    # residual from pushing the solver away from the requested flange pose.
    old_cost = np.linalg.norm(error * weight)
    for scale in (1.0, 0.5, 0.25, 0.125, 0.0625, 0.03125):
        candidate = q + scale * dq
        if not arm.valid_joints(candidate):
            continue
        actual = arm.fk(candidate)
        remaining = np.r_[
            target[:3, 3] - actual[:3, 3],
            rotation_vector(target[:3, :3] @ actual[:3, :3].T),
        ]
        if np.linalg.norm(remaining * weight) <= old_cost + 1e-9:
            return candidate
    return q.copy()  # blocked directions stay put; no alternate IK branch
