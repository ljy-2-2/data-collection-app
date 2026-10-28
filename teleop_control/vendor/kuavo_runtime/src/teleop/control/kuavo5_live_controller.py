"""Robot-side composition. One control owner, one serial, bounded-slot IK worker.

No ROS/device discovery is performed here. All callbacks are injected. Closing
cannot cancel a vendor call already in progress; late results cannot publish.
"""

from dataclasses import dataclass, replace
import math
from queue import Empty, Queue
import threading
import time

from teleop.contracts.command import JointCommand
from teleop.control.kuavo5_arm_execution_session import (
    ArmExecutionSession, ExecutionResult, GripSample, ReturnSample)
from teleop.robots.kuavo5_native_ik import (
    ARM_NAMES, IKCandidateRejected, IKCandidateLimitRejected,
    IKTargetUnreachable, IKPoseResidualRejected)
from teleop.robots.kuavo5_diagnostics import control_snapshot, poses as diagnostic_poses


@dataclass(frozen=True)
class ArmSolveWork:
    frame: object
    feedback: object
    epoch: int
    retarget_epoch: int
    clutch_generations: tuple
    started_ns: int
    ik_seed_rad: object = None


class KuavoLiveController:
    def __init__(self, schema, transport, *, solve, clock=time.monotonic_ns,
                 control_period_s=.02, diagnostic=None,
                 max_velocity_rad_s=.6, max_acceleration_rad_s2=None,
                 reuse_valid_solution_seed=False, tracking_governor=None,
                 reference_sync="all_joints", return_config=None,
                 joint_velocity_limits=None, joint_acceleration_limits=None):
        if type(reuse_valid_solution_seed) is not bool:
            raise ValueError('reuse_valid_solution_seed must be bool')
        if diagnostic is not None and not callable(diagnostic):
            raise ValueError('diagnostic must be an explicit nonblocking callback')
        self._return = None
        if return_config is not None:
            from teleop.control.kuavo5_arm_return import ArmReturnState
            self._return = ArmReturnState(schema, return_config)
        self._diagnostic = diagnostic
        self._latest_feedback = None
        self._candidate_source = None
        self._recovery_trigger = None
        self._reuse_valid_solution_seed = reuse_valid_solution_seed
        self._seed_history = {}
        self._pending_seed = None
        self.transport, self._clock = transport, clock
        self._session = ArmExecutionSession(schema, publish=self._publish,
            hold=lambda _: transport.release(), period_s=control_period_s,
            max_velocity_rad_s=max_velocity_rad_s,
            max_acceleration_rad_s2=max_acceleration_rad_s2,
            max_offset_rad=None, recover_unreachable=True,
            tracking_governor=tracking_governor, reference_sync=reference_sync,
            joint_velocity_limits=joint_velocity_limits, joint_acceleration_limits=joint_acceleration_limits)
        self._solve = solve
        self._requests, self._results = Queue(maxsize=1), Queue(maxsize=1)
        self._closed = threading.Event()
        self._frame = None
        self._sequence = -1
        self._device = 0
        self._sides = ()
        self._clutch_generations = {'left': 0, 'right': 0}
        self._feedback_ns = -1
        self._publication_sequence = 0
        self._synchronized_wire_sequence = 0
        self._inflight = False
        self._solve_deadline = None
        self._hold_stamp = None
        self._output_baseline_stamp = None
        self._resume_pending = False
        self._last_release_frame_ns = None
        self._last_press_frame_ns = None
        self._arm_release_frame_ns = None
        # One explicit release/re-press during an ordinary handover may wait
        # for its own hold acknowledgement. Fault/pause recovery cannot use it.
        self._operator_release_token = None
        self._worker = threading.Thread(target=self._work, name='kuavo-official-ik', daemon=True)
        self._worker.start()

    @property
    def tracking_diagnostics(self):
        return self._session.tracking_diagnostics

    @property
    def return_status(self):
        return (dict(enabled=False, active=False, state='disabled',
                     reason='fixed return was not configured')
                if self._return is None else self._return.status())

    def request_return(self):
        """Request the configured target, without inventing a physical Grip.

        Called by the input owner on a new operator button edge. Preparation
        waits for any previous hold reply and newer real input and feedback.
        """
        now = self._clock()
        if self._return is None or self._closed.is_set() or self._return.active:
            return False
        frame = self._frame
        if frame is None or not 0 <= now-frame.received_monotonic_ns < 100_000_000 \
                or any(getattr(frame, side+'_controller').grip > .90 for side in ('left', 'right')):
            self._return.reason = 'return requires fresh input and both physical Grips released'
            return False
        if self.transport.status()['fault']:
            self._return.reason = 'return refused while transport is faulted'
            return False
        self._session.invalidate_input('explicit fixed return requested', now_ns=now)
        self.transport.release()
        self._operator_release_token = None
        self._seed_history.clear()
        self._pending_seed = None
        self._return.request(now)
        return True

    def _finish_return(self, state, reason, now):
        self._return.finish(state, reason, now)
        self._session.end_return(reason, now_ns=now)
        self.transport.release()
        self._operator_release_token = None
        self._seed_history.clear()
        self._pending_seed = None
        self._candidate_source = None

    def cancel_return(self, reason='operator cancelled fixed return'):
        if self._return is None or not self._return.active:
            return False
        self._finish_return('cancelled', reason, self._clock())
        return True

    def _work(self):
        while not self._closed.is_set():
            try:
                ticket, work = self._requests.get(timeout=.05)
            except Empty:
                continue
            if self._closed.is_set():
                return
            try:
                result, error = self._solve(work), None
            except Exception as exc:
                result, error = None, exc
            self._results.put((ticket, work, result, error))

    def observe(self, frame):
        now = self._clock()
        if self._closed.is_set():
            return
        if frame.stale or frame.sequence_id <= self._sequence \
                or frame.device_timestamp_ns < self._device \
                or not 0 <= now-frame.received_monotonic_ns < 100_000_000:
            self.invalidate('invalid/stale XR input')
            return
        self._sequence = frame.sequence_id
        if frame.device_timestamp_ns == self._device:
            return
        self._device = frame.device_timestamp_ns
        sides = tuple(side for side in ('left', 'right')
                      if getattr(frame, side+'_controller').grip > (.90 if side in self._sides else .95))
        if self._return is not None and self._return.active:
            self._frame, self._sides = frame, sides
            if any(getattr(frame, side+'_controller').grip > .90 for side in ('left', 'right')):
                # Preserve the release hysteresis even when the cancelling
                # press is between .90 and the ordinary .95 engage threshold.
                self._sides = tuple(side for side in ('left', 'right')
                    if getattr(frame, side+'_controller').grip > .90)
                self.cancel_return('physical Grip pressed; return cancelled, release before teleoperation')
                # This press cancels return only. It cannot also acquire arm
                # authority; a subsequent physical release is mandatory.
                return
            if self._return.state == 'preparing':
                self._session.observe_input(GripSample(frame.sequence_id,
                    frame.received_monotonic_ns, ()), now_ns=now)
            else:
                self._session.observe_return_input(ReturnSample(frame.sequence_id,
                    frame.received_monotonic_ns), now_ns=now)
                if self._session.requires_release:
                    self._finish_return('failed', 'return input lease was lost', now)
                    return
            self.transport.refresh_input(sequence_id=frame.sequence_id,
                input_deadline_ns=frame.received_monotonic_ns+100_000_000)
            return
        if self._sides and not sides:
            self._last_release_frame_ns = frame.received_monotonic_ns
        elif not self._sides and sides:
            self._last_press_frame_ns = frame.received_monotonic_ns
        epoch = self._session.epoch
        previous_sides = self._sides
        transport_before = self.transport.status()
        normal_release = (bool(previous_sides) and not sides
            and not self._session.requires_release
            and self._frame is not None
            and 0 <= now-self._frame.received_monotonic_ns < 100_000_000
            and transport_before['mode'] == 'external'
            and transport_before['desired_mode'] == 2
            and not transport_before['fault'])
        for side in ('left', 'right'):
            if (side in previous_sides) != (side in sides):
                self._clutch_generations[side] += 1
        self._session.observe_input(GripSample(frame.sequence_id, frame.received_monotonic_ns, sides), now_ns=now)
        if self._session.requires_release or self._session.last_input_sequence_id != frame.sequence_id:
            self._operator_release_token = None
        elif normal_release:
            self._operator_release_token = frame.received_monotonic_ns
        continuous_side_change = bool(previous_sides and sides
                                      and not self._session.requires_release)
        if not sides or (self._session.epoch != epoch and not continuous_side_change):
            self.transport.release()
        self._sides, self._frame = sides, frame
        self.transport.refresh_input(sequence_id=frame.sequence_id,
                                     input_deadline_ns=frame.received_monotonic_ns+100_000_000)

    def invalidate(self, reason):
        self._operator_release_token = None
        if self._return is not None and self._return.active:
            self._finish_return('failed', reason, self._clock())
        else:
            self._session.invalidate_input(reason, now_ns=self._clock())
        self.transport.release()
        self._frame = None
        self._sides = ()
        self._seed_history.clear()
        self._pending_seed = None

    def _remember_checked_seed(self, result, feedback, now):
        pending, self._pending_seed = self._pending_seed, None
        if not self._reuse_valid_solution_seed or pending is None \
                or result.status not in ('waiting_transport', 'published'):
            return
        work, candidate = pending
        if work.epoch != self._session.epoch \
                or work.retarget_epoch != self._session.retarget_epoch:
            return
        # offer_solved normalizes the accepted candidate to its original input
        # source. History must use that same origin even for a shorter TTL.
        expires = work.frame.received_monotonic_ns+min(200, candidate.ttl_ms)*1_000_000
        if not work.frame.received_monotonic_ns <= now < expires:
            return
        # The full candidate has passed the session gate at this point. A
        # numerical initial guess may lead measured joints while the bounded
        # published reference catches up. Tracking limits belong to that
        # reference, not to selection of the next optimization starting point.
        for index, side in enumerate(('left', 'right')):
            if side not in self._sides or work.clutch_generations[index] != self._clutch_generations[side]:
                continue
            names = ARM_NAMES[index*7:index*7+7]
            q = tuple(float(candidate.targets[n]) for n in names)
            self._seed_history[side] = (q, work.retarget_epoch,
                work.clutch_generations[index], work.frame.received_monotonic_ns, expires)

    def _select_seed(self, feedback, now):
        if not self._reuse_valid_solution_seed:
            return None
        seed = [float(feedback.positions[n]) for n in ARM_NAMES]
        for index, side in enumerate(('left', 'right')):
            history = self._seed_history.get(side)
            if history is None:
                continue
            q, epoch, generation, created, expires = history
            offset = index*7
            eligible = side in self._sides and epoch == self._session.retarget_epoch \
                and generation == self._clutch_generations[side] and created <= now < expires
            if eligible:
                seed[offset:offset+7] = q
            else:
                self._seed_history.pop(side, None)
        return tuple(seed)

    def _publish(self, encoded):
        now = self._clock()
        deadline = min(self._frame.received_monotonic_ns+100_000_000,
                       self._session.candidate_deadline_ns)
        if deadline <= now:
            raise ValueError('candidate/input expired before publication')
        self._publication_sequence += 1
        command = JointCommand(dict(zip(encoded.names, map(math.radians, encoded.positions_deg))),
                               'kuavo_live_bounded', self._publication_sequence, now,
                               max(1, min(100, (deadline-now)//1_000_000)))
        self.transport.submit(command, input_deadline_ns=deadline)
        if not self.transport.pump():
            raise RuntimeError('ROS sender refused target: '+str(self.transport.status()))
        completed = self._clock()
        if self._diagnostic is not None:
            feedback = self._latest_feedback
            recovery = self._session.recovery_deadline_ns is not None
            source = self._candidate_source
            if source is None:
                source = dict(sequence_id=encoded.sequence_id,
                    input_created_time_ns=self._frame.received_monotonic_ns,
                    device_timestamp_ns=self._frame.device_timestamp_ns,
                    epoch=self._session.epoch)
            self._diagnostic(control_snapshot(ARM_NAMES, epoch=source['epoch'], source=source,
                latest_frame=self._frame, publication_sequence_id=self._publication_sequence,
                started_ns=now, completed_ns=completed, command=command, feedback=feedback,
                active_sides=(('left', 'right') if self._return is not None and self._return.active else self._sides),
                reference_kind=('bounded_fixed_joint_return' if self._return is not None and self._return.active
                                else 'ik_recovery_braking_or_hold' if recovery else 'bounded_ik_candidate'),
                command_sequence_id=encoded.sequence_id,
                recovery_trigger=self._recovery_trigger if recovery else None,
                wire_evidence=self.transport.publication_evidence(),
                tracking_governor=self.tracking_diagnostics))
        return completed

    def _drain_result(self, now):
        try:
            ticket, work, candidate, error = self._results.get_nowait()
        except Empty:
            return
        self._inflight = False
        self._solve_deadline = None
        self._pending_seed = None
        if error is None:
            if self._session.offer_solved(ticket, candidate, now_ns=now):
                self._pending_seed = (work, candidate)
                self._candidate_source = dict(sequence_id=work.frame.sequence_id,
                    input_created_time_ns=work.frame.received_monotonic_ns,
                    device_timestamp_ns=work.frame.device_timestamp_ns, epoch=work.epoch)
        else:
            # A rejected pose is not a usable answer. Like explicit no-solution,
            # it may brake an established reference while seeking a new answer.
            # A finite unsent out-of-range solver vector is discarded too;
            # malformed responses, measured limits, time and transport stay hard.
            recoverable = isinstance(error, (IKTargetUnreachable,
                IKPoseResidualRejected, IKCandidateLimitRejected))
            accepted = self._session.reject_solve(ticket,
                reason=type(error).__name__+': '+str(error), now_ns=now,
                recoverable=recoverable)
            if accepted and recoverable:
                self._recovery_trigger = dict(sequence_id=work.frame.sequence_id,
                    input_created_time_ns=work.frame.received_monotonic_ns,
                    device_timestamp_ns=work.frame.device_timestamp_ns, epoch=work.epoch)
            # A canceled/old result has no authority over the current session,
            # regardless of whether it failed with an IK or transport exception.
            # Current guard/transport checks still run independently in tick().
            if accepted and not isinstance(error, IKCandidateRejected):
                self.invalidate('official compute failed: '+str(error))

    def tick(self, feedback):
        now = self._clock()
        if self._closed.is_set():
            return self._session.close()
        if feedback.timestamp_ns != self._feedback_ns:
            try:
                feedback = self.transport.update_feedback(feedback)
            except Exception as exc:
                if self._return is not None and self._return.active:
                    self._finish_return('failed', 'return feedback rejected: '+str(exc), now)
                raise
            self._feedback_ns = feedback.timestamp_ns
        elif self._latest_feedback is not None:
            # Continue with the exact normalized observation already accepted
            # by the transport, rather than reintroducing raw limit roundoff.
            feedback = self._latest_feedback
        self._latest_feedback = feedback
        self._drain_result(now)
        # Recovery is leased by the same fresh XR input deadline that is renewed
        # in observe().  Mirroring its rolling deadline into the transport's
        # one-shot absolute watchdog creates a race at each renewal boundary.
        # Keep that separate watchdog disabled; the transport input lease and
        # the session clock checks still independently stop stale input.
        self.transport.constrain_recovery_deadline(None)
        if self._inflight and now >= self._solve_deadline:
            self.invalidate('official IK exceeded source deadline')
            raise TimeoutError('official IK exceeded 200ms source deadline; late result discarded')
        status = self.transport.status()
        if status['fault']:
            self.invalidate('transport fault: '+status['fault'])
        if self._return is not None and self._return.active:
            try:
                return self._tick_return(feedback, now)
            except Exception as exc:
                if self._return.active:
                    self._finish_return('failed', 'return failed: '+str(exc), now)
                raise
        # This validation can itself request hold when a candidate expires.
        checked = self._session.tick(feedback, now_ns=now, publication_allowed=False)
        if checked.status in ('waiting_release', 'fault', 'closed'):
            self._operator_release_token = None
        if checked.status in ('waiting_candidate', 'waiting_release', 'fault', 'closed') \
                and self.transport.status()['desired_mode'] == 2 \
                and not (checked.status == 'waiting_candidate'
                         and self._session.awaiting_recoverable_target):
            # A mode request is already a side effect, even before first publish.
            # Unexplained/expired candidate loss remains a stop boundary.
            # A typed ordinary numerical miss before first output may wait in
            # the already requested mode, under fresh XR/feedback watchdogs;
            # it sends nothing until another fully valid answer arrives.
            # Other candidate loss while that request exists is a stop boundary:
            # latch until a physical Grip release instead of auto-rearming from
            # another solve result while the operator still holds the button.
            if checked.status == 'waiting_candidate':
                self._session.invalidate_input(
                    'candidate unavailable after external mode request; release Grip before retry',
                    now_ns=now)
                checked = self._session.tick(
                    feedback, now_ns=now, publication_allowed=False)
            self.transport.release()
        # Use ONE post-validation snapshot for the reply fence and transition
        # gate. Mixing snapshots can see a completed hold without its fence.
        status = self.transport.status()
        hold_stamp = status.get('hold_acknowledged_time_ns')
        if hold_stamp is not None and hold_stamp != self._hold_stamp:
            self._hold_stamp = hold_stamp
            self._resume_pending = True
            release_token, self._operator_release_token = self._operator_release_token, None
            normal_reengage = (release_token is not None
                and release_token == self._last_release_frame_ns
                and release_token <= hold_stamp
                and self._last_press_frame_ns is not None
                and self._last_press_frame_ns > release_token
                and checked.status not in ('waiting_release', 'fault', 'closed')
                and not status['fault'])
            grip_was_not_released_since_arm = (
                self._last_release_frame_ns == self._arm_release_frame_ns)
            grip_was_pressed_before_hold_reply = (
                self._last_press_frame_ns is None or self._last_press_frame_ns <= hold_stamp)
            if self._sides and checked.status != 'waiting_release' \
                    and not normal_reengage \
                    and (grip_was_not_released_since_arm
                         or grip_was_pressed_before_hold_reply):
                # Fault/pause hold still requires a new release. An explicit
                # normal release/re-press may instead wait for this reply,
                # then for newer XR/feedback below, before fresh computation.
                self._session.invalidate_input(
                    'hold acknowledged while Grip active; release Grip before retry',
                    now_ns=now)
            else:
                self._session.fence_after_hold(now_ns=now)
            checked = self._session.tick(feedback, now_ns=now, publication_allowed=False)
        waiting_hold = status['desired_mode'] == 0 and (
            status['mode_request_inflight'] is not None or status['mode_request_pending'] is not None
            or status['mode'] == 'external')
        if self._resume_pending:
            if self._frame is None or self._frame.received_monotonic_ns <= self._hold_stamp \
                    or feedback.timestamp_ns <= self._hold_stamp:
                waiting_hold = True
            elif not waiting_hold:
                # tick() may have captured an inactive-arm anchor while waiting.
                # Rebuild it only from the post-reply observation below.
                self._session.fence_after_hold(now_ns=now)
                self._resume_pending = False
                checked = self._session.tick(feedback, now_ns=now, publication_allowed=False)
        if waiting_hold:
            return (ExecutionResult('waiting_hold', 'await hold reply and newer XR/feedback')
                    if self._sides and checked.status not in ('waiting_release', 'fault', 'closed')
                    else checked)
        deadline = self._session.candidate_deadline_ns
        # No mode change before a valid, bounded-age solution is available.
        if checked.status == 'waiting_transport' and deadline is not None and now < deadline and status['desired_mode'] != 2 \
                and status['mode_request_inflight'] is None and status['mode_request_pending'] is None \
                and not status['requires_release']:
            self.transport.arm()
            self._arm_release_frame_ns = self._last_release_frame_ns
            self.transport.refresh_input(sequence_id=self._frame.sequence_id,
                input_deadline_ns=self._frame.received_monotonic_ns+100_000_000)
        result = checked
        if self.transport.status()['mode'] == 'external' and self.transport.status()['desired_mode'] == 2:
            baseline = self.transport.binding_output_baseline()
            if baseline is None:
                self.invalidate('external mode missing binding output baseline')
                return self._session.tick(feedback, now_ns=now)
            positions, published_ns = baseline
            if published_ns != self._output_baseline_stamp:
                if published_ns > now:
                    return ExecutionResult('waiting_transport',
                        'await output baseline clock')
                self._session.synchronize_output_baseline(
                    positions, published_ns=published_ns, now_ns=now)
                self._output_baseline_stamp = published_ns
            result = self._session.tick(feedback, now_ns=now)
            wire = self.transport.publication_evidence()
            if wire is not None and wire['command_sequence_id'] > self._synchronized_wire_sequence:
                # Scheduling/enqueue delays make the delivered secant velocity
                # differ from the nominal planner velocity. Continue from the
                # reference actually handed to WBC, without renewing any lease.
                self._session.synchronize_reference_velocity(
                    dict(zip(ARM_NAMES, map(math.radians, wire['positions_deg']))),
                    dict(zip(ARM_NAMES, map(math.radians, wire['velocities_deg_s']))),
                    published_ns=self._session.last_publish_time_ns)
                self._synchronized_wire_sequence = wire['command_sequence_id']
        if result.status in ('waiting_release', 'fault', 'closed'):
            self.transport.release()
        self._remember_checked_seed(result, feedback, now)
        ticket = self._session.begin_solve(now_ns=now)
        if ticket is not None:
            normalized = self._frame
            for side in ('left', 'right'):
                normalized = replace(normalized, **{side+'_controller': replace(
                    getattr(normalized, side+'_controller'), grip=1. if side in self._sides else 0.)})
            self._requests.put_nowait((ticket, ArmSolveWork(
                normalized, feedback, ticket.epoch,
                self._session.retarget_epoch,
                tuple(self._clutch_generations[side] for side in ('left', 'right')),
                now, self._select_seed(feedback, now))))
            self._inflight = True
            self._solve_deadline = ticket.sample.created_time_ns+200_000_000
        return result

    def _tick_return(self, feedback, now):
        returning = self._return
        if now < returning.started_ns or now >= returning.deadline_ns:
            self._finish_return('failed', 'return clock invalid or return timed out', now)
            return ExecutionResult('waiting_release', returning.reason)
        if self._frame is None or not 0 <= now-self._frame.received_monotonic_ns < 100_000_000:
            self._finish_return('failed', 'return XR input expired', now)
            return ExecutionResult('waiting_release', returning.reason)
        status = self.transport.status()
        if returning.state == 'preparing':
            checked = self._session.tick(feedback, now_ns=now, publication_allowed=False)
            if checked.status in ('fault', 'closed'):
                self._finish_return('failed', checked.reason, now)
                return checked
            hold_stamp = status.get('hold_acknowledged_time_ns')
            # With no prior external-mode request there is no pending hold
            # reply. The binding's independently prepared baseline still gates
            # arm(). Once control has been used, require its actual hold ack.
            baseline_ready = status['mode'] == 'hold' and status['hold_acknowledged']
            unused = status['mode'] == 'unknown' and status['publish_attempts'] == 0 \
                and hold_stamp is None
            fence = max(returning.started_ns, hold_stamp or returning.started_ns)
            if status['desired_mode'] != 0 or status['mode_request_inflight'] is not None \
                    or status['mode_request_pending'] is not None or not (baseline_ready or unused) \
                    or self._frame.received_monotonic_ns <= fence or feedback.timestamp_ns <= fence \
                    or self._session.requires_release:
                return ExecutionResult('return_preparing', returning.reason)
            self._session.fence_after_hold(now_ns=now)
            if not self._session.start_return(ReturnSample(self._frame.sequence_id,
                    self._frame.received_monotonic_ns), now_ns=now,
                    max_velocity_rad_s=returning.options['max_velocity_rad_s'],
                    max_acceleration_rad_s2=returning.options['max_acceleration_rad_s2']):
                self._finish_return('failed', 'return authority could not be acquired', now)
                return ExecutionResult('waiting_release', returning.reason)
            self._hold_stamp, self._resume_pending = hold_stamp, False
            self._output_baseline_stamp = None
            returning.activate(feedback)
        candidate = JointCommand(dict(returning.targets), 'kuavo_fixed_joint_return',
            self._frame.sequence_id, self._frame.received_monotonic_ns, 100)
        if self._session.offer(candidate, epoch=self._session.epoch):
            self._candidate_source = dict(sequence_id=self._frame.sequence_id,
                input_created_time_ns=self._frame.received_monotonic_ns,
                device_timestamp_ns=self._frame.device_timestamp_ns,
                epoch=self._session.epoch, authority='fixed_joint_return')
        checked = self._session.tick(feedback, now_ns=now, publication_allowed=False)
        if checked.status in ('waiting_release', 'fault', 'closed'):
            self._finish_return('failed', checked.reason, now)
            return checked
        status = self.transport.status()
        if checked.status == 'waiting_transport' and status['desired_mode'] != 2 \
                and status['mode_request_inflight'] is None and status['mode_request_pending'] is None \
                and not status['requires_release']:
            self.transport.arm()
            self.transport.refresh_input(sequence_id=self._frame.sequence_id,
                input_deadline_ns=self._frame.received_monotonic_ns+100_000_000)
        status = self.transport.status()
        if status['mode'] != 'external' or status['desired_mode'] != 2:
            return checked
        baseline = self.transport.binding_output_baseline()
        if baseline is None:
            self._finish_return('failed', 'return external mode has no output baseline', now)
            return ExecutionResult('waiting_release', returning.reason)
        positions, published_ns = baseline
        if published_ns != self._output_baseline_stamp:
            if published_ns > now:
                return ExecutionResult('waiting_transport', 'await return output baseline clock')
            self._session.synchronize_output_baseline(positions,
                published_ns=published_ns, now_ns=now)
            self._output_baseline_stamp = published_ns
        result = self._session.tick(feedback, now_ns=now)
        wire = self.transport.publication_evidence()
        if wire is not None and wire['command_sequence_id'] > self._synchronized_wire_sequence:
            self._session.synchronize_reference_velocity(
                dict(zip(ARM_NAMES, map(math.radians, wire['positions_deg']))),
                dict(zip(ARM_NAMES, map(math.radians, wire['velocities_deg_s']))),
                published_ns=self._session.last_publish_time_ns)
            self._synchronized_wire_sequence = wire['command_sequence_id']
        if result.status in ('waiting_release', 'fault', 'closed'):
            self._finish_return('failed', result.reason, now)
        elif result.status == 'published' and returning.arrived(feedback, self._session.last_published):
            self._finish_return('completed', 'measured target reached with quiet feedback; release before teleoperation', now)
            return ExecutionResult('return_completed', returning.reason)
        return result

    def close(self):
        self.cancel_return('control session closed')
        self._closed.set()
        execution = self._session.close()
        status = self.transport.close()
        self._worker.join(timeout=.05)
        return dict(status, solver_inflight=self._inflight, physical_stop_verified=False,
                    execution_status=execution.status, execution_reason=execution.reason)


class NativeArmWorkSolver:
    """Worker-owned retargeting; control owner supplies an optional seed snapshot."""

    def __init__(self, solver, retargeter, *, target_progress=None):
        self._solver, self._retargeter = solver, retargeter
        self._target_progress = target_progress
        self._retarget_epoch = None
        self._clutch_generations = None

    def __call__(self, work):
        if work.retarget_epoch != self._retarget_epoch:
            self._retargeter.declutch()
            self._solver.reset()
            if self._target_progress is not None:
                self._target_progress.reset()
            self._retarget_epoch = work.retarget_epoch
        elif self._clutch_generations is not None:
            changed = tuple(side for index, side in enumerate(('left', 'right'))
                            if work.clutch_generations[index] != self._clutch_generations[index])
            if changed:
                # Edges are recorded by the input owner, even if this serial
                # worker skipped the brief released frame between two solves.
                self._retargeter.declutch(changed)
                self._solver.reset(changed)
                if self._target_progress is not None:
                    self._target_progress.reset(changed)
        self._clutch_generations = work.clutch_generations
        anchors = None
        def anchor(side):
            nonlocal anchors
            if anchors is None:
                anchors = self._solver.forward_kinematics(work.feedback)
            return anchors[side]
        result = self._retargeter.update(work.frame, anchor)
        if result.intent is None:
            raise IKCandidateRejected('no active arm target')
        seed_options = {} if work.ik_seed_rad is None else {'seed_q': work.ik_seed_rad}
        context = {'epoch': work.epoch,
            'input_created_time_ns': work.frame.received_monotonic_ns,
            'device_timestamp_ns': work.frame.device_timestamp_ns}
        progress = self._target_progress
        if progress is None:
            trial = None
        elif callable(getattr(progress, 'prepare_at', None)):
            trial = progress.prepare_at(result.intent, now_ns=work.started_ns)
        else:
            trial = progress.prepare(result.intent)
        target = result.intent if trial is None else trial.effective_intent
        if trial is not None:
            context['target_progress'] = dict(
                policy=progress.policy, token_id=trial.token_id,
                raw_targets=diagnostic_poses(trial.raw_intent.targets),
                effective_targets=diagnostic_poses(trial.effective_intent.targets),
                failure_rebase_policy=progress.failure_rebase_policy,
                last_failure_rebase={side: dict(
                    accepted_token_id=source.accepted_token_id,
                    failure_token_id=source.failure_token_id,
                    qualified_pose=diagnostic_poses({side: source.qualified_pose})[side])
                    for side, source in trial.rebase_sources.items()})
            context['target_progress'].update(progress.diagnostic_fields(trial))
        try:
            candidate = self._solver.solve(target, work.feedback,
                sequence_id=work.frame.sequence_id, now_ns=work.started_ns, ttl_ms=200,
                diagnostic_context=context, **seed_options)
            if trial is not None:
                qualified = None
                validated_poses = getattr(self._solver, 'validated_poses_for', None)
                if validated_poses is not None:
                    complete = validated_poses(candidate)
                    if complete is None or any(side not in complete for side in target.targets):
                        raise ValueError('qualified FK poses unavailable for returned candidate')
                    qualified = {side: complete[side] for side in target.targets}
                if not progress.commit(trial, qualified_poses=qualified):
                    raise IKCandidateRejected('target progress generation changed during solve')
        except (IKTargetUnreachable, IKPoseResidualRejected, IKCandidateLimitRejected):
            if trial is not None:
                progress.discard(trial, rebase_to_qualified=True)
            raise
        except Exception:
            if progress is not None:
                progress.reset()
            raise
        return candidate
