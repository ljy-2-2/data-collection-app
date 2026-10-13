"""File 3/4 — IK: laptop-side differential IK (trimmed robot_io/arm_ik.py).

One damped-least-squares step per tick toward the retargeted EE pose. The
solver integrates its OWN commanded configuration (seeded from telemetry on
engage) so telemetry lag cannot feed back; non-arm joints come from telemetry
so the base_link -> EE chain stays correct.

Targets: 4x4 poses of arm_{l,r}_end_link in base_link — exactly what
retarget.py emits.
"""
import json
import math
import os

import numpy as np
import pinocchio as pin

from telemetry import complete_joint_positions

URDF = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "assets", "G2_t2_crs.urdf")
BASE_FRAME = "base_link"
EE_FRAMES = {"left": "arm_l_end_link", "right": "arm_r_end_link"}
ARM_JOINTS = {
    "left": ["idx21_arm_l_joint1", "idx22_arm_l_joint2", "idx23_arm_l_joint3",
             "idx24_arm_l_joint4", "idx25_arm_l_joint5", "idx26_arm_l_joint6",
             "idx27_arm_l_joint7"],
    "right": ["idx61_arm_r_joint1", "idx62_arm_r_joint2", "idx63_arm_r_joint3",
              "idx64_arm_r_joint4", "idx65_arm_r_joint5", "idx66_arm_r_joint6",
              "idx67_arm_r_joint7"],
}
BODY_JOINTS = [f"idx0{i}_body_joint{i}" for i in range(1, 6)]

DAMPING = 1.0e-3        # damped-least-squares lambda^2
ROT_WEIGHT = 0.5        # rotation error weight vs translation (rad vs m)
POSTURE_GAIN = 0.5      # [1/s] null-space pull toward the engage posture
LIMIT_MARGIN = 0.02     # [rad] stand-off from joint limits

# The GDK client validates joint_servo_control against ITS OWN limit table,
# which is ~0.035 rad narrower than the URDF on every arm joint (hw 2026-07-16:
# 'Param idx66_arm_r_joint6 value is out of range' -> request rejected).
GDK_LIMITS_JSON = os.path.expanduser(
    "~/.cache/agibot/app/gdk/config/mc_impl_config.json")


def _gdk_limits():
    try:
        with open(GDK_LIMITS_JSON) as f:
            d = json.load(f)
        return {n: (float(v["min"]), float(v["max"])) for n, v in d.items()
                if isinstance(v, dict) and "min" in v and "max" in v}
    except Exception:
        return {}


