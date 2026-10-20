"""Small strictly convex IK quadratic program; no hardware or extra dependencies.

The starting step is zero (feasible). A bounded active-set solve redistributes
motion when a joint/wrist face blocks the unconstrained Newton step. The motion
limiter still enforces per-cycle speed, acceleration and a valid stopping path.
"""

import numpy as np

NUMERICAL_RESERVE_DEG = 1e-6
DAMPING_SQ = 0.03**2


def bounded_correction(
    j,
    unconstrained,
    arm,
    q,
    soft_margin_deg=0.0,
    reserve_deg=0.0,
    dt=None,
    max_outward_speed_deg_s=None,
    joint_metric=None,
    damping_sq=DAMPING_SQ,
):
    faces, bounds = arm.joint_halfspaces()
    raw_faces = faces.copy()
    # Solve in radians; normalize rows for stable working-set tolerances.
    # Keep the planned correction microscopically INSIDE the existing safety
    # margin. Exact coupled-wrist faces suffer cancellation during integration
    # and braking; this reserve is 0.000001 degree, not a relaxed joint limit.
    slack = np.radians(bounds - faces @ q - NUMERICAL_RESERVE_DEG)
    lengths = np.linalg.norm(faces, axis=1)
    faces, slack = faces / lengths[:, None], np.maximum(0, slack) / lengths
    # A hard position face alone lets the motion limiter arrive at the face at
    # speed and then alternate between tracking and braking.  This was visible
    # in the live left-arm trace after J1/J3/J5 reached their limits together.
    # Add a velocity damper before the hard face: outward velocity falls
    # continuously to zero at ``reserve_deg`` while inward/tangential motion
    # remains free.  The ordinary hard faces above remain the final authority.
    if (
        soft_margin_deg > 0
        and 0 <= reserve_deg < soft_margin_deg
        and dt is not None
        and dt > 0
        and max_outward_speed_deg_s is not None
        and max_outward_speed_deg_s > 0
    ):
        distance_deg = (bounds - raw_faces @ q) / lengths
        near = distance_deg < soft_margin_deg
        if np.any(near):
            blend = np.clip(
                (distance_deg[near] - reserve_deg) / (soft_margin_deg - reserve_deg),
                0.0,
                1.0,
            )
            # Smoothstep avoids a slope jump when a face enters/leaves the
            # damping band, which otherwise feels like a small stick-slip.
            blend = blend * blend * (3.0 - 2.0 * blend)
            faces = np.vstack((faces, faces[: len(lengths)][near]))
            slack = np.r_[
                slack,
                np.radians(max_outward_speed_deg_s * dt * blend),
            ]
    # Pose tracking belongs in the smooth damped objective, not in normalized
    # zero-slack halfspaces made from each task's error direction. Those rows
    # become ill-conditioned as a task converges and amplify roundoff enough
    # to split otherwise mirrored trajectories. Only physical joint/branch
    # bounds and their velocity dampers are hard constraints here.
    if np.all(faces @ unconstrained <= slack + 1e-12):
        return unconstrained, False

    hessian = j.T @ j + damping_sq * (
        np.eye(7) if joint_metric is None else np.diag(joint_metric)
    )
    # This equals -J.T@error for the ordinary damped least-squares solution.
    # When differential.py adds a safe redundancy correction, retaining its
    # unconstrained optimum lets the active-set solve carry that correction up
    # to a joint/wrist boundary instead of silently throwing it away.
    gradient = -(hessian @ unconstrained)
    x = np.zeros(7)
    working = []
    for _ in range(48):
        g = hessian @ x + gradient
        if working:
            a = faces[working]
            kkt = np.block([[hessian, a.T], [a, np.zeros((len(a), len(a)))]])
            # Redundant faces at a joint/wrist corner can make KKT singular.
            solution = np.linalg.lstsq(kkt, np.r_[-g, np.zeros(len(a))], rcond=1e-11)[0]
            direction, multipliers = solution[:7], solution[7:]
        else:
            direction = np.linalg.solve(hessian, -g)
            multipliers = np.empty(0)
        if np.max(np.abs(direction)) < 1e-10:
            if not working or np.min(multipliers) >= -1e-9:
                break
            working.pop(int(np.argmin(multipliers)))
            continue
        products = faces @ direction
        candidates = [
            i for i in range(len(faces)) if i not in working and products[i] > 1e-12
        ]
        alpha, blocker = 1.0, None
        for i in candidates:
            fraction = max(0.0, (slack[i] - faces[i] @ x) / products[i])
            if fraction < alpha:
                alpha, blocker = fraction, i
        x += alpha * direction
        if blocker is not None:
            working.append(blocker)
    # A feasible, non-worsening partial solution is also safe on iteration limit.
    if (
        not np.isfinite(x).all()
        or np.any(faces @ x > slack + 1e-9)
        or 0.5 * x @ hessian @ x + gradient @ x > 1e-10
    ):
        return np.zeros(7), True
    return x, True
