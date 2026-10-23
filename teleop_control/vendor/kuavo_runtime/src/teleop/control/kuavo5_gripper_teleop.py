"""Trigger-controlled grippers in the Kuavo arm session, independent of Grip.

No hardware is constructed. With no injected driver this is a zero-send input
preview; physical position and enable state remain unknown, never simulated.
Live callers explicitly supply a commissioned CAN-owner driver. Trajectory
design follows TJ/Marvin gripper/motion.py; tests supply an in-memory driver.
"""

from dataclasses import asdict
import math
import time

from teleop.robots.damiao_gripper import finite


INPUT_TTL_NS = 100_000_000
# A 10 ms loop period is not a usable queue+CAN round-trip deadline. Keep a
# bounded 50 ms transport budget, still capped by the original fresh XR lease.
COMMAND_BUDGET_NS = 50_000_000
RETURN_TOLERANCE_RAD = .01
RETURN_TIMEOUT_NS = 30_000_000_000


def validate_gripper_mode(mode):
    # Configuration, explicit operator control authorization and owner identity
    # are validated by the entry/transport before constructing a live driver.
    if mode not in ('off', 'preview', 'damiao'):
        raise ValueError('unknown gripper mode')
    return mode


def ramp(position, velocity, goal, dt, speed, accel, lower, upper):
    if dt <= 0:
        return position, velocity
    goal = max(lower, min(upper, goal))
    def braking(distance):
        step = accel*dt
        return max(0., 2*accel*max(0., distance)/
                   (math.sqrt(step*step+2*accel*max(0., distance))+step))
    wanted = math.copysign(min(speed, braking(abs(goal-position))), goal-position)
    velocity = max(velocity-accel*dt, min(velocity+accel*dt, wanted))
    velocity = max(-braking(position-lower), min(braking(upper-position), velocity))
    target = max(lower, min(upper, position+velocity*dt))
    if abs(goal-target) < 1e-9 and abs(velocity) <= accel*dt:
        return goal, 0.
    return target, velocity


