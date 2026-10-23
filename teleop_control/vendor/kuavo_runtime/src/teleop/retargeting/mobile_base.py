"""XRFrame -> planar base intent; pure math/state, with no hardware imports.

Wheel VR stick directions follow the vendor WheelQuest3IkIncrementalTools.
X+A toggles only this mapper's local unlock; it NEVER calls vendor reset/mode
services. Faults request zero immediately; ordinary centering slews to zero.
These are reference semantics, not verified physical stopping guarantees.
"""
from dataclasses import dataclass
import math

from teleop.contracts.base_motion import BaseVelocityIntent
from teleop.contracts.xr import XRFrame


@dataclass(frozen=True)
class BaseInputConfig:
    # Project preview values, not manufacturer-approved hardware maxima.
    max_translation_m_s: float = .2
    max_yaw_rad_s: float = .25
    translation_acceleration_m_s2: float = .4
    yaw_acceleration_rad_s2: float = .5
    deadzone: float = .1
    input_timeout_s: float = .15
    max_tick_s: float = .04
    vendor_mapping: bool = False
    recover_with_neutral: bool = False

    def __post_init__(self):
        for name, value in vars(self).items():
            if name in ('vendor_mapping', 'recover_with_neutral'):
                if type(value) is not bool:
                    raise ValueError(name+' must be bool')
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(name+' must be finite numeric')
            if name == 'deadzone':
                if not 0 <= value < 1:
                    raise ValueError('deadzone must be in [0, 1)')
            elif value <= 0:
                raise ValueError(name+' must be positive')
        if self.max_tick_s > self.input_timeout_s:
            raise ValueError('max_tick_s cannot exceed input_timeout_s')


SPEED_PROFILES=('vendor_vr','vendor_handheld')


def base_speed_limits(profile):
    if profile=='vendor_vr': return (.4,.4,.25)
    if profile=='vendor_handheld': return (.8,.8,.5)
    raise ValueError('unknown base speed profile: '+str(profile))


def vendor_base_config(profile='vendor_vr'):
    """Audited VR or handheld axes; factory MPC supplies reference smoothing.

    Both profiles clip axes separately. VR suppresses all components <= 0.01;
    handheld uses the audited raw-axis and scaled-output 0.05 thresholds.
    """
    limits=base_speed_limits(profile)
    return BaseInputConfig(max_translation_m_s=limits[0], max_yaw_rad_s=limits[2],
                           deadzone=.05 if profile=='vendor_handheld' else 0.,
                           input_timeout_s=.1, vendor_mapping=True, recover_with_neutral=True)


@dataclass(frozen=True)
class BaseMappingResult:
    intent: BaseVelocityIntent
    requested_velocity: tuple
    status: str
    reason: str


