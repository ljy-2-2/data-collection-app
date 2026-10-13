"""Robot-independent two-axis head intent; not an executable joint command."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class HeadOrientationIntent:
    """Changes from the operator anchor, radians; left/up are positive.

    Roll and translation are intentionally absent. A robot-specific mapper must
    still combine these changes with a robot anchor, mounting and joint limits.
    The current ControlRouter does not execute this intent.
    """

    yaw_left_rad: float
    pitch_up_rad: float

    def __post_init__(self) -> None:
        for name in ("yaw_left_rad", "pitch_up_rad"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite number")
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, float(value))