class KuavoGripperTeleop:
    """Bounded work per tick: latest leased XR input, independent side faults.

    observe/tick/invalidate/close have one owner (the robot loop). Driver receive
    callbacks may run on a separate thread. No blocking CAN reads or retries.
    """

    def __init__(self, driver=None, *, clock=time.monotonic_ns):
        self.driver, self.clock = driver, clock
        self.sides = tuple(driver.profiles) if driver else ('left', 'right')
        self.states = {s: dict(state='WAIT_RELEASE', reason='release trigger before takeover',
                              trigger=None, target_rad=None, target_velocity_rad_s=0.,
                              enable_after_sequence=None, enable_started_ns=None,
                              owner_target_ack_ns=None,
                              recovery_started_ns=None, recovery_until_ns=None,
                              recoverable_stops=0,
                              accepted_commands=0, deferred_sends=0) for s in self.sides}
        self.frame = None
        self.sequence = -1
        self.device = 0
        self.source = None
        self.last_tick = None
        self.closed = False
        self._needs_disable = set()
        self._generation = 0
        self._release_after_sequence = {}
        self._return_targets = {}
        self._return_capture = {}
        self._return_samples = {}
        self._return_feedback_sequence = {}
        self._return_release_sequence = {}
        self._return_handoff_after_sequence = None
        self._return = dict(active=False, state='IDLE', reason='no return requested',
                            targets={}, samples={}, started_ns=None,
                            handoff_allowed=False)

    @property
    def return_capture(self):
        """Original startup readings and any bounded inward normalization."""
        return {side: dict(record) for side, record in self._return_capture.items()}

    @property
    def return_status(self):
        """HOLDING means measured arrival; active also includes the hold lease."""
        return dict(self._return, targets=dict(self._return['targets']),
                    samples=dict(self._return_samples))

    def _return_feedback(self, side, now, *, capture=False):
        p, f = self.driver.profiles[side], self.driver.feedback(side)
        if f is None or not 0 <= now-f.received_ns < p.feedback_timeout_ms*1_000_000:
            raise ValueError(side+': fresh motor feedback required')
        for name in ('position_rad', 'velocity_rad_s', 'torque_nm', 'mos_c', 'rotor_c'):
            finite(getattr(f, name), name)
        if max(f.mos_c, f.rotor_c) >= p.max_temperature_c:
            raise ValueError(side+': motor overtemperature')
        lo, hi = p.target_bounds if capture else p.bounds
        # CAN feedback encodes position in 16 bits, even in float-command mode4.
        # A motor at an exact target endpoint can decode just outside it. Reuse
        # the existing feedback tolerance for capture, capped by soft travel.
        # This does not expand targets or runtime motion/feedback limits.
        epsilon = p.feedback_tolerance_rad if capture else 0.
        lower, upper = max(p.bounds[0], lo-epsilon), min(p.bounds[1], hi+epsilon)
        if not lower <= f.position_rad <= upper:
            raise ValueError(side+': return feedback outside normal travel; '
                             'position={:.9f}, allowed=[{:.9f}, {:.9f}] rad'.format(
                                 f.position_rad, lower, upper))
        state = self.states[side]['state']
        expected = 0 if capture or state in ('WAIT_RELEASE', 'READY') else 1
        if f.state != expected:
            raise ValueError(side+': motor enable state does not match session ownership')
        return f

    def capture_return_targets(self):
        """Read disabled startup pose; normalize within feedback tolerance.

        Stored targets stay inside target_bounds; original feedback is unchanged.
        This never enables or sends a target.
        """
        if self.closed or self.driver is None or self._needs_disable or self._return_targets:
            raise ValueError('idle live driver required to capture return targets')
        if any(s['state'] not in ('WAIT_RELEASE', 'READY', 'WAIT_FEEDBACK')
               for s in self.states.values()):
            raise ValueError('healthy disabled session required to capture return targets')
        now = self.clock()
        targets, records = {}, {}
        for side in self.sides:
            f = self._return_feedback(side, now, capture=True)
            p = self.driver.profiles[side]
            lo, hi = p.target_bounds
            target = max(lo, min(hi, f.position_rad))
            targets[side] = target
            records[side] = dict(measured_position_rad=f.position_rad,
                                 target_rad=target, adjustment_rad=target-f.position_rad,
                                 capture_tolerance_rad=p.feedback_tolerance_rad,
                                 feedback_resolution_rad=2*p.pmax/65535,
                                 received_ns=f.received_ns, sequence=f.sequence)
        self._return_capture = records
        return targets

    def request_return(self, targets):
        """Atomically validate all sides, then let tick perform bounded motion.

        Targets are explicit measured radians, not Trigger fractions. The caller
        retains the session's ordinary fresh XR lease and global motion guards.
        An enable already in flight must finish before requesting this mode.
        """
        # A completed combined return may still own one or both grippers: arm
        # use alone never requires a Trigger press. A NEW explicit request can
        # reclaim that released-for-handoff hold, but cannot overwrite work or
        # a hold whose arm coordinator has not yet declared completion.
        if self._return_targets and not (self._return['state'] == 'HOLDING' and
                                         self._return['handoff_allowed']):
            return False
        try:
            now = self.clock()
            if self.closed or self.driver is None or self._needs_disable:
                raise ValueError('return requires an available live driver with no pending disable')
            if self.frame is None or not 0 <= now-self.frame.received_monotonic_ns < INPUT_TTL_NS:
                raise ValueError('return requires fresh XR input')
            if not isinstance(targets, dict) or set(targets) != set(self.sides):
                raise ValueError('return targets must contain every configured side exactly once')
            values, feedback = {}, {}
            for side in self.sides:
                state, p = self.states[side], self.driver.profiles[side]
                if state['state'] not in ('WAIT_RELEASE', 'READY', 'ACTIVE'):
                    raise ValueError(side+': session is not ready for return')
                value = finite(targets[side], side+' return target')
                if not p.target_bounds[0] <= value <= p.target_bounds[1]:
                    raise ValueError(side+': return target outside normal target bounds')
                measured = self._return_feedback(side, now)
                speed_limit = min(p.speed_rad_s, .3)
                moving_too_fast = abs(measured.velocity_rad_s) > speed_limit+1e-12
                motion = [(measured.position_rad, measured.velocity_rad_s)]
                if state['state'] == 'ACTIVE':
                    reference_speed = finite(state['target_velocity_rad_s'], 'accepted target velocity')
                    moving_too_fast |= abs(reference_speed) > speed_limit+1e-12
                    motion.append((finite(state['target_rad'], 'accepted target'), reference_speed))
                if moving_too_fast:
                    raise ValueError(side+': gripper moving too fast for return; '
                                     'wait for grippers to stop, then press Y again')
                # A slower acceleration limit cannot instantly make a normal
                # fast trajectory stoppable near an endpoint. Reserve stopping
                # distance plus the maximum tick horizon before accepting, so
                # ramp's boundary clip need not hide an abrupt velocity change.
                lo, hi = p.bounds
                accel = min(p.accel_rad_s2, .6)
                for position, velocity in motion:
                    room = hi-position if velocity > 0 else position-lo
                    stopping_room = velocity*velocity/(2*accel)+abs(velocity)*.02
                    if not lo <= position <= hi or room+1e-12 < stopping_room:
                        raise ValueError(side+': insufficient stopping room for return acceleration; '
                                         'wait for grippers to stop, then press Y again')
                values[side], feedback[side] = value, measured
        except Exception as exc:
            # A rejected restart must not silently dismantle the prior hold or
            # alter its handoff authority. No side has been changed or sent.
            if self._return_targets:
                self._return.update(reason='return request refused: '+str(exc))
            else:
                self._return.update(active=False, state='REJECTED', reason=str(exc))
            return False
        self._return_targets = values
        self._return_samples = dict.fromkeys(self.sides, 0)
        self._return_feedback_sequence = {s: f.sequence for s, f in feedback.items()}
        self._return_release_sequence = {}
        self._return_handoff_after_sequence = None
        self._return = dict(active=True, state='RETURNING', reason='returning to measured startup targets',
                            targets=dict(values), started_ns=now, handoff_allowed=False)
        for side, state in self.states.items():
            # Preserve an owned moving reference. Resetting it would disguise
            # an instantaneous velocity step as a bounded acceleration ramp.
            if state['state'] != 'ACTIVE':
                state['target_velocity_rad_s'] = 0.
            if state['state'] == 'WAIT_RELEASE':
                state.update(state='READY', reason='explicit return accepted')
        return True

    def allow_return_handoff(self):
        """Authorize a future release/press handoff, without sending anything."""
        if self._return['state'] != 'HOLDING' or not self._return_targets:
            return False
        if not self._return['handoff_allowed']:
            self._return.update(handoff_allowed=True,
                                reason='return complete; release then press Trigger to resume')
            self._return_handoff_after_sequence = self.sequence
        return True

    def complete_return(self):
        return self.allow_return_handoff()

    def _end_return(self, state, reason):
        self._return_targets = {}
        self._return_release_sequence = {}
        self._return_handoff_after_sequence = None
        self._return.update(active=False, state=state, reason=reason, handoff_allowed=False)

    def cancel_return(self, reason='return cancelled'):
        """Revoke preset control through the existing owned disable path."""
        if self._return_targets:
            self._end_return('CANCELLED', reason)
        self.invalidate(reason)

    def _check_return_progress(self, now):
        if (not self._return_targets or self._return['handoff_allowed'] or
                self._return['state'] not in ('RETURNING', 'HOLDING')):
            return
        for side, goal in self._return_targets.items():
            if self.states[side]['state'] != 'ACTIVE':
                self._return_samples[side] = 0
                continue
            try:
                f = self._return_feedback(side, now)
            except Exception as exc:
                self._fault(side, str(exc))
                return
            if f.sequence <= self._return_feedback_sequence[side]:
                continue
            self._return_feedback_sequence[side] = f.sequence
            fresh = (f.received_ns > self._return['started_ns'] and
                     0 <= now-f.received_ns < self.driver.profiles[side].feedback_timeout_ms*1_000_000)
            state = self.states[side]
            at_goal = (fresh and state['state'] == 'ACTIVE' and f.state == 1 and
                       state['target_rad'] is not None and
                       abs(state['target_rad']-goal) <= RETURN_TOLERANCE_RAD and
                       abs(state['target_velocity_rad_s']) <= .05 and
                       abs(f.position_rad-goal) <= RETURN_TOLERANCE_RAD and
                       abs(f.velocity_rad_s) <= .05)
            self._return_samples[side] = min(2, self._return_samples[side]+1) if at_goal else 0
        if all(count >= 2 for count in self._return_samples.values()):
            self._return.update(state='HOLDING', reason='measured return complete; holding startup targets')
        elif self._return['state'] == 'HOLDING':
            self._return.update(state='RETURNING', reason='feedback moved away; confirming return again')

    def _return_goal(self, side, trigger):
        if side not in self._return_targets:
            return None
        if self._return['state'] == 'HOLDING' and self._return['handoff_allowed']:
            if trigger <= .05 and self.sequence > self._return_handoff_after_sequence:
                self._return_release_sequence[side] = self.sequence
            elif trigger > .05 and self.sequence > self._return_release_sequence.get(side, self.sequence):
                del self._return_targets[side]
                if not self._return_targets:
                    self._end_return('COMPLETED', 'return complete; Trigger control resumed')
                return None
        return self._return_targets[side]

    def observe(self, frame):
        if self.closed:
            return False
        now = self.clock()
        if frame.stale or not 0 <= now-frame.received_monotonic_ns < INPUT_TTL_NS:
            self.invalidate('input stale or future')
            return False
        if frame.sequence_id <= self.sequence or frame.device_timestamp_ns <= self.device:
            return False  # Cached input never renews a lease or authorizes takeover.
        if self.source is not None and frame.source != self.source:
            self.invalidate('input source changed; release trigger')
        for side in self.sides:
            value = finite(getattr(frame, side+'_controller').trigger, 'trigger')
            if not 0 <= value <= 1:
                self.invalidate('invalid trigger')
                return False
        self.sequence, self.device, self.source = frame.sequence_id, frame.device_timestamp_ns, frame.source
        self.frame = frame
        return True

    def invalidate(self, reason):
        if self._return_targets:
            self._end_return('FAILED', reason)
        self.frame = None
        self.last_tick = None
        self._generation += 1
        for side, state in self.states.items():
            if state['state'] in ('ACTIVE', 'ENABLING'):
                self._needs_disable.add(side)
            if state['state'] not in ('FAULT', 'WAIT_RECOVERY'):
                state.update(state='WAIT_RELEASE', reason=reason, trigger=None, target_rad=None,
                             target_velocity_rad_s=0., enable_after_sequence=None, enable_started_ns=None,
                             recovery_until_ns=None)

    def _fault(self, side, reason):
        state = self.states[side]
        if state['state'] in ('ACTIVE', 'ENABLING'):
            self._needs_disable.add(side)
        state.update(state='FAULT', reason=reason, target_rad=None, target_velocity_rad_s=0.)
        if self._return_targets:
            self.invalidate(side+': '+reason)

    def transport_failed(self, side, reason):
        """Latch an owner failure even while no controller frame is available."""
        self._fault(side, 'CAN transport: '+str(reason))

    def _disable_pending(self, now):
        if self.driver is None:
            self._needs_disable.clear()
            return
        for side in tuple(self._needs_disable):
            try:
                if self.driver.disable(side, deadline_ns=now+COMMAND_BUDGET_NS):
                    self._needs_disable.remove(side)
                else:
                    self.states[side]['deferred_sends'] += 1
            except Exception as exc:
                self.states[side].update(state='FAULT', reason='disable unconfirmed: '+str(exc))

    def tick(self, *, allowed=True, reason='robot motion guard paused'):
        if self.closed:
            return self.status()
        now = self.clock()
        if not allowed:
            self.invalidate(reason)
        if self.frame is not None and not 0 <= now-self.frame.received_monotonic_ns < INPUT_TTL_NS:
            self.invalidate('input expired; release trigger after recovery')
        if (self._return_targets and self._return['state'] == 'RETURNING' and
                now-self._return['started_ns'] >= RETURN_TIMEOUT_NS):
            self.invalidate('gripper return timed out')
        dt = 0. if self.last_tick is None else min(.02, max(0., (now-self.last_tick)/1e9))
        self.last_tick = now
        for side, state in self.states.items():
            if self.frame is not None:
                # Display current input even when control is latched off.
                state['trigger'] = getattr(self.frame, side+'_controller').trigger
            else:
                state['trigger'] = None
            if state['state'] == 'FAULT' or side in self._needs_disable or self.frame is None:
                continue
            trigger = getattr(self.frame, side+'_controller').trigger
            state['trigger'] = trigger
            if self.driver is None:
                # Preview displays operator demand only; no fictional motor feedback.
                if state['state'] == 'WAIT_RELEASE' and trigger <= .05:
                    state.update(state='READY', reason='preview: waiting for trigger press')
                elif state['state'] == 'READY' and trigger > .05:
                    state.update(state='PREVIEW', reason='input preview only; CAN bridge unavailable')
                continue
            try:
                self._step(side, state, trigger, now, dt)
                if self._return_targets and state['state'] in ('WAIT_RECOVERY', 'WAIT_FEEDBACK'):
                    self.invalidate(side+': '+state['reason'])
            except Exception as exc:
                self._fault(side, str(exc))
        self._check_return_progress(now)
        self._disable_pending(now)
        return self.status()

    def _step(self, side, state, trigger, now, dt):
        p, f = self.driver.profiles[side], self.driver.feedback(side)
        if f is None:
            if state['state'] in ('ACTIVE', 'ENABLING'):
                raise ValueError('motor feedback lost')
            state['reason'] = 'waiting for actual motor feedback'
            return
        age = now-f.received_ns
        if (age >= p.feedback_timeout_ms*1_000_000 and f.state == 0 and
                state['state'] in ('WAIT_RELEASE', 'READY', 'WAIT_FEEDBACK')):
            state.update(state='WAIT_FEEDBACK', reason='waiting for fresh disabled feedback; no control sent')
            return
        if not 0 <= age < p.feedback_timeout_ms*1_000_000:
            raise ValueError('motor feedback stale or future')
        if f.state not in (0, 1):
            raise ValueError('motor fault '+str(f.state))
        if max(f.mos_c, f.rotor_c) >= p.max_temperature_c:
            raise ValueError('motor overtemperature')
        lo, hi = sorted((p.open_rad, p.closed_rad))
        if not lo <= f.position_rad <= hi:
            raise ValueError('feedback outside calibrated travel')
        if p.position_recovery:
            lo, hi = p.bounds
            if not lo-p.feedback_tolerance_rad <= f.position_rad <= hi+p.feedback_tolerance_rad:
                raise ValueError('hard position limit; recovery not permitted')
            if state['state'] == 'WAIT_RECOVERY':
                if now-state['recovery_started_ns'] >= p.feedback_timeout_ms*1_000_000:
                    raise ValueError('recoverable stop was not confirmed in time')
                if f.state == 0 and f.received_ns > state['recovery_started_ns']:
                    self._release_after_sequence[side] = self.sequence
                    state.update(state='WAIT_RELEASE', reason='position stop confirmed; release then press trigger',
                                 recovery_until_ns=None, owner_target_ack_ns=None)
                return
            outside_soft = not lo-.005 <= f.position_rad <= hi+.005
            returning = (state['recovery_until_ns'] is not None and now < state['recovery_until_ns'])
            if not outside_soft:
                state['recovery_until_ns'] = None
            if state['state'] in ('ACTIVE', 'ENABLING') and outside_soft and not returning:
                self._needs_disable.add(side)
                state.update(state='WAIT_RECOVERY', reason='position deviation; awaiting disabled feedback',
                             recovery_started_ns=now, target_rad=None, target_velocity_rad_s=0.,
                             recoverable_stops=state['recoverable_stops']+1)
                return
        if state['state'] == 'WAIT_FEEDBACK':
            if f.state != 0:
                raise ValueError('motor already enabled by unknown owner')
            self._release_after_sequence[side] = self.sequence
            state.update(state='WAIT_RELEASE', reason='feedback recovered; release trigger again')
            return
        if state['state'] == 'WAIT_RELEASE':
            if trigger <= .05 and self.sequence > self._release_after_sequence.get(side, -1):
                state.update(state='READY', reason='waiting for trigger press')
            return
        if state['state'] == 'READY':
            if trigger <= .05 and side not in self._return_targets:
                return
            if f.state != 0:
                raise ValueError('motor already enabled by unknown owner')
            if not p.takeover_bounds[0] <= f.position_rad <= p.takeover_bounds[1]:
                if p.position_recovery:
                    self._release_after_sequence[side] = self.sequence
                    state.update(state='WAIT_RELEASE', reason='outside recovery band; reposition disabled gripper inside travel')
                    return
                raise ValueError('takeover position outside soft travel; no automatic inward move')
            # Mark the possible actuation before invoking an external callback;
            # an exception has an unknown outcome and must request revocation.
            state.update(state='ENABLING', reason='waiting for fresh enable feedback',
                         target_rad=f.position_rad, enable_after_sequence=f.sequence,
                         enable_started_ns=now, target_velocity_rad_s=0., owner_target_ack_ns=None,
                         recovery_until_ns=(now+300_000_000 if p.position_recovery and
                             not p.bounds[0]-.005 <= f.position_rad <= p.bounds[1]+.005 else None))
            if not self.driver.enable(side, f.position_rad, deadline_ns=min(
                    now+COMMAND_BUDGET_NS, self.frame.received_monotonic_ns+INPUT_TTL_NS)):
                state.update(state='READY', target_rad=None, enable_started_ns=None,
                             enable_after_sequence=None, reason='waiting for CAN send slot')
                state['deferred_sends'] += 1
            return
        if state['state'] == 'ENABLING':
            if now-state['enable_started_ns'] >= p.feedback_timeout_ms*1_000_000:
                raise ValueError('enable acknowledgement timeout')
            if f.sequence > state['enable_after_sequence'] and f.received_ns > state['enable_started_ns'] and f.state == 1:
                state.update(state='ACTIVE', reason='trigger controls closure independently of Grip')
            return
        if f.state != 1:
            raise ValueError('motor unexpectedly disabled')
        # Rebase only on a NEW successful wire ACK. A queued sample may have
        # been rate-limited or dropped by transport; integrating from queue
        # acceptance alone accumulates an artificial tracking/torque error.
        # This is a target ACK, never evidence of physical motor movement.
        ack = self.driver.acknowledged_target(side)
        if ack is not None and ack[1] != state['owner_target_ack_ns']:
            state.update(target_rad=ack[0], owner_target_ack_ns=ack[1])
        preset = self._return_goal(side, trigger)
        speed = min(p.speed_rad_s, .3) if preset is not None else p.speed_rad_s
        accel = min(p.accel_rad_s2, .6) if preset is not None else p.accel_rad_s2
        target, velocity = ramp(state['target_rad'], state['target_velocity_rad_s'],
                                p.target(trigger) if preset is None else preset,
                                dt, speed, accel, *p.bounds)
        if self.driver.position(side, target, deadline_ns=min(
                now+COMMAND_BUDGET_NS, self.frame.received_monotonic_ns+INPUT_TTL_NS,
                f.received_ns+p.feedback_timeout_ms*1_000_000)):
            state.update(target_rad=target, target_velocity_rad_s=velocity,
                         accepted_commands=state['accepted_commands']+1)
        else:
            state['deferred_sends'] += 1  # Do not advance a trajectory that was not accepted.

    def status(self):
        observed_ns = self.clock()
        motors = {}
        for side, state in self.states.items():
            f = self.driver.feedback(side) if self.driver else None
            motors[side] = dict(state, feedback=asdict(f) if f else None)
            if self.driver:
                p = self.driver.profiles[side]
                motors[side].update(control_mode=p.control_mode,
                    feedback_timeout_ms=p.feedback_timeout_ms,
                    open_rad=p.open_rad, closed_rad=p.closed_rad, margin_rad=p.margin_rad,
                    soft_lower_rad=p.bounds[0], soft_upper_rad=p.bounds[1],
                    target_lower_rad=p.target_bounds[0], target_upper_rad=p.target_bounds[1],
                    target_reserve_rad=p.target_reserve_rad,
                    open_target_reserve_rad=p.endpoint_reserves[0],
                    closed_target_reserve_rad=p.endpoint_reserves[1],
                    working_open_rad=p.target(0.), working_closed_rad=p.target(1.),
                    position_recovery=p.position_recovery,
                    feedback_tolerance_rad=p.feedback_tolerance_rad,
                    max_torque_nm=p.max_torque_nm, mit_kp=p.kp, mit_kd=p.kd,
                    desired_closure_fraction=state['trigger'],
                    target_closure_fraction=((state['target_rad']-p.open_rad)/(p.closed_rad-p.open_rad)
                                             if state['target_rad'] is not None else None),
                    feedback_age_ms=((observed_ns-f.received_ns)/1e6 if f else None),
                    feedback_count=f.sequence if f else 0,
                    actual_feedback_hz=self.driver.observed_hz(side),
                    dropped_feedback=self.driver.dropped_feedback[side])
                motors[side]['effective_current_limit_a'] = p.effective_current_limit_a
                motors[side]['measured_closure_fraction'] = (
                    (f.position_rad-p.open_rad)/(p.closed_rad-p.open_rad) if f else None)
        return dict(mode='injected_driver' if self.driver else 'preview', motors=motors,
                    return_status=self.return_status,
                    return_capture=self.return_capture,
                    observed_ns=observed_ns,
                    closed=self.closed, disable_pending=sorted(self._needs_disable),
                    physical_execution_verified=False, generation=self._generation)

    def close(self):
        if not self.closed:
            self.invalidate('teleoperation session closed')
            self._disable_pending(self.clock())
            self.closed = True
        return self.status()
