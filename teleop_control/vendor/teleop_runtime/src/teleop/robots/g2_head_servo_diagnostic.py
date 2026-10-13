"""Fixed-goal G2 head servo experiment, not a free head-follow mode.

All bounds are software checks. Accepted vendor commands cannot be cancelled here.
"""
import math
import sys
import time

from teleop.backends.direct_joint import DirectJointBackend
from teleop.contracts.intent import JointPositionIntent
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.robots.g2 import G2Adapter
from teleop.robots.g2_head import G2_HEAD_JOINTS
from teleop.robots.g2_head_servo import G2HeadFeedbackReader, G2HeadServoSender

YAW, ROLL, PITCH = G2_HEAD_JOINTS
RATE = 0.2
DELTA = math.radians(5)
WINDOW_NS = 5_000_000_000


class _BoundedSender(G2HeadServoSender):
    def __init__(self, robot, anchor, deadline, stop, clock_ns):
        super().__init__(robot, clock_ns=clock_ns, target_rate_rad_s=RATE)
        self.fixed = {n: anchor.positions[n] for n in G2_HEAD_JOINTS}
        self.deadline, self.stop = deadline, stop
        self.reanchor(anchor)

    def send(self, request):
        if self.stop() or self.clock() >= self.deadline:
            raise ValueError('stopped or motion deadline expired')
        targets = dict(zip(request.joint_names, request.joint_positions))
        if (not self.fixed[YAW] <= targets.get(YAW, math.inf) <= self.fixed[YAW] + DELTA
                or any(targets.get(n) != self.fixed[n] for n in (ROLL, PITCH))):
            raise ValueError('request outside fixed diagnostic interval')
        return super().send(request)


def _checked_read(adapter, reader, clock, audit):
    state = adapter.read_state(now_ns=clock())
    audit('feedback', measured_rad={n: state.positions[n] for n in G2_HEAD_JOINTS},
          raw=reader.diagnostics())
    if (not 0 <= clock() - state.timestamp_ns <= 200_000_000
            or reader.changed_ns is None or not 0 <= clock()-reader.changed_ns <= 200_000_000):
        raise ValueError('feedback read/log exceeded freshness window')
    return state


def _check_position(state, fixed, previous):
    yaw = state.positions[YAW]
    if not fixed[YAW] - .001 <= yaw <= fixed[YAW] + DELTA + .001:
        raise ValueError('measured yaw outside diagnostic interval')
    if abs(yaw - previous) > .01:
        raise ValueError('measured yaw jumped more than 0.01 rad')
    if any(abs(state.positions[n] - fixed[n]) > .005 for n in (ROLL, PITCH)):
        raise ValueError('held roll/pitch moved')


def _execute(adapter, reader, sender, anchor, deadline, *, clock, sleep, stop, audit, counts):
    router, safety = ControlRouter(DirectJointBackend()), SafetyGateway()
    fixed = dict(sender.fixed)
    goal, previous = fixed[YAW] + DELTA, fixed[YAW]
    last_target, last_send = previous, clock()
    best_error, progress_time = DELTA, clock()
    sleep(.01)
    while clock() < deadline:
        if stop(): return 130
        state = _checked_read(adapter, reader, clock, audit)
        _check_position(state, fixed, previous)
        current = state.positions[YAW]
        if abs(last_target - current) > .01 + 1e-12:
            raise ValueError('feedback left last target lead bound')
        if stop(): return 130
        if clock() >= deadline: break
        if abs(goal-current) <= .001:
            audit('reached_feedback', goal_rad=goal, physical_observer_confirmation_required=True)
            # Observe after streaming ends. Do not silently assume stop-stream == stop-motor.
            for _ in range(20):
                if stop(): return 130
                sleep(.05)
                after = _checked_read(adapter, reader, clock, audit)
                _check_position(after, fixed, current)
                current = after.positions[YAW]
                if abs(current-goal) > .003:
                    raise ValueError('head drifted after stream stopped')
            return 0
        error = abs(goal-current)
        if best_error-error >= .0005:
            best_error, progress_time = error, clock()
        elif clock()-progress_time >= 4_000_000_000:
            raise ValueError('no motor progress for 4 seconds')
        step = RATE * max(0, min((clock()-last_send)/1e9, .01))
        desired = min(goal, current+.01)
        target = last_target + max(-step, min(step, desired-last_target))
        if not fixed[YAW] <= target <= goal or abs(target-current) > .01 + 1e-12:
            raise ValueError('generated target outside diagnostic bounds')
        targets = dict(fixed, **{YAW: target})
        command = router.route(JointPositionIntent(targets, source='head-servo-diagnostic'),
                               state, now_ns=clock())
        validated = safety.validate(command, state, adapter.get_joint_schema(), now_ns=clock())
        audit('send_begin', attempt=counts['attempted']+1, targets_rad=targets,
              source_timestamp=reader.diagnostics().get('source_timestamp'),
              measured_rad={n: state.positions[n] for n in G2_HEAD_JOINTS})
        # Durable logging may block. Recheck immediately before calling the SDK.
        if stop(): return 130
        if clock() >= deadline: break
        if not 0 <= clock()-state.timestamp_ns <= 200_000_000:
            raise ValueError('feedback expired while logging')
        if reader.changed_ns is None or not 0 <= clock()-reader.changed_ns <= 200_000_000:
            raise ValueError('source feedback expired while logging')
        counts['attempted'] += 1
        begin = clock()
        adapter.send_joint_command(validated, now_ns=begin)
        counts['accepted'] += 1
        audit('send_return', attempt=counts['attempted'], call_ms=(clock()-begin)/1e6)
        previous, last_target, last_send = current, target, clock()
        sleep(.01)  # never catch up with bursts after slow calls or fsync
    raise TimeoutError('five-second motion window expired; no further sends')


