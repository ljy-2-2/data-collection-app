"""One fresh PICO button event coordinates the existing arm/gripper owners.

No devices are discovered or created here. There is no second ROS/CAN sender.
Inputs must already have passed the robot-issued lease. This extra edge fence
prevents held Y, B+Y and reconnects from becoming new return requests.
"""
import math
import time


class KuavoReturnCoordinator:
    def __init__(self, arms, grippers, gripper_targets, *, emit=None,
                 clock=time.monotonic_ns, timeout_s=120.):
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 5 <= timeout_s <= 120:
            raise ValueError('bounded return timeout required')
        self.arms, self.grippers = arms, grippers
        self.targets = dict(gripper_targets)
        self._emit = emit or (lambda packet: None)
        self.clock = clock
        self._active = False
        self._y_armed = False
        self._sequence = -1
        self._device = 0
        self._source = None
        self._state, self._reason = 'idle', 'release Y, then press Y to return'
        self._request_sequence = None
        self._deadline_ns = None
        self._timeout_ns = int(timeout_s*1e9)

    def _event(self, event, reason):
        self._reason = reason
        self._emit(dict(event=event, reason=reason,
                        request_sequence=self._request_sequence,
                        observed_ns=self.clock(), return_home=self.status()))

    def observe(self, frame, *, allowed=True):
        now = self.clock()
        if not allowed or frame.stale \
                or not 0 <= now-frame.received_monotonic_ns < 100_000_000:
            self.pause('return cancelled: robot guard or input is not ready')
            return
        if frame.sequence_id <= self._sequence or frame.device_timestamp_ns <= self._device:
            return
        if self._source is not None and frame.source != self._source:
            self.pause('return cancelled: input source changed')
        self._sequence, self._device, self._source = (
            frame.sequence_id, frame.device_timestamp_ns, frame.source)
        y = frame.left_controller.buttons.secondary
        b = frame.right_controller.buttons.secondary
        # A held cancel consumes this Y press too. Releasing B while Y remains
        # held must not start a return. Y must be released in a later frame.
        if b:
            self._y_armed = False
            self.cancel('operator pressed B')
            return
        # Match KuavoLiveController's deliberate Grip-cancel threshold. A
        # light resting squeeze (.17-.20 in the recorded trials) is not a
        # takeover request. Starting a return still requires release below .05;
        # B cancels immediately at any Grip value.
        if self._active and any(getattr(frame, side+'_controller').grip > .90
                                for side in ('left', 'right')):
            self._y_armed = False
            self.cancel('operator squeezed arm Grip during return')
            return
        if not y:
            self._y_armed = True
            return
        if not self._y_armed:
            return
        self._y_armed = False
        self.request(frame, allowed=allowed)

    def request(self, frame, *, allowed=True):
        """Shared request path for a button edge or an explicit one-shot command.

        The caller must validate its transport lease first. This method never
        refreshes input timestamps or publishes a motor target.
        """
        now = self.clock()
        if not allowed or frame.stale or not 0 <= now-frame.received_monotonic_ns < 100_000_000:
            self._event('return_refused', 'return requires fresh input and ready robot guard')
            return
        if self._active:
            self._event('return_refused', 'return already in progress')
            return
        self._request_sequence = frame.sequence_id
        if any(getattr(frame, side+'_controller').grip > .05 for side in ('left', 'right')):
            self._event('return_refused', 'release both arm Grips before pressing Y')
            return
        # Neither request method publishes. Both components are prepared before
        # the robot loop invokes either tick. A failed second request therefore
        # cancels the pending first one before its first return publication.
        if not self.arms.request_return():
            self._event('return_refused', self.arms.return_status.get('reason', 'arms not ready'))
            return
        try:
            accepted = self.grippers.request_return(self.targets)
        except Exception:
            self.arms.cancel_return('gripper return preparation failed')
            raise
        if not accepted:
            self.arms.cancel_return('gripper return not ready')
            self._event('return_refused', self.grippers.return_status.get('reason', 'grippers not ready'))
            return
        self._active, self._state = True, 'preparing'
        self._deadline_ns = now+self._timeout_ns
        self._event('return_started', 'return accepted; waiting for fresh readiness and bounded execution')

    def cancel(self, reason):
        if not self._active and not self.grippers.return_status.get('active', False):
            return
        try:
            self.arms.cancel_return(reason)
            # Arms may already have completed while grippers are still moving.
            # Fence any normal IK work observed in this same cancellation frame.
            self.arms.invalidate(reason+'; release both Grips before takeover')
        finally:
            self.grippers.cancel_return(reason)
        self._active, self._state = False, 'cancelled'
        self._event('return_cancelled', reason+'; cancellation requested, physical stop not independently verified')

    def pause(self, reason):
        self._y_armed = False
        self.cancel(reason)

    def update(self, *, allowed=True, reason='robot guard paused'):
        if not allowed:
            self.pause(reason)
            return self.status()
        if not self._active:
            return self.status()
        if self.clock() >= self._deadline_ns:
            self.cancel('combined return timed out')
            return self.status()
        arms, claws = self.arms.return_status, self.grippers.return_status
        if arms['state'] in ('failed', 'cancelled') or claws['state'] in ('FAILED', 'CANCELLED'):
            why = ('arms: '+arms.get('reason', '') if arms['state'] in ('failed', 'cancelled')
                   else 'grippers: '+claws.get('reason', ''))
            self.cancel(why)
            return self.status()
        if arms['state'] == 'completed' and claws['state'] == 'HOLDING':
            if not self.grippers.allow_return_handoff():
                self.cancel('gripper handoff could not be established')
                return self.status()
            self._active, self._state = False, 'completed'
            self._event('return_completed', 'arms and grippers reached their return targets; release inputs before manual takeover')
        else:
            self._state = 'preparing' if arms['state'] == 'preparing' else 'returning'
            self._reason = 'arms: '+arms['state']+'; grippers: '+claws['state']
        return self.status()

    def status(self):
        return dict(state=self._state, active=self._active, reason=self._reason,
                    arms=dict(self.arms.return_status), grippers=dict(self.grippers.return_status),
                    gripper_initial_targets_rad=dict(self.targets),
                    physical_stop_verified=False, collision_avoidance=False)
