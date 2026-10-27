"""Grip-held joint-space preparation from measured pose; no automatic motion."""

import math
import numpy as np
from .control import check_start_pose

READY_JOINTS_DEG = np.array([0.0, -35.0, 0.0, -65.0, 0.0, 15.0, 0.0])


class PreparationPlan:
    def __init__(self, models, measured, config, validate=True, sides=None):
        self.start = {s: np.asarray(q, dtype=float).copy() for s, q in measured.items()}
        self.sides = set(models.arms if sides is None else sides)
        self.goal = {
            s: READY_JOINTS_DEG.copy() if s in self.sides else self.start[s].copy()
            for s in models.arms
        }
        check_start_pose(models, self.goal, config["collision_margin_m"], self.sides)
        self.delta = {s: self.goal[s] - self.start[s] for s in self.goal}
        largest = max(float(np.max(np.abs(d))) for d in self.delta.values())
        self.speed = min(3.0, config["max_joint_speed_deg_s"])
        self.accel = min(3.0, config["max_joint_accel_deg_s2"])
        # Quintic s(u)=10u^3-15u^4+6u^5: peak ds/du=1.875,
        # peak |d2s/du2|=10/sqrt(3). Both endpoints have zero speed/accel.
        self.duration = 1.05 * max(
            1.0,
            1.875 * largest / self.speed,
            math.sqrt((10 / math.sqrt(3)) * largest / self.accel),
        )
        self.samples = max(2, math.ceil(largest / 0.5) + 1)
        self.checked_samples = 0
        if validate:
            self.validate_chunk(models, config, self.samples)

    @property
    def validated(self):
        return self.checked_samples == self.samples

    def validate_chunk(self, models, config, count=4):
        from .gripper_collision import motion_reason
        for index in range(
            self.checked_samples, min(self.samples, self.checked_samples + count)
        ):
            u = index / (self.samples - 1)
            q = {s: self.start[s] + u * self.delta[s] for s in self.goal}
            if any(not models.arms[s].valid_joints(v) for s, v in q.items()):
                raise ValueError("准备路径超出关节限位")
            reason = models.collision_reason(q, config["collision_margin_m"])
            if reason:
                raise ValueError(f"准备路径 {u:.1%} 存在干涉：{reason}")
            previous_u = max(0, index - 1) / (self.samples - 1)
            previous = {s: self.start[s] + previous_u * self.delta[s] for s in self.goal}
            reason = motion_reason(models, previous, q)
            if reason:
                raise ValueError(reason)
            self.checked_samples += 1

    def at(self, seconds):
        u = float(np.clip(seconds / self.duration, 0, 1))
        fraction = 10 * u**3 - 15 * u**4 + 6 * u**5
        return {s: self.start[s] + fraction * self.delta[s] for s in self.goal}

    def report(self):
        return {
            "start_joints_deg": {s: q.tolist() for s, q in self.start.items()},
            "goal_joints_deg": {s: q.tolist() for s, q in self.goal.items()},
            "duration_s": self.duration,
            "max_speed_deg_s": self.speed,
            "max_accel_deg_s2": self.accel,
            "path_samples": self.samples,
            "path_validated": self.validated,
            "moving_sides": sorted(self.sides),
            "operator_action": "按住哪侧握把，只准备哪侧；松开只停止对应臂；本侧准备完成后松开再握住进入遥操",
        }
