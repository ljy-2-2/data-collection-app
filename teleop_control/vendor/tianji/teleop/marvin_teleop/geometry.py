"""All internal transforms use metres, radians, column vectors and right-handed frames."""

import math
import numpy as np


def transform(xyz=(0, 0, 0), rpy=(0, 0, 0)):
    r, p, y = rpy
    rx = rotation([1, 0, 0], r)
    ry = rotation([0, 1, 0], p)
    rz = rotation([0, 0, 1], y)
    t = np.eye(4)
    t[:3, :3] = rz @ ry @ rx
    t[:3, 3] = xyz
    return t


def rotation(axis, angle):
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    x, y, z = a
    k = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + math.sin(angle) * k + (1 - math.cos(angle)) * (k @ k)


def inverse(t):
    out = np.eye(4)
    out[:3, :3] = t[:3, :3].T
    out[:3, 3] = -out[:3, :3] @ t[:3, 3]
    return out


def pose(values):
    if isinstance(values, str):
        values = values.split(",")
    v = np.asarray(values, dtype=float)
    if v.shape != (7,) or not np.isfinite(v).all():
        raise ValueError("pose must contain seven finite values")
    x, y, z, w = v[3:]
    n = np.linalg.norm(v[3:])
    if not 0.9 <= n <= 1.1:
        raise ValueError("invalid quaternion norm")
    x, y, z, w = v[3:] / n
    t = np.eye(4)
    t[:3, 3] = v[:3]
    t[:3, :3] = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    return t


def angle(r):
    return math.acos(float(np.clip((np.trace(r) - 1) / 2, -1, 1)))


def relative_target(hand, hand0, tcp0, basis, scale):
    out = tcp0.copy()
    out[:3, 3] += scale * basis @ (hand[:3, 3] - hand0[:3, 3])
    # Conjugation expresses the spatial rotation in the robot world basis.
    out[:3, :3] = basis @ hand[:3, :3] @ hand0[:3, :3].T @ basis.T @ tcp0[:3, :3]
    return out


def segment_distance(p1, q1, p2, q2):
    """Distance between finite segments, including degenerate points."""
    u, v, w = q1 - p1, q2 - p2, p1 - p2
    a, b, c, d, e = u @ u, u @ v, v @ v, u @ w, v @ w
    candidates = []

    def add(s, t):
        candidates.append(np.linalg.norm(w + s * u - t * v))

    if a < 1e-15 and c < 1e-15:
        return float(np.linalg.norm(w))
    if a < 1e-15:
        return float(np.linalg.norm(w - np.clip(e / c, 0, 1) * v))
    if c < 1e-15:
        return float(np.linalg.norm(w + np.clip(-d / a, 0, 1) * u))
    for s in (0.0, 1.0):
        add(s, np.clip((e + s * b) / c, 0, 1))
    for t in (0.0, 1.0):
        add(np.clip((t * b - d) / a, 0, 1), t)
    det = a * c - b * b
    if det > 1e-15:
        s, t = (b * e - c * d) / det, (a * e - b * d) / det
        if 0 <= s <= 1 and 0 <= t <= 1:
            add(s, t)
    return float(min(candidates))
