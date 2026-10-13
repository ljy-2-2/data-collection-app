"""Manufacturer CCS inverse solutions with local waypoints and elbow branch holds."""

import numpy as np
from .differential import rotation_vector
from .geometry import rotation
from .motion import advance


class BranchDomain:
    def __init__(self, arm, reference):
        self.arm = arm
        d = reference[3] - arm.elbow_singularity_deg
        self.sign = 1 if d >= 0 else -1
        self.margin = min(0.1, abs(d))

    def valid_joints(self, q):
        return (
            self.arm.valid_joints(q)
            and self.sign * (q[3] - self.arm.elbow_singularity_deg)
            >= self.margin - 1e-12
        )


def step(arm, target, reference, dt, speed, velocity=None, max_accel=None):
    q, target = np.asarray(reference, dtype=float), np.asarray(target, dtype=float)
    v = np.zeros(7) if velocity is None else np.asarray(velocity, dtype=float)
    if (
        not arm.valid_joints(q)
        or target.shape != (4, 4)
        or not np.isfinite(target).all()
        or not 0 < dt <= 0.1
        or not np.isfinite(speed)
        or speed <= 0
        or max_accel is None
        or max_accel <= 0
        or v.shape != (7,)
        or not np.isfinite(v).all()
    ):
        raise ValueError("invalid vendor tracking input")
    current = arm.fk(q)
    dp = target[:3, 3] - current[:3, 3]
    dr = rotation_vector(target[:3, :3] @ current[:3, :3].T)
    distance, angular = np.linalg.norm(dp), np.linalg.norm(dr)
    domain = BranchDomain(arm, q)
    desired = q.copy()
    arm.last_ik_constrained = False
    arm.last_vendor_note = "厂家参考构型逆解"
    arm.last_vendor_waypoint = None
    if distance > 1e-8 or angular > 1e-8:
        fraction = min(1.0, 0.03 / max(distance, 1e-12), 0.15 / max(angular, 1e-12))
        for shrink in (1.0, 0.25, 0.0625, 0.015625, 0.00390625):
            f = fraction * shrink
            waypoint = current.copy()
            waypoint[:3, 3] += f * dp
            waypoint[:3, :3] = (
                rotation(dr, f * angular) if angular > 1e-12 else np.eye(3)
            ) @ current[:3, :3]
            try:
                candidate = arm.ik(waypoint, q, allow_redundant_singular=True)
                if np.max(np.abs(candidate - q)) > 8 or not domain.valid_joints(
                    candidate
                ):
                    raise ValueError("vendor solution changes branch or is not local")
            except ValueError as error:
                arm.last_vendor_note = str(error)
                continue
            desired = candidate
            arm.last_vendor_waypoint = waypoint.copy()
            arm.last_ik_constrained = shrink < 1
            arm.last_vendor_note = (
                "厂家参考构型逆解"
                if shrink == 1
                else "厂家逆解缩短本次目标，保持当前肘部构型"
            )
            break
        else:
            arm.last_ik_constrained = True
            arm.last_vendor_note = (
                "当前构型无局部可达逆解，减速保持；向回移动手柄或重新定位"
            )
    result, arm.last_motion_constrained = advance(
        domain, q, desired, v, dt, speed, max_accel
    )
    return result
