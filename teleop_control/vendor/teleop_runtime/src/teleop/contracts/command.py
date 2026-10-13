from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class JointCommand:
    """A named-joint command that has not passed the safety gateway yet."""

    targets: Mapping[str, float]
    source: str
    sequence_id: int
    created_time_ns: int
    ttl_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))
        if not self.targets:
            raise ValueError("joint command must contain at least one target")
        if type(self.sequence_id) is not int or self.sequence_id < 0:
            raise ValueError("sequence_id must be a non-negative integer")
        if type(self.created_time_ns) is not int or self.created_time_ns < 0:
            raise ValueError("created_time_ns must be a non-negative integer")
        if type(self.ttl_ms) is not int or self.ttl_ms <= 0:
            raise ValueError("ttl_ms must be a positive integer")


@dataclass(frozen=True)
class ValidatedJointCommand(JointCommand):
    """Marker type produced only after SafetyGateway validation."""


@dataclass(frozen=True)
class GripperCommand:
    """Normalized gripper command that has not passed its safety gateway."""

    targets: Mapping[str, float]
    source: str
    sequence_id: int
    created_time_ns: int
    ttl_ms: int

    def __post_init__(self) -> None:
        targets = dict(self.targets)
        if not targets:
            raise ValueError("gripper command must contain at least one target")
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
        if type(self.sequence_id) is not int or self.sequence_id < 0:
            raise ValueError("sequence_id must be a non-negative integer")
        if type(self.created_time_ns) is not int or self.created_time_ns < 0:
            raise ValueError("created_time_ns must be a non-negative integer")
        if type(self.ttl_ms) is not int or self.ttl_ms <= 0:
            raise ValueError("ttl_ms must be a positive integer")
        object.__setattr__(self, "targets", MappingProxyType(normalized))


@dataclass(frozen=True)
class ValidatedGripperCommand(GripperCommand):
    """Marker type produced only after GripperSafetyGateway validation."""
