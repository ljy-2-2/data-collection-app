"""Pause the injected base owner before the existing arm/gripper return.

No hardware imports or second command owner. A quiet report is observation of
software feedback, not a certified physical stop. The base stays locally locked
after completion or cancellation; unlocking requires a new released/neutral X.
"""
import time

from teleop.contracts.xr import XRFrame
from teleop.control.kuavo5_return_coordinator import KuavoReturnCoordinator


class MobileReturnCoordinator(KuavoReturnCoordinator):
    def __init__(self, arms, grippers, targets, *, base, emit=None,
                 clock=time.monotonic_ns, timeout_s=120.):
        if not callable(getattr(base, 'request_hold', None)):
            raise ValueError('base must provide request_hold')
        if type(getattr(base, 'stop_timeout_ns', None)) is not int or base.stop_timeout_ns <= 0:
            raise ValueError('base must provide a positive stop_timeout_ns')
        self.base = base
        self._latest_frame = None
        self._waiting_base_stop = False
        self._base_request_ns = None
        self._base_deadline_ns = None
        self._terminal_hold_requested = False
        super().__init__(arms, grippers, targets, emit=emit, clock=clock, timeout_s=timeout_s)

    def _fresh(self, frame):
        return (isinstance(frame, XRFrame) and not frame.stale and
                0 <= self.clock()-frame.received_monotonic_ns < 100_000_000)

    def observe(self, frame, *, allowed=True):
        if not allowed or not self._fresh(frame):
            self._latest_frame = None
            self.pause('return cancelled: robot guard or input is not ready')
            return
        changed_source = self._source is not None and frame.source != self._source
        backwards = frame.sequence_id < self._sequence or frame.device_timestamp_ns < self._device
        if changed_source or backwards:
            self._latest_frame = None
            self.pause('return cancelled: input source changed or sequence moved backwards')
            self._sequence, self._device, self._source = (
                frame.sequence_id, frame.device_timestamp_ns, frame.source)
            return
        if frame.sequence_id <= self._sequence or frame.device_timestamp_ns <= self._device:
            return
        if self._active and self._latest_frame is not None and \
                frame.received_monotonic_ns-self._latest_frame.received_monotonic_ns > 100_000_000:
            self.pause('return cancelled: input gap before new packet')
            self._latest_frame = None
            self._sequence, self._device, self._source = (
                frame.sequence_id, frame.device_timestamp_ns, frame.source)
            return
        self._latest_frame = frame
        # The parent consumes held/released Y and Grip cancellation. Its request
        # callback below only starts the base pause, never the arm return yet.
        super().observe(frame, allowed=allowed)

    def request(self, frame, *, allowed=True):
        if not allowed or not self._fresh(frame):
            self._event('return_refused', 'return requires fresh input and ready robot guard')
            return
        if self._active:
            self._event('return_refused', 'return already in progress')
            return
        self._request_sequence = frame.sequence_id
        if any(getattr(frame, side+'_controller').grip > .05 for side in ('left', 'right')):
            self._event('return_refused', 'release both arm Grips before pressing Y')
            return
        report = self.base.report
        if report.get('error') or report.get('status') in ('fault', 'closing', 'closed'):
            self._event('return_refused', 'base is not ready: '+str(report.get('error') or report.get('status')))
            return
        self._latest_frame = frame
        self._base_request_ns = self.clock()
        self._base_deadline_ns = self._base_request_ns+self.base.stop_timeout_ns
        self._terminal_hold_requested = False
        # This resets the observation window and revokes X release authority.
        # The caller's following base tick also inhibits mapping while active.
        self.base.request_hold('Y return requested; wait for new quiet base feedback')
        self._waiting_base_stop = self._active = True
        self._state = 'waiting_base_stop'
        self._deadline_ns = None
        self._event('return_waiting_base_stop', 'base locked; waiting for new quiet feedback before arm/gripper return')

    def _terminal_hold(self, reason):
        if not self._terminal_hold_requested:
            self.base.request_hold(reason+'; release X and center sticks before unlocking')
            self._terminal_hold_requested = True

    def cancel(self, reason):
        if self._active or self.grippers.return_status.get('active', False):
            # Do this before clearing active, including Y+X in one input frame.
            # With mapper readiness revoked, that frame cannot unlock the base.
            self._terminal_hold(reason)
        self._waiting_base_stop = False
        super().cancel(reason)

    def update(self, *, allowed=True, reason='robot guard paused', handoff_allowed=True):
        if type(handoff_allowed) is not bool:
            raise ValueError('handoff_allowed must be bool')
        if not allowed:
            self.pause(reason)
            return self.status()
        if not self._active:
            return self.status()
        if not self._fresh(self._latest_frame):
            self.pause('return cancelled: input expired while waiting or returning')
            return self.status()
        now = self.clock()
        report = self.base.report
        if report.get('error') or report.get('status') in ('fault', 'closing', 'closed'):
            self.cancel('base became unavailable: '+str(report.get('error') or report.get('status')))
            return self.status()
        observed_ns = report.get('observed_ns')
        new_report = (type(observed_ns) is int and self._base_request_ns < observed_ns <= now
                      and now-observed_ns < 200_000_000)
        stopped = new_report and report.get('stop_observed') is True
        if self._waiting_base_stop:
            if now >= self._base_deadline_ns:
                self.cancel('base stop feedback timed out before return')
                return self.status()
            if not stopped:
                return self.status()
            if any(getattr(self._latest_frame, side+'_controller').grip > .05
                   for side in ('left', 'right')):
                self._reason = 'base quiet; release both arm Grips before return can begin'
                return self.status()
            if not handoff_allowed:
                # The owner's waiting tick clears its local input on pause.
                # A still-fresh coordinator frame does not prove those owners
                # received a new input this tick. Wait for the shared loop's
                # next real observation; never replay or re-date a cached frame.
                self._reason = 'base quiet; waiting for new shared input before return'
                return self.status()
            self._waiting_base_stop = self._active = False
            self._state = 'idle'
            try:
                super().request(self._latest_frame, allowed=allowed)
            except Exception:
                self._terminal_hold('return preparation failed')
                raise
            if not self._active:
                self._terminal_hold('return was refused')
            return self.status()
        if not stopped:
            self.cancel('base quiet feedback lost during return')
            return self.status()
        super().update(allowed=allowed, reason=reason)
        if not self._active:
            self._terminal_hold('return '+self._state)
        return self.status()

    def status(self):
        result = super().status()
        result['base'] = dict(self.base.report)
        result['base_stop_requested_ns'] = self._base_request_ns
        result['base_stop_deadline_ns'] = self._base_deadline_ns
        return result