class MobileBaseMapper:
    def __init__(self, config=None):
        self.config = config if config is not None else BaseInputConfig()
        if not isinstance(self.config, BaseInputConfig):
            raise ValueError('config must be BaseInputConfig')
        self._enabled = False
        self._closed = False
        self._ready = False
        self._combo = False
        self._last_tick = None
        self._last_fresh = None
        self._last_sequence = None
        self._last_device = None
        self._source = None
        self._target = (0., 0., 0.)
        self._velocity = (0., 0., 0.)
        self._resume_pending = False
        self._neutral_since = None
        self._pause_count = 0
        self._lock_reason = 'initial lock; release X/A and center sticks before unlock'

    @property
    def diagnostics(self):
        return dict(input_pause_count=self._pause_count,
                    neutral_recovery_pending=self._resume_pending,
                    lock_reason=None if self._enabled else self._lock_reason)

    def _result(self, now, reason):
        intent = BaseVelocityIntent(now, *self._velocity, self._enabled)
        status = 'closed' if self._closed else ('unlocked' if self._enabled else 'locked')
        if self._resume_pending:
            status = 'waiting_neutral'
        if not self._enabled and not self._closed:
            reason = self._lock_reason
        return BaseMappingResult(intent, self._target, status, reason)

    def _lock(self, reason='authority revoked; explicit X+A required'):
        self._enabled = False
        self._ready = False
        self._combo = False
        self._target = self._velocity = (0., 0., 0.)
        self._resume_pending = False
        self._neutral_since = None
        self._lock_reason = reason

    def _expire_input(self):
        # Never continue an expired velocity. Only a new, sustained neutral
        # gesture can recover an already-activated vendor base session.
        resumable = self.config.recover_with_neutral and (self._enabled or self._resume_pending)
        if resumable and not self._resume_pending:
            self._pause_count += 1
        self._lock('input timeout; center sticks and release X/A for 100 ms to continue'
                   if resumable else 'input timeout; release buttons and neutralize sticks before unlock')
        self._resume_pending = resumable

    def pause_for_input_timeout(self, *, now_ns):
        """Final-send expiry follows the same recovery as loop-time expiry."""
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError('now_ns must be a nonnegative integer')
        if not self._closed:
            self._expire_input()
        return self._result(now_ns, 'input timeout before final send')

    def _map_axes(self, f):
        x, y = f.left_controller.axis
        if self.config.vendor_mapping:
            turn=f.right_controller.axis[0]
            dz=self.config.deadzone
            if dz:
                x=0. if abs(x)<dz else x
                y=0. if abs(y)<dz else y
                turn=0. if abs(turn)<dz else turn
            values = (y*self.config.max_translation_m_s,
                      -x*self.config.max_translation_m_s,
                      -turn*self.config.max_yaw_rad_s)
            if dz: return values if any(abs(v)>=dz for v in values) else (0.,0.,0.)
            return values if any(abs(v) > .01 for v in values) else (0., 0., 0.)
        magnitude = math.hypot(x, y)
        dz = self.config.deadzone
        if magnitude <= dz:
            vx = vy = 0.
        else:
            speed = (min(magnitude, 1.)-dz)/(1.-dz)*self.config.max_translation_m_s
            vx, vy = y/magnitude*speed, -x/magnitude*speed
        turn = f.right_controller.axis[0]
        yaw = (0. if abs(turn) <= dz else
               -math.copysign((abs(turn)-dz)/(1.-dz)*self.config.max_yaw_rad_s, turn))
        return vx, vy, yaw

    def step(self, frame, *, now_ns):
        if type(now_ns) is not int or now_ns < 0:
            self._lock()
            raise ValueError('now_ns must be a nonnegative integer')
        if self._closed:
            return self._result(now_ns, 'session closed')
        previous_tick = self._last_tick
        self._last_tick = now_ns
        if previous_tick is not None and now_ns < previous_tick:
            self._lock('local clock moved backwards')
            return self._result(now_ns, 'local clock moved backwards')
        dt = 0. if previous_tick is None else min((now_ns-previous_tick)*1e-9, self.config.max_tick_s)
        timeout_ns = int(self.config.input_timeout_s*1e9)
        expired = self._last_fresh is not None and now_ns-self._last_fresh >= timeout_ns
        reason = 'waiting for fresh input'
        if frame is not None:
            if not isinstance(frame, XRFrame):
                self._lock('invalid XRFrame')
                return self._result(now_ns, 'invalid XRFrame')
            age = now_ns-frame.received_monotonic_ns
            if frame.stale or age < 0 or age >= timeout_ns:
                self._lock('stale or future input')
                return self._result(now_ns, 'stale or future input')
            changed_source = self._source is not None and self._source != frame.source
            backwards = ((self._last_sequence is not None and frame.sequence_id < self._last_sequence) or
                         (self._last_device is not None and frame.device_timestamp_ns < self._last_device))
            if changed_source or backwards:
                self._lock('input stream changed; explicit rearm required')
                self._source = frame.source
                self._last_sequence = frame.sequence_id
                self._last_device = frame.device_timestamp_ns
                self._last_fresh = frame.received_monotonic_ns
                return self._result(now_ns, 'input stream changed; explicit rearm required')
            fresh = ((self._last_sequence is None or frame.sequence_id > self._last_sequence) and
                     (self._last_device is None or frame.device_timestamp_ns > self._last_device))
            # A new valid sample can cover the previous lease expiry. Check it
            # before revoking authority; do not treat duplicate or later samples
            # as proof of continuous validity. An already observed gap stays paused.
            overlaps = fresh and self._last_fresh is not None and \
                frame.received_monotonic_ns <= self._last_fresh+timeout_ns
            if expired and not overlaps:
                self._expire_input()
            if fresh:
                self._source = frame.source
                self._last_sequence = frame.sequence_id
                self._last_device = frame.device_timestamp_ns
                self._last_fresh = frame.received_monotonic_ns
                left = frame.left_controller.buttons.primary
                right = frame.right_controller.buttons.primary
                combo = left and right
                target = self._map_axes(frame)
                neutral = not any(target)
                if self._resume_pending:
                    if combo and not self._combo:
                        self._lock('operator locked base channel')
                    elif neutral and not left and not right:
                        if self._neutral_since is None:
                            self._neutral_since = frame.received_monotonic_ns
                        if frame.received_monotonic_ns-self._neutral_since >= 100_000_000:
                            self._resume_pending = False
                            self._neutral_since = None
                            self._enabled = True
                            reason = 'fresh neutral input restored base channel'
                    else:
                        self._neutral_since = None
                    self._combo = combo
                    return self._result(now_ns, reason)
                if not left and not right and neutral:
                    self._ready = True
                if combo and not self._combo:
                    if self._enabled:
                        self._lock('operator locked base channel')
                        reason = 'operator locked base channel'
                    elif self._ready and neutral:
                        self._enabled = True
                        self._ready = False
                        reason = 'operator unlocked base channel'
                    else:
                        reason = 'unlock blocked: release X/A and center sticks first'
                        self._lock_reason = reason
                else:
                    reason = 'stick centered' if neutral else 'latest stick target'
                self._combo = combo
                self._target = target if self._enabled else (0., 0., 0.)
        elif expired:
            self._expire_input()
        if self._enabled and self.config.vendor_mapping:
            self._velocity = self._target
        elif self._enabled:
            x, y, w = self._velocity
            tx, ty, tw = self._target
            dx, dy = tx-x, ty-y
            delta = math.hypot(dx, dy)
            scale = min(1., self.config.translation_acceleration_m_s2*dt/delta) if delta else 1.
            dw = max(-self.config.yaw_acceleration_rad_s2*dt,
                     min(self.config.yaw_acceleration_rad_s2*dt, tw-w))
            self._velocity = (x+dx*scale, y+dy*scale, w+dw)
        return self._result(now_ns, reason)

    def revoke(self):
        """Discard authority; fresh released/neutral input must precede rearm."""
        self._lock()

    def close(self, *, now_ns):
        self._lock()
        self._closed = True
        return self._result(now_ns, 'session closed; zero intent only, physical stop unverified')
