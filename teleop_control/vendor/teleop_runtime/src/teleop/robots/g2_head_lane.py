"""Optional G2 head lane, sharing one SDK lifecycle with the dual-arm worker.

Robot-specific composition stays here; generic head mapping/control is reused.
All SDK calls run synchronously on the worker thread, never in competing threads.
"""
import time
from teleop.control.head_follow_pipeline import HeadFollowSession
from teleop.robots.g2 import G2Adapter
from teleop.robots.g2_head import G2_HEAD_ROLL, G2HeadJointMapper
from teleop.robots.g2_head_servo import G2HeadFeedbackReader, G2NormalHeadSender, normal_head_limits
from teleop.xr.wire import message_age_ns


class CombinedControlFault(RuntimeError):
    """Fatal for all control lanes; restarting requires an operator."""


class GuardedG2ControlRobot:
    """Serialize calls by worker ownership and latch a failed/slow call.

    No cancellation of a call already inside the vendor SDK is promised.
    """
    def __init__(self, robot, *, clock_ns=time.monotonic_ns, audit=lambda *a, **k:None):
        self.robot, self.clock, self.audit = robot, clock_ns, audit
        self.guard = lambda:None
        self.cycle_ns = None
        self.failed = False
        self.max_call_ms = 0.

    def begin_cycle(self, guard):
        if self.failed: raise CombinedControlFault('G2 control already faulted')
        self.cycle_ns, self.guard = self.clock(), guard

    def check(self):
        if self.failed: raise CombinedControlFault('G2 control already faulted')
        try:
            self.guard()
        except Exception as error:
            self.failed = True
            raise CombinedControlFault(f'combined send guard failed: {error}') from error
        # Two nominal 100 Hz ticks: bounded jitter, not a hard-real-time claim.
        # The 100 ms per-call watchdog below also applies during startup.
        if self.cycle_ns is not None and self.clock()-self.cycle_ns > 20_000_000:
            self.failed = True
            raise CombinedControlFault('combined control cycle exceeded 20 ms')

    def _call(self, name, *args, **kwargs):
        self.check()
        started = self.clock()
        try:
            result = getattr(self.robot, name)(*args, **kwargs)
            elapsed = (self.clock()-started)/1e6
            self.max_call_ms = max(self.max_call_ms, elapsed)
            self.audit('sdk_call', method=name, call_ms=elapsed, returned=True)
            if elapsed > 100:
                raise CombinedControlFault(f'{name} exceeded 100 ms')
            self.check()
            return result
        except Exception as error:
            self.failed = True
            raise CombinedControlFault(f'{name}: {error}') from error

    def get_joint_states(self): return self._call('get_joint_states')
    def get_end_state(self): return self._call('get_end_state')
    def joint_servo_control(self, request): return self._call('joint_servo_control', request)
    def move_ee_pos(self, request): return self._call('move_ee_pos', request)
    def move_head_joint_servo(self, *args, **kwargs):
        return self._call('move_head_joint_servo', *args, **kwargs)


class G2HeadLane:
    def __init__(self, robot, *, clock_ns=time.monotonic_ns, sleep=time.sleep,
                 audit=lambda *a, **k:None, health=lambda:None, stopped=lambda:False):
        self.clock, self.audit, self.health, self.stopped = clock_ns, audit, health, stopped
        self.message, self.session, self.sent = None, None, 0
        self.last_candidate = None
        self.reader = G2HeadFeedbackReader(robot, clock_ns=clock_ns)
        try:
            self._initialize(robot, sleep)
        except Exception as error:
            self.audit('head_fault', stage='startup', reason=str(error),
                       feedback=self.reader.diagnostics(include_history=True),
                       last_sent_targets_rad=None, physical_robot_command_sent=False)
            raise

    def _initialize(self, robot, sleep):
        clock_ns, audit = self.clock, self.audit
        probe = G2Adapter(dry_run=False, state_reader=self.reader)
        samples, timestamps = [], []
        for _ in range(3):
            self.health()
            if self.stopped(): raise CombinedControlFault('stopped during head preflight')
            samples.append(probe.read_state(now_ns=self.clock()))
            timestamps.append(self.reader.timestamp)
            self.audit('startup_feedback', raw=self.reader.diagnostics())
            sleep(.03)
        if not timestamps[0] < timestamps[1] < timestamps[2]:
            raise CombinedControlFault('head startup timestamps must advance')
        limits = normal_head_limits(samples[-1])
        if any(max(s.positions[n] for s in samples)-min(s.positions[n] for s in samples) > .001 for n in limits):
            raise CombinedControlFault('head startup feedback unstable')
        self.audit('fixed_envelope', limits_rad=dict(limits), rate_rad_s=.3)
        self.sender = G2NormalHeadSender(robot, reader=self.reader, limits=limits,
            roll_hold=limits[G2_HEAD_ROLL][0], clock_ns=self.clock, guard=self.guard, audit=audit)
        self.adapter = G2Adapter(dry_run=False, state_reader=self.reader, command_sender=self.sender,
                                allow_live_commands=True, default_velocity_rad_s=.1)
        self.session = HeadFollowSession(self.adapter, G2HeadJointMapper(self.adapter.get_joint_schema()),
            roll_joint=G2_HEAD_ROLL, send_enabled=True, on_anchor=self._anchor,
            clock_ns=self.clock, stop_requested=self.stopped, target_rate_rad_s=.3,
            target_limits=limits, before_send=self._candidate)

    def guard(self):
        self.health()
        if self.stopped() or self.message is None: raise CombinedControlFault('combined input stopped')
        now, frame = self.clock(), self.message.frame
        message_age_ns(self.message, now)
        if frame.stale or not 0 <= now-frame.received_monotonic_ns <= 300_000_000:
            raise CombinedControlFault('head XR expired before send')
        last = self.session.last_input_ns if self.session else None
        if last is not None and not 0 <= now-last <= 300_000_000:
            raise CombinedControlFault('head device input stopped updating')

    def _anchor(self, state):
        self.audit('anchor', head_quaternion=self.session.anchor_head_quaternion,
                   positions_rad=dict(self.session.previous))
        self.guard()
        self.sender.reanchor(state)

    def _candidate(self, command, state, frame):
        self.last_candidate = {'xr_sequence': frame.sequence_id,
                               'mapping': self.session.last_mapping}
        self.audit('candidate', xr_sequence=frame.sequence_id,
                   head_quaternion=list(frame.head_pose.orientation_xyzw),
                   mapping=self.session.last_mapping, targets_rad=dict(command.targets),
                   measured_rad={n:state.positions[n] for n in command.targets},
                   feedback=self.reader.diagnostics())
        self.guard()

    def step(self, message):
        self.message = message
        self.health()
        try:
            message_age_ns(message, self.clock())
        except ValueError:
            self.session.reset()
            return
        try:
            command = self.session.step(message.frame)
        except Exception as error:
            self.audit('head_fault', stage='control', reason=str(error),
                       xr_sequence=message.frame.sequence_id,
                       head_quaternion_repr=repr(message.frame.head_pose.orientation_xyzw),
                       feedback=self.reader.diagnostics(include_history=True),
                       last_sent_targets_rad=(dict(self.sender.last_sent_targets)
                           if self.sender.last_sent_targets is not None else None),
                       head_commands=self.sent, last_candidate=self.last_candidate)
            raise
        if command is not None: self.sent += 1
        self.audit('head_cycle', xr_sequence=message.frame.sequence_id,
                   status=self.session.status, sent=self.sent)

    def close(self):
        self.session.reset()
        self.adapter.stop()
