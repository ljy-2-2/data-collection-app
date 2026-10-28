"""Independent chassis session; injected transport, no hardware imports.

    A zero request and observed quiet odometry are separate facts. Neither
    certifies a physical stop or substitutes for the manufacturer's emergency stop.
"""
from dataclasses import dataclass, replace
import math
import time

from teleop.retargeting.mobile_base import MobileBaseMapper, vendor_base_config


class InputDeadlineExpired(ValueError):
    """Transport rejected a nonzero target before publishing anything."""
    def __init__(self, observed_ns):
        if type(observed_ns) is not int or observed_ns < 0:
            raise ValueError('invalid expiry observation time')
        self.observed_ns=observed_ns
        super().__init__('input lease expired before final publication')


@dataclass(frozen=True)
class BaseFeedback:
    observed_ns: int
    vx: float
    vy: float
    wz: float

    def __post_init__(self):
        if type(self.observed_ns) is not int or self.observed_ns < 0:
            raise ValueError('invalid feedback timestamp')
        for value in (self.vx,self.vy,self.wz):
            if type(value) not in (int,float) or not math.isfinite(value):
                raise ValueError('invalid feedback velocity')


@dataclass(frozen=True)
class BaseHealth:
    observed_ns: int
    control_enabled: bool
    velocity_enabled: bool
    mpc_enabled: bool
    error: str = ''
    downstream_quiet: bool = False
    downstream_observed_ns: int = None


