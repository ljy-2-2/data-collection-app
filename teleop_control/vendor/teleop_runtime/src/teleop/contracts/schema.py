from dataclasses import dataclass
import math


@dataclass(frozen=True)
class JointInfo:
    """Standard description of one logical robot joint."""

    name: str
    position_min: float
    position_max: float
    max_step: float
    independently_commandable: bool = True
    max_velocity: float | None = None
    group: str = "other"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("joint name must not be empty")
        values = (self.position_min, self.position_max, self.max_step)
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"joint {self.name!r} limits must be finite")
        if self.position_min > self.position_max:
            raise ValueError(f"joint {self.name!r} has inverted position limits")
        if self.max_step < 0:
            raise ValueError(f"joint {self.name!r} max_step must be non-negative")
        if not isinstance(self.independently_commandable, bool):
            raise ValueError(
                f"joint {self.name!r} independently_commandable must be bool"
            )
        if self.max_velocity is not None and (
            not math.isfinite(self.max_velocity) or self.max_velocity <= 0
        ):
            raise ValueError(
                f"joint {self.name!r} max_velocity must be finite and positive"
            )
        if not isinstance(self.group, str) or not self.group.strip():
            raise ValueError(f"joint {self.name!r} group must not be empty")

    @property
    def position_limits(self) -> tuple[float, float]:
        """Inclusive joint-position limits in radians."""
        return (self.position_min, self.position_max)


@dataclass(frozen=True)
class JointSchema:
    """The named joints exposed by one RobotAdapter."""

    joints: tuple[JointInfo, ...]

    def __post_init__(self) -> None:
        names = [joint.name for joint in self.joints]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate joint names: {duplicates}")

    @property
    def commandable_names(self) -> tuple[str, ...]:
        return tuple(
            joint.name for joint in self.joints if joint.independently_commandable
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(joint.name for joint in self.joints)

    @property
    def group_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(joint.group for joint in self.joints))

    def get(self, name: str) -> JointInfo:
        for joint in self.joints:
            if joint.name == name:
                return joint
        raise KeyError(name)
