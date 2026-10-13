"""Verify anatomical reflection using BOTH installed Marvin models.

This is an offline/startup invariant, never a command sign override. A spatial
angular velocity is an axial vector: under reflection H it becomes det(H) H w.
Mirroring rotations therefore requires H dR H, not a component-wise Euler flip.
"""

import numpy as np
from .differential import jacobian, rotation_vector


def validate_mirror(arms, samples=40):
    left, right = arms["left"], arms["right"]
    h = np.diag([1.0, -1.0, 1.0])  # W axes: forward, left, up
    zero = np.zeros(7)
    # Validate the robot installation independently of possibly different tools.
    l0, r0 = left.flange_fk(zero), right.flange_fk(zero)
    jl, jr = jacobian(left, zero, l0), jacobian(right, zero, r0)
    dots = np.sum(jr[3:] * (-h @ jl[3:]), axis=0)
    if np.max(np.abs(np.abs(dots) - 1)) > 1e-4:
        raise ValueError("installed arms do not have corresponding mirrored joint axes")
    signs = np.sign(dots)
    flange_reflection = l0[:3, :3].T @ h @ r0[:3, :3]
    offset = right.base[:3, 3] - h @ left.base[:3, 3]
    rng = np.random.default_rng(696)
    max_p = max_r = max_j = 0.0
    for _ in range(samples):
        q = rng.uniform(
            [-40, -70, -50, -100, -40, -30, -30], [40, -15, 50, -25, 40, 30, 30]
        )
        qr = signs * q
        if not left.valid_joints(q) or not right.valid_joints(qr):
            raise ValueError("mirrored configurations violate Marvin limits")
        l, r = left.flange_fk(q), right.flange_fk(qr)
        max_p = max(max_p, float(np.linalg.norm(h @ l[:3, 3] + offset - r[:3, 3])))
        expected_r = h @ l[:3, :3] @ flange_reflection
        max_r = max(
            max_r, float(np.linalg.norm(rotation_vector(expected_r @ r[:3, :3].T)))
        )
        jl, jr = jacobian(left, q, l), jacobian(right, qr, r)
        expected_j = np.vstack((h @ jl[:3], -h @ jl[3:]))
        max_j = max(max_j, float(np.max(np.abs(jr * signs - expected_j))))
    if max_p > 1e-4 or max_r > 1e-4 or max_j > 1e-4:
        raise ValueError(
            f"Marvin mirror mismatch: position={max_p}, rotation={max_r}, Jacobian={max_j}"
        )
    return {
        "samples": samples,
        "joint_signs_left_to_right": signs.tolist(),
        "world_reflection": h.tolist(),
        "flange_reflection": flange_reflection.tolist(),
        "position_offset_m": offset.tolist(),
        "max_position_error_m": max_p,
        "max_rotation_error_rad": max_r,
        "max_jacobian_error": max_j,
    }


def joint_symmetry(joints, arm_control=None):
    """Diagnostic only: the URDF-verified anatomical correspondence, not a servo target."""
    if not joints or any(s not in joints for s in ('left', 'right')):
        return None
    signs = np.array([-1., 1., -1., 1., -1., 1., -1.])
    left, right = (np.asarray(joints[s], dtype=float) for s in ('left', 'right'))
    if left.shape != (7,) or right.shape != (7,) or not np.isfinite([left, right]).all():
        return None
    error = right - signs * left
    result = {'joint_signs_left_to_right': signs.tolist(),
              'mirror_error_deg': error.tolist(),
              'left_j1_deg': float(left[0]), 'right_j1_deg': float(right[0]),
              'j1_mirror_error_deg': float(error[0]),
              'anchor_j1_mirror_error_deg': None, 'relative_j1_mirror_error_deg': None,
              'interpretation': 'Compare only mirrored tasks and mirrored starting poses; not a fault or a coupled command.'}
    anchors = [(arm_control or {}).get(s, {}).get('joint_anchor_deg') for s in ('left', 'right')]
    if all(a is not None for a in anchors):
        start_error = float(anchors[0][0] + anchors[1][0])
        result['anchor_j1_mirror_error_deg'] = start_error
        result['relative_j1_mirror_error_deg'] = float(error[0] - start_error)
    return result
