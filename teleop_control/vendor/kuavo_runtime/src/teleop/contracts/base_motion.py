"""Robot-independent planar velocity request, not a physical motion receipt."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class BaseVelocityIntent:
    monotonic_ns: int
    forward_m_s: float
    left_m_s: float
    yaw_rad_s: float
    enabled: bool
    frame_id: str = 'robot_base'

    def __post_init__(self):
        if type(self.monotonic_ns) is not int or self.monotonic_ns < 0:
            raise ValueError('monotonic_ns must be a nonnegative integer')
        if type(self.enabled) is not bool:
            raise ValueError('enabled must be bool')
        if self.frame_id != 'robot_base':
            raise ValueError('base velocity is in robot_base, not the world frame')
        for name in ('forward_m_s', 'left_m_s', 'yaw_rad_s'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(name+' must be finite numeric')
            object.__setattr__(self, name, float(value))
        if not self.enabled and any(self.velocity):
            raise ValueError('disabled intent must be zero')

    @property
    def velocity(self):
        return self.forward_m_s, self.left_m_s, self.yaw_rad_s
