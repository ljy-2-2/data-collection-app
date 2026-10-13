"""G2-specific conversion from semantic head intent to read-only joint targets."""

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

from teleop.contracts.head import HeadOrientationIntent
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointSchema


G2_HEAD_YAW = "idx11_head_joint1"
G2_HEAD_ROLL = "idx12_head_joint2"
G2_HEAD_PITCH = "idx13_head_joint3"
G2_HEAD_JOINTS = (G2_HEAD_YAW, G2_HEAD_ROLL, G2_HEAD_PITCH)


@dataclass(frozen=True)
class G2HeadJointPreview:
    """Candidate yaw/pitch targets; the roll value is observed, not commanded."""

    targets: Mapping[str, float]
    anchor_positions: Mapping[str, float]
    roll_hold_rad: float
    clamped_joints: tuple[str, ...]
    direction_calibrated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))
        object.__setattr__(
            self,
            "anchor_positions",
            MappingProxyType(dict(self.anchor_positions)),
        )


class G2HeadJointMapper:
    """Apply the provisional G2 mounting signs and configured joint limits.

    The URDF-axis hypothesis is left = +idx11 and up = -idx13.  A read-only
    preview cannot prove the physical camera direction, so callers must keep
    ``direction_calibrated`` false until a separately authorized motion check.
    """

    def __init__(
        self,
        schema: JointSchema,
        *,
        yaw_joint_sign: float = 1.0,
        pitch_joint_sign: float = -1.0,
    ) -> None:
        missing = tuple(name for name in G2_HEAD_JOINTS if name not in schema.names)
        if missing:
            raise ValueError(f"schema is missing G2 head joints: {missing}")
        for label, value in (
            ("yaw_joint_sign", yaw_joint_sign),
            ("pitch_joint_sign", pitch_joint_sign),
        ):
            if isinstance(value, bool) or value not in (-1.0, 1.0):
                raise ValueError(f"{label} must be +1 or -1")
        self._schema = schema
        self._yaw_joint_sign = float(yaw_joint_sign)
        self._pitch_joint_sign = float(pitch_joint_sign)

    def preview(
        self,
        intent: HeadOrientationIntent,
        anchor_state: RobotState,
    ) -> G2HeadJointPreview:
        if not isinstance(intent, HeadOrientationIntent):
            raise TypeError("intent must be HeadOrientationIntent")
        if not isinstance(anchor_state, RobotState):
            raise TypeError("anchor_state must be RobotState")
        missing = tuple(
            name for name in G2_HEAD_JOINTS if name not in anchor_state.positions
        )
        if missing:
            raise ValueError(f"state is missing G2 head joints: {missing}")
        for name in G2_HEAD_JOINTS:
            value = anchor_state.positions[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"G2 head state {name} must be finite")
            if not math.isfinite(value):
                raise ValueError(f"G2 head state {name} must be finite")

        raw_targets = {
            G2_HEAD_YAW: anchor_state.positions[G2_HEAD_YAW]
            + self._yaw_joint_sign * intent.yaw_left_rad,
            G2_HEAD_PITCH: anchor_state.positions[G2_HEAD_PITCH]
            + self._pitch_joint_sign * intent.pitch_up_rad,
        }
        targets: dict[str, float] = {}
        clamped = []
        for name, raw_target in raw_targets.items():
            joint = self._schema.get(name)
            target = min(max(raw_target, joint.position_min), joint.position_max)
            targets[name] = target
            if not math.isclose(target, raw_target, abs_tol=1e-12):
                clamped.append(name)
        return G2HeadJointPreview(
            targets=targets,
            anchor_positions={
                name: float(anchor_state.positions[name]) for name in G2_HEAD_JOINTS
            },
            roll_hold_rad=float(anchor_state.positions[G2_HEAD_ROLL]),
            clamped_joints=tuple(clamped),
        )