class BaseSession:
    """50 Hz owner loop; zero on expired input/fault/exit, no mode services."""
    def __init__(self, *, publish, speed_profile='vendor_vr', toggle_mode='x_a',
                 recover_with_neutral=None, clock=time.monotonic_ns):
        self.publish=publish
        config=vendor_base_config(speed_profile, toggle_mode=toggle_mode)
        if recover_with_neutral is not None:
            if type(recover_with_neutral) is not bool:
                raise ValueError('recover_with_neutral must be bool')
            config=replace(config,recover_with_neutral=recover_with_neutral)
        self.mapper=MobileBaseMapper(config)
        self.clock=clock
        self._hold_since=None
        self._hold_reason=''
        self.report=dict(stop_observed=False,observed_ns=0,status='starting',error=None)
        # Observation grace only; does not change vendor braking or input TTL.
        self.stop_timeout_ns=3_000_000_000 if speed_profile=='vendor_handheld' else 2_000_000_000
        self.error=None
        self.started=False
        self.closing=False
        self.closed=False
        self.close_reason=''
        self._quiet_since=None
        self._last_now=None
        self._last_output=(0.,0.,0.)
        self._stop_since=None
        self._close_since=None
        self._last_feedback_ns=None
        self.publish_attempts=0
        self.publish_enqueued=0

    def fail(self, reason):
        self.error=self.error or str(reason)
        self.mapper.revoke()

    def request_close(self, reason):
        self.closing=True
        self.close_reason=str(reason)
        self.mapper.revoke()

    def request_hold(self, reason):
        """Revoke sticks and start a NEW stop-observation window; no I/O here."""
        self.mapper.revoke()
        self._hold_since=self.clock()
        self._stop_since=self._hold_since
        self._hold_reason=str(reason)
        self._quiet_since=None
        self.report=dict(self.report,stop_observed=False,hold_requested_ns=self._hold_since)

    def tick(self, frame, *, now_ns, health, feedback, inhibit=False):
        if type(inhibit) is not bool:
            raise ValueError('inhibit must be bool')
        if type(now_ns) is not int or now_ns < 0:
            self.fail('invalid local time')
            raise ValueError('invalid local time')
        if self._last_now is not None and now_ns < self._last_now:
            self.fail('local clock moved backwards')
        self._last_now=now_ns
        if not isinstance(health,BaseHealth) or type(health.observed_ns) is not int \
                or not 0 <= now_ns-health.observed_ns < 500_000_000:
            self.fail('control graph stale or unavailable')
        elif health.error:
            self.fail(health.error)
        elif any(v is not True for v in (health.control_enabled,health.velocity_enabled,health.mpc_enabled)):
            self.fail('manufacturer control/velocity/MPC mode not ready; no automatic mode change')
        feedback_valid=isinstance(feedback,BaseFeedback) and 0 <= now_ns-feedback.observed_ns < 200_000_000
        if feedback_valid and self._last_feedback_ns is not None and feedback.observed_ns < self._last_feedback_ns:
            feedback_valid=False
        if not feedback_valid:
            self._quiet_since=None
            self.fail('odometry stale, invalid or reordered')
        else:
            self._last_feedback_ns=feedback.observed_ns
            quiet=(max(abs(feedback.vx),abs(feedback.vy)) <= .01 and abs(feedback.wz) <= .02
                   and isinstance(health,BaseHealth) and health.downstream_quiet is True)
            # A delayed sample from before the stop request cannot start the
            # subsequent quiet window, even if its receive time is recent.
            stop_boundary=max(v for v in (0,self._stop_since,self._close_since,self._hold_since) if v is not None)
            downstream_after_hold=(self._hold_since is None or
                (isinstance(health,BaseHealth) and type(health.downstream_observed_ns) is int
                 and health.downstream_observed_ns>=stop_boundary))
            if quiet and feedback.observed_ns>=stop_boundary and downstream_after_hold:
                if self._quiet_since is None: self._quiet_since=feedback.observed_ns
            else:
                self._quiet_since=None
        quiet_span=(feedback.observed_ns-self._quiet_since if feedback_valid and self._quiet_since is not None else 0)
        if not self.started and not self.error and not self.closing and not self.closed and quiet_span >= 500_000_000:
            self.started=True
        if self.closed:
            output=(0.,0.,0.); status='closed'; reason=self.close_reason
        elif self.error or self.closing or not self.started or inhibit:
            self.mapper.revoke()
            output=(0.,0.,0.)
            status='fault' if self.error else ('closing' if self.closing else ('locked' if inhibit else 'starting'))
            reason=self.error or self.close_reason or (self._hold_reason if inhibit else 'waiting for 0.5 s quiet odometry')
        else:
            result=self.mapper.step(frame,now_ns=now_ns)
            output=result.intent.velocity; status=result.status; reason=result.reason
        if any(output):
            self._stop_since=None
            self._hold_since=None
        elif any(self._last_output):
            self._stop_since=now_ns
            self._quiet_since=None
            quiet_span=0
        elif self.started and feedback_valid and not quiet and self._stop_since is None:
            # Also watch motion that appears while we already request zero.
            self._stop_since=now_ns
        if self.closing and self._close_since is None:
            self._close_since=now_ns
            self._quiet_since=None
            quiet_span=0
        stop_observed=not any(output) and feedback_valid and quiet_span >= 300_000_000
        if stop_observed: self._stop_since=None
        if self._stop_since is not None and now_ns-self._stop_since >= self.stop_timeout_ns and not stop_observed:
            self.fail('stop feedback not quiet within configured timeout; physical stop unverified')
            status='fault'; reason=self.error
        if self.started and not self.closed:
            self.publish_attempts+=1
            try:
                self.publish(output)
                self.publish_enqueued+=1
            except InputDeadlineExpired as exc:
                # This is a stale input, not a broken publisher. Revoke that
                # velocity immediately without closing an otherwise healthy
                # session. Recovery still needs fresh neutral input.
                result=self.mapper.pause_for_input_timeout(now_ns=max(now_ns,exc.observed_ns))
                output=(0.,0.,0.); status=result.status; reason=result.reason
                self._stop_since=max(now_ns,exc.observed_ns)
                self._quiet_since=None; stop_observed=False
                self.publish_attempts+=1
                try:
                    self.publish(output)
                    self.publish_enqueued+=1
                except Exception as zero_error:
                    self.fail('publisher failed after expired input: '+str(zero_error))
                    status='fault'; reason=self.error
            except Exception as exc:
                self.fail('publisher failed: '+str(exc))
                status='fault'; reason=self.error
        self._last_output=output
        if self.closing and (stop_observed or now_ns-self._close_since >= self.stop_timeout_ns):
            self.closed=True
            status='closed'
        self.report=dict(event='base_status',status=status,state=status.upper(),reason=reason,
            observed_ns=now_ns,hold_requested_ns=self._hold_since,
            enabled=status=='unlocked',velocity=list(output),
            stop_observed=bool(stop_observed),physical_stop_verified=False,
            downstream_quiet=isinstance(health,BaseHealth) and health.downstream_quiet is True,
            publish_attempts=self.publish_attempts,publish_enqueued=self.publish_enqueued,
            error=self.error,input_recovery=self.mapper.diagnostics)
        return self.report
