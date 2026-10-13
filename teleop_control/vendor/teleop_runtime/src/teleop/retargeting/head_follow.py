"""Two-axis direction mapping; no robot, SDK, command or IK dependencies."""

import math

from teleop.contracts.head import HeadOrientationIntent
from teleop.contracts.xr import XRFrame


def _direction_angles(quaternion: tuple[float, float, float, float]) -> tuple[float, float]:
    norm = math.hypot(*quaternion)
    if not math.isfinite(norm) or norm < 1e-12:
        raise ValueError("head quaternion must have a finite non-zero norm")
    x, y, z, w = (component / norm for component in quaternion)
    # R(q) @ (0, 0, -1): viewing direction in right-handed OpenXR local space.
    # Local roll changes the up/right vectors but not this forward vector.
    fx = -2 * (x*z + w*y)
    fy = 2 * (w*x - y*z)
    fz = -(1 - 2 * (x*x + y*y))
    horizontal = math.hypot(fx, fz)
    if horizontal < 0.05:
        raise ValueError("head direction nearly vertical; yaw is undefined")
    return math.atan2(-fx, -fz), math.atan2(fy, horizontal)


class HeadFollowMapper:
    """Anchor heading/elevation to gravity, not to a possibly rolled head frame.

    Heading difference uses the shortest signed angle (-pi..pi). This preview
    is for normal head turns, not accumulated multi-revolution tracking.
    """

    def __init__(self) -> None:
        self._anchor: tuple[float, float] | None = None
        self._last_timestamp = 0
        self.status = "waiting_for_anchor"

    @property
    def anchored(self) -> bool:
        return self._anchor is not None

    def reset(self) -> None:
        self._anchor = None
        self._last_timestamp = 0
        self.status = "waiting_for_anchor"

    def update(self, frame: XRFrame, *, now_ns: int | None = None) -> HeadOrientationIntent | None:
        # Offline callers may use sample time; live callers must supply current time.
        now_ns = frame.received_monotonic_ns if now_ns is None else now_ns
        age = now_ns - frame.received_monotonic_ns
        if frame.stale or age < 0 or age > 300_000_000:
            self.reset()
            self.status = "stale_or_invalid_time"
            return None
        try:
            angles = _direction_angles(frame.head_pose.orientation_xyzw)
        except ValueError:
            self.reset()
            self.status = "invalid_head_orientation"
            raise
        if frame.device_timestamp_ns <= self._last_timestamp:
            self.status = "duplicate_or_out_of_order"
            return None
        self._last_timestamp = frame.device_timestamp_ns
        if self._anchor is None:
            self._anchor = angles
            self.status = "anchored"
            return HeadOrientationIntent(0.0, 0.0)
        yaw = angles[0] - self._anchor[0]
        self.status = "tracking"
        return HeadOrientationIntent(
            math.atan2(math.sin(yaw), math.cos(yaw)), angles[1] - self._anchor[1]
        )
