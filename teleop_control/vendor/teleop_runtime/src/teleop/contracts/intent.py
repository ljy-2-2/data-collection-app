from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Iterable, Mapping


def _finite_tuple(
    name: str,
    values: Iterable[object],
    length: int,
) -> tuple[float, ...]:
    try:
        raw = tuple(values)
    except TypeError as error:
        raise ValueError(f"{name} must contain exactly {length} numeric values") from error
    if len(raw) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    if any(isinstance(value, bool) for value in raw):
        raise ValueError(f"{name} values must be numbers, not bool")
    try:
        converted = tuple(float(value) for value in raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} values must be numeric") from error
    if not all(math.isfinite(value) for value in converted):
        raise ValueError(f"{name} values must be finite")
    return converted


@dataclass(frozen=True)
class CartesianPose:
    """One end-effector pose in a named robot coordinate frame."""

    frame_id: str
    position_m: tuple[float, float, float]
    orientation_xyzw: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        if not isinstance(self.frame_id, str) or not self.frame_id.strip():
            raise ValueError("frame_id must be a non-empty string")
        position = _finite_tuple("position_m", self.position_m, 3)
        quaternion = _finite_tuple("orientation_xyzw", self.orientation_xyzw, 4)
        norm = math.sqrt(sum(value * value for value in quaternion))
        if norm < 1e-12:
            raise ValueError("quaternion norm must be non-zero")
        object.__setattr__(self, "position_m", position)
        object.__setattr__(
            self,
            "orientation_xyzw",
            tuple(value / norm for value in quaternion),
        )


@dataclass(frozen=True)
class EndEffectorPoseIntent:
    """Robot-independent left/right end-effector pose targets."""

    targets: Mapping[str, CartesianPose]
    source: str = "xr_end_effector_pose"

    def __post_init__(self) -> None:
        targets = dict(self.targets)
        if not targets:
            raise ValueError("end-effector pose intent must contain at least one target")
        unknown = set(targets) - {"left", "right"}
        if unknown:
            raise ValueError(f"unsupported end-effector side: {sorted(unknown)}")
        for side, pose in targets.items():
            if not isinstance(pose, CartesianPose):
                raise ValueError(f"{side} target must be a CartesianPose")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        object.__setattr__(self, "targets", MappingProxyType(targets))


@dataclass(frozen=True)
class JointPositionIntent:
    """Absolute named-joint targets requested by an input source."""

    targets: Mapping[str, float]
    source: str = "direct_joint_position"

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))
        if not self.targets:
            raise ValueError("joint position intent must contain at least one target")


@dataclass(frozen=True)
class JointJogIntent:
    """Small named-joint increments relative to a fresh RobotState."""

    deltas: Mapping[str, float]
    source: str = "direct_joint_jog"

    def __post_init__(self) -> None:
        object.__setattr__(self, "deltas", MappingProxyType(dict(self.deltas)))
        if not self.deltas:
            raise ValueError("joint jog intent must contain at least one delta")


@dataclass(frozen=True)
class GripperIntent:
    """Robot-independent left/right gripper closure fractions in ``[0, 1]``."""

    targets: Mapping[str, float]
    source: str = "xr_gripper"

    def __post_init__(self) -> None:
        targets = dict(self.targets)
        if not targets:
            raise ValueError("gripper intent must contain at least one target")
        unknown = set(targets) - {"left", "right"}
        if unknown:
            raise ValueError(f"unsupported gripper side: {sorted(unknown)}")
        normalized = {}
        for side, value in targets.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{side} gripper target must be numeric")
            converted = float(value)
            if not math.isfinite(converted):
                raise ValueError(f"{side} gripper target must be finite")
            if not 0.0 <= converted <= 1.0:
                raise ValueError(f"{side} gripper target must be within [0, 1]")
            normalized[side] = converted
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        object.__setattr__(self, "targets", MappingProxyType(normalized))