def run_diagnostic(gdk, *, audit, enable_motion=False, settle_seconds=2,
                   clock_ns=time.monotonic_ns, sleep=time.sleep,
                   stop_requested=lambda: False, output=sys.stdout):
    initialized, adapter, reader, code = False, None, None, 2
    counts = {'attempted': 0, 'accepted': 0}
    try:
        if type(enable_motion) is not bool or not math.isfinite(settle_seconds) or not 0 <= settle_seconds <= 10:
            raise ValueError('invalid motion flag or settle time')
        audit('precheck', motion_enabled=enable_motion, target_rate_rad_s=RATE,
              yaw_delta_deg=5, window_seconds=5, automatic_return=False)
        if stop_requested(): raise InterruptedError('stop requested before initialization')
        if gdk.gdk_init() != gdk.GDKRes.kSuccess: raise RuntimeError('GDK initialization failed')
        initialized = True
        robot = gdk.Robot()
        sleep(settle_seconds)
        reader = G2HeadFeedbackReader(robot, clock_ns=clock_ns)
        adapter = G2Adapter(dry_run=False, state_reader=reader)
        samples = []
        timestamps = []
        for _ in range(3):
            if stop_requested(): raise InterruptedError('stop requested during startup')
            state = _checked_read(adapter, reader, clock_ns, audit)
            samples.append(state)
            timestamps.append(reader.timestamp)
            sleep(.03)
        if not timestamps[0] < timestamps[1] < timestamps[2]:
            raise ValueError('initial source timestamps must advance')
        for n in G2_HEAD_JOINTS:
            values = [s.positions[n] for s in samples]
            if max(values)-min(values) > .001: raise ValueError('initial head feedback unstable')
        anchor = samples[-1]
        yaw = anchor.positions[YAW]
        if abs(yaw) > math.radians(15): raise ValueError('initial yaw outside +/-15 degrees')
        joint = adapter.get_joint_schema().get(YAW)
        if not joint.position_min <= yaw+DELTA <= joint.position_max: raise ValueError('goal exceeds joint limits')
        audit('plan', anchor_rad={n: anchor.positions[n] for n in G2_HEAD_JOINTS},
              goal_yaw_rad=yaw+DELTA, target_rate_rad_s=RATE, actual_speed_guaranteed=False)
        if stop_requested(): raise InterruptedError('stop requested before motion')
        if not enable_motion:
            code = 0
        else:
            deadline = clock_ns()+WINDOW_NS
            sender = _BoundedSender(robot, anchor, deadline, stop_requested, clock_ns)
            adapter = G2Adapter(dry_run=False, state_reader=reader, command_sender=sender,
                                allow_live_commands=True, default_velocity_rad_s=.1)
            code = _execute(adapter, reader, sender, anchor, deadline, clock=clock_ns,
                            sleep=sleep, stop=stop_requested, audit=audit, counts=counts)
    except Exception as error:
        print(f'HEAD_SERVO_DIAGNOSTIC_ERROR {error}; no further commands', file=output, flush=True)
        try: audit('error', reason=str(error), raw=reader.diagnostics() if reader else {})
        except Exception: pass  # disk may have failed; stderr remains best-effort evidence
        code = 130 if isinstance(error, InterruptedError) else 2
    finally:
        if adapter is not None: adapter.stop()  # local-only, NOT hardware emergency stop
        if initialized:
            try:
                if gdk.gdk_release() != gdk.GDKRes.kSuccess: raise RuntimeError('GDK release failed')
            except Exception as error:
                print(str(error), file=output, flush=True)
                code = 2
        try: audit('summary', **counts, code=code, physical_continuity_verified=False)
        except Exception: code = 2
        print(f'HEAD_SERVO_DIAGNOSTIC_SUMMARY {counts} code={code}', file=output, flush=True)
    return code
