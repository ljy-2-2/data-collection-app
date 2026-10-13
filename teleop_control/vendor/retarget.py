"""File 2/4 — RETARGET: clutch + map controller motion to EE targets.

Same math as the production StockRetargeter, nothing else:
- grip > 0.95 engages: latch the (compensated) controller pose, the robot's
  CURRENT EE pose (anchor_fn), and the OPERATOR YAW FRAME — the headset's
  facing direction projected to horizontal. Deltas are expressed in that yaw
  frame before the fixed XR->robot axis swap, so "push away from you" is
  always robot-forward no matter how the XR world frame was oriented when
  the headset booted/recentered.
- while engaged (R_op = yaw frame at engage, columns are op axes in XR world):
      t_target = ee0.t + scale * R_rx @ R_opᵀ @ (vr.t - vr0.t)
      R_target = (R_rx @ R_opᵀ @ (vr.R @ vr0.Rᵀ) @ R_op @ R_rxᵀ) @ ee0.R
- grip < 0.90 releases: returns no target (send.py stops sending; the arm
  holds its last joint_servo target on its own). Re-engage re-anchors.

Targets are 4x4 poses of arm_{l,r}_end_link expressed in base_link.
"""
import math

import numpy as np

ENGAGE = 0.95
RELEASE = 0.90

# XR (OpenXR: x right, y up, z back) -> robot base_link (x fwd, y left, z up).
R_ROBOT_FROM_XR = np.array([[0.0, 0.0, -1.0],
                            [-1.0, 0.0, 0.0],
                            [0.0, 1.0, 0.0]])


def _rpy_deg(rx, ry, rz):
    rx, ry, rz = map(math.radians, (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = (math.cos(rx), math.sin(rx), math.cos(ry),
                              math.sin(ry), math.cos(rz), math.sin(rz))
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rx @ Ry @ Rz


# How the controller sits in the hand (stock rotation_compensation; the left
# hand mirrors the right about the sagittal plane).
R_COMP = {"right": _rpy_deg(90.0, 5.0, 0.0), "left": _rpy_deg(90.0, -5.0, 0.0)}


def pose_to_T(p):
    """[x,y,z,qx,qy,qz,qw] -> 4x4."""
    x, y, z, qx, qy, qz, qw = [float(v) for v in p]
    n = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    T = np.eye(4)
    T[:3, 3] = (x, y, z)
    if n < 1e-12:
        return T
    qx, qy, qz, qw = qx / n, qy / n, qz / n, qw / n
    T[:3, :3] = [
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
        [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
        [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)],
    ]
    return T


def yaw_frame(head_pose):
    """Operator yaw frame in XR world coords (same convention as XR: y up,
    z = backward of where the head faces). Columns = op axes in XR world."""
    R = pose_to_T(head_pose)[:3, :3]
    fwd = R @ np.array([0.0, 0.0, -1.0])  # headset forward, XR world
    fwd[1] = 0.0                          # project to horizontal (XR y is up)
    n = float(np.linalg.norm(fwd))
    if n < 1e-6:
        return np.eye(3)  # looking straight up/down: fall back to XR world
    z = -fwd / n
    y = np.array([0.0, 1.0, 0.0])
    return np.column_stack([np.cross(y, z), y, z])


class Retargeter:
    def __init__(self, scale=1.0):
        self._scale = float(scale)
        self._st = {s: {"engaged": False, "vr0": None, "ee0": None, "R_op": None}
                    for s in ("left", "right")}

    def engaged(self, side):
        return self._st[side]["engaged"]

    def declutch(self, side=None):
        states = self._st.values() if side is None else (self._st[side],)
        for st in states:
            st["engaged"] = False
            st["vr0"] = None
            st["ee0"] = None
            st["R_op"] = None

    def update(self, side, ctrl, head_pose, anchor_fn):
        """One clutch step. ctrl = {"pose","grip","trigger"} and head_pose
        come from receive.py; anchor_fn() -> current EE 4x4 in base_link
        (called only on engage).
        Returns (target_4x4 | None, gripper_closed_frac 0..1)."""
        st = self._st[side]
        grip = float(ctrl["grip"])
        gripper = min(1.0, max(0.0, float(ctrl["trigger"])))

        T_vr = pose_to_T(ctrl["pose"])
        T_vr[:3, :3] = T_vr[:3, :3] @ R_COMP[side]

        if not st["engaged"] and grip > ENGAGE:
            ee0 = anchor_fn()
            if ee0 is not None and np.all(np.isfinite(ee0)):
                st["engaged"] = True
                st["vr0"] = T_vr.copy()
                st["ee0"] = np.asarray(ee0, dtype=np.float64).copy()
                st["R_op"] = yaw_frame(head_pose)
        elif st["engaged"] and grip < RELEASE:
            st["engaged"] = False

        if not st["engaged"]:
            return None, gripper

        R = R_ROBOT_FROM_XR @ st["R_op"].T  # XR world -> operator -> robot
        t_delta = R @ (T_vr[:3, 3] - st["vr0"][:3, 3])
        R_delta = R @ (T_vr[:3, :3] @ st["vr0"][:3, :3].T) @ R.T
        target = np.eye(4)
        target[:3, 3] = st["ee0"][:3, 3] + self._scale * t_delta
        target[:3, :3] = R_delta @ st["ee0"][:3, :3]
        return target, gripper
