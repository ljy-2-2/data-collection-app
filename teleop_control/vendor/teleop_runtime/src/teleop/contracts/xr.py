from collections.abc import Iterable
from dataclasses import dataclass
import math


OPENXR_COORDINATE_FRAME = "openxr_local"


def _finite_tuple(name: str, values: Iterable[object], length: int) -> tuple[float, ...]:
    try:
        raw = tuple(values)
    except TypeError as error:
        raise ValueError(f"{name} must be an iterable of {length} numbers") from error
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


def _finite_float(name: str, value: object) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not bool")
    try:
        converted = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _require_int(name: str, value: object, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class Pose:
    """A pose in the paper's right-handed OpenXR local frame, in metres.

    Axes are X right, Y up, and Z backward. Quaternion order is x, y, z, w.
    Robot-frame conversion belongs to the later retargeting stage.
    """

    position_m: tuple[float, float, float]
    orientation_xyzw: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "position_m", _finite_tuple("position_m", self.position_m, 3)
        )
        object.__setattr__(
            self,
            "orientation_xyzw",
            _finite_tuple("orientation_xyzw", self.orientation_xyzw, 4),
        )

    @classmethod
    def from_sequence(cls, values: Iterable[object]) -> "Pose":
        pose = _finite_tuple("pose", values, 7)
        return cls(position_m=pose[:3], orientation_xyzw=pose[3:])

    @property
    def xyz_xyzw(self) -> tuple[float, ...]:
        return self.position_m + self.orientation_xyzw


@dataclass(frozen=True)
class ControllerButtons:
    """Side-local controller buttons.

    Primary/secondary means X/Y on the left controller and A/B on the right.
    """

    primary: bool = False
    secondary: bool = False
    menu: bool = False
    axis_click: bool = False

    def __post_init__(self) -> None:
        for name in ("primary", "secondary", "menu", "axis_click"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")


@dataclass(frozen=True)
class ControllerState:
    pose: Pose
    axis: tuple[float, float]
    grip: float
    trigger: float
    buttons: ControllerButtons

    def __post_init__(self) -> None:
        if not isinstance(self.pose, Pose):
            raise ValueError("pose must be a Pose")
        axis = _finite_tuple("axis", self.axis, 2)
        if any(value < -1.0 or value > 1.0 for value in axis):
            raise ValueError("axis values must be within [-1, 1]")
        grip = _finite_float("grip", self.grip)
        trigger = _finite_float("trigger", self.trigger)
        if not 0.0 <= grip <= 1.0:
            raise ValueError("grip must be within [0, 1]")
        if not 0.0 <= trigger <= 1.0:
            raise ValueError("trigger must be within [0, 1]")
        if not isinstance(self.buttons, ControllerButtons):
            raise ValueError("buttons must be ControllerButtons")
        object.__setattr__(self, "axis", axis)
        object.__setattr__(self, "grip", grip)
        object.__setattr__(self, "trigger", trigger)


@dataclass(frozen=True)
class XRFrame:
    """One immutable, robot-independent snapshot of XR operator input."""

    sequence_id: int
    device_timestamp_ns: int
    received_monotonic_ns: int
    stale: bool
    head_pose: Pose
    left_controller: ControllerState
    right_controller: ControllerState
    source: str = "unknown"
    coordinate_frame: str = OPENXR_COORDINATE_FRAME

    def __post_init__(self) -> None:
        _require_int("sequence_id", self.sequence_id, 0)
        _require_int("device_timestamp_ns", self.device_timestamp_ns, 1)
        _require_int("received_monotonic_ns", self.received_monotonic_ns, 0)
        if not isinstance(self.stale, bool):
            raise ValueError("stale must be bool")
        if not isinstance(self.head_pose, Pose):
            raise ValueError("head_pose must be a Pose")
        if not isinstance(self.left_controller, ControllerState):
            raise ValueError("left_controller must be ControllerState")
        if not isinstance(self.right_controller, ControllerState):
            raise ValueError("right_controller must be ControllerState")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("source must be a non-empty string")
        if self.coordinate_frame != OPENXR_COORDINATE_FRAME:
            raise ValueError(
                f"coordinate_frame must be {OPENXR_COORDINATE_FRAME!r} in XRFrame v1"
            )