class ArmIK:
    def __init__(self, max_joint_vel=2.0):
        self._max_vel = float(max_joint_vel)
        self._model = pin.buildModelFromUrdf(os.path.normpath(URDF))
        self._data = self._model.createData()
        self._q_neutral = pin.neutral(self._model)
        self._base_fid = self._model.getFrameId(BASE_FRAME)
        self._ee_fids = {s: self._model.getFrameId(f) for s, f in EE_FRAMES.items()}

        gdk_lim = _gdk_limits()
        self._idx_q, self._idx_v, self._lo, self._hi = {}, {}, {}, {}
        for side, names in ARM_JOINTS.items():
            joints = [self._model.joints[self._model.getJointId(n)] for n in names]
            self._idx_q[side] = np.array([j.idx_q for j in joints])
            self._idx_v[side] = np.array([j.idx_v for j in joints])
            lo = self._model.lowerPositionLimit[self._idx_q[side]].copy()
            hi = self._model.upperPositionLimit[self._idx_q[side]].copy()
            for i, n in enumerate(names):  # intersect with the client's table
                if n in gdk_lim:
                    lo[i] = max(lo[i], gdk_lim[n][0])
                    hi[i] = min(hi[i], gdk_lim[n][1])
            self._lo[side] = lo + LIMIT_MARGIN
            self._hi[side] = hi - LIMIT_MARGIN

        self._q_cmd = {"left": None, "right": None}   # commanded-config integrator
        self._q_seed = {"left": None, "right": None}  # engage posture (null-space)
        self.last_err = {}  # side -> (pos_err_m, rot_err_rad) of the last solve

    def seeded(self, side):
        return self._q_cmd[side] is not None

    def seed(self, side, joint_positions):
        """Latch the integrator onto the measured arm position (on engage)."""
        complete_joint_positions(BODY_JOINTS, joint_positions)
        q = np.array(complete_joint_positions(ARM_JOINTS[side], joint_positions))
        self._q_cmd[side] = q
        self._q_seed[side] = q.copy()

    def clear(self, side):
        self._q_cmd[side] = None
        self._q_seed[side] = None
        self.last_err.pop(side, None)

    def forward_pose(self, side, joint_positions):
        """Return base_link -> arm end-link as a 4x4 matrix.

        ``joint_positions`` may contain the full live robot state or only the
        arm being evaluated. Missing joints use the URDF neutral position.
        """
        q = self._q_full(joint_positions)
        pin.framesForwardKinematics(self._model, self._data, q)
        pose = (
            self._data.oMf[self._base_fid].inverse()
            * self._data.oMf[self._ee_fids[side]]
        )
        result = np.eye(4, dtype=np.float64)
        result[:3, :3] = pose.rotation
        result[:3, 3] = pose.translation
        return result

    def _q_full(self, joint_positions):
        """Telemetry dict -> full configuration (continuous joints as cos/sin)."""
        q = self._q_neutral.copy()
        for name, pos in joint_positions.items():
            if not self._model.existJointName(name):
                continue
            j = self._model.joints[self._model.getJointId(name)]
            if j.nq == 1:
                q[j.idx_q] = pos
            elif j.nq == 2:
                q[j.idx_q] = math.cos(pos)
                q[j.idx_q + 1] = math.sin(pos)
        return q

    def solve(self, side, T_target, joint_positions, dt):
        """One CLIK step toward T_target (4x4 in base_link). Returns the 7
        commanded positions in ARM_JOINTS[side] order."""
        q_cmd = self._q_cmd[side]
        if q_cmd is None:
            raise RuntimeError(f"ik.solve({side!r}) before seed()")
        complete_joint_positions(BODY_JOINTS + ARM_JOINTS[side], joint_positions)

        q = self._q_full(joint_positions)
        q[self._idx_q[side]] = q_cmd
        pin.framesForwardKinematics(self._model, self._data, q)
        T_cur = self._data.oMf[self._base_fid].inverse() * self._data.oMf[self._ee_fids[side]]

        T_tgt = np.asarray(T_target, dtype=np.float64)
        M_tgt = pin.SE3(np.ascontiguousarray(T_tgt[:3, :3]), T_tgt[:3, 3].copy())
        err = pin.log(T_cur.actInv(M_tgt)).vector
        self.last_err[side] = (float(np.linalg.norm(err[:3])),
                               float(np.linalg.norm(err[3:])))

        J = pin.computeFrameJacobian(
            self._model, self._data, q, self._ee_fids[side], pin.LOCAL
        )[:, self._idx_v[side]]
        w = np.ones(6)
        w[3:] = ROT_WEIGHT
        Jw = J * w[:, None]
        J_pinv = Jw.T @ np.linalg.inv(Jw @ Jw.T + DAMPING * np.eye(6))
        dq = J_pinv @ (err * w)
        nullspace = np.eye(len(dq)) - J_pinv @ Jw
        dq += nullspace @ (POSTURE_GAIN * float(dt) * (self._q_seed[side] - q_cmd))

        if not np.all(np.isfinite(dq)):
            raise ValueError(f"ik.solve({side!r}): non-finite step "
                             "(bad target or telemetry)")

        max_step = self._max_vel * float(dt)
        biggest = float(np.max(np.abs(dq)))
        if biggest > max_step > 0.0:
            dq *= max_step / biggest

        q_cmd = np.clip(q_cmd + dq, self._lo[side], self._hi[side])
        self._q_cmd[side] = q_cmd
        return [float(v) for v in q_cmd]
