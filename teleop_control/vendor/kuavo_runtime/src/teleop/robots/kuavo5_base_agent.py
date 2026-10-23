"""User-started robot-side base runner. Default import has no hardware I/O."""
from collections import deque
import fcntl
import json
import signal
import sys
import threading
import time

from teleop.control.kuavo5_base_session import BaseSession
from teleop.robots.kuavo5_base_ros import BaseRosBinding
from teleop.robots.kuavo5_live_protocol import InputLease, StaleInputLease, INPUT_TTL_NS
from teleop.retargeting.mobile_base import base_speed_limits
from teleop.robots.kuavo5_base_datagram import robot_channel
from teleop.robots.kuavo5_base_timing import InputTiming


class BaseStream:
    """I/O threads cannot stall the control/watchdog loop. Latest input wins."""
    def __init__(self, reader, writer, datagram=None, udp_required=False, clock=time.monotonic_ns):
        self.reader=reader; self.writer=writer
        self.clock=clock; self.timing=InputTiming()
        self.datagram=datagram
        self.udp_required=udp_required or datagram is not None
        self.condition=threading.Condition()
        self.pending=None; self.closing=False; self.error=None
        self.output=deque(); self.done=False; self.dropped_challenges=0
        self.threads=[]

    def start(self):
        def read():
            try:
                while True:
                    line=self.reader.readline(65538)
                    if not line:
                        with self.condition: self.closing=True
                        return
                    if len(line)>65536: raise ValueError('input packet too large')
                    packet=json.loads(line)
                    if not isinstance(packet,dict): raise ValueError('invalid input object')
                    with self.condition:
                        if packet=={'event':'close'}:
                            self.closing=True; return
                        if packet.get('event')!='xr': raise ValueError('unknown input event')
                        if self.udp_required: raise ValueError('SSH input forbidden in UDP session')
                        self.pending=packet
                        self.timing.mark('ssh_reply_received',self.clock(),record=True,
                                         challenge_id=packet.get('challenge_id'))
            except Exception as exc:
                with self.condition:
                    self.error=str(exc); self.closing=True
        def write():
            try:
                while True:
                    with self.condition:
                        while not self.output and not self.done: self.condition.wait()
                        if not self.output: return
                        packet=self.output.popleft()
                    self.writer.write(json.dumps(packet,allow_nan=False,separators=(',', ':'))+'\n')
                    self.writer.flush()
            except Exception as exc:
                with self.condition:
                    self.error='output stream failed: '+str(exc); self.closing=True
        for name,target in (('base-input',read),('base-output',write)):
            thread=threading.Thread(target=target,name=name,daemon=True)
            thread.start(); self.threads.append(thread)

    def send(self, packet):
        if self.datagram is not None and packet.get('event')=='challenge':
            self.datagram.send(packet)
            return
        with self.condition:
            if packet.get('event')=='challenge':
                kept=deque(p for p in self.output if p.get('event')!='challenge')
                self.dropped_challenges+=len(self.output)-len(kept)
                self.output=kept
            if len(self.output)>=64:
                self.error='output backpressure'; self.closing=True
                return
            self.output.append(packet); self.condition.notify()

    def poll(self):
        with self.condition:
            packet=self.datagram.poll() if self.datagram is not None else self.pending
            self.pending=None
            return packet,self.closing,self.error

    def close(self):
        with self.condition: self.done=True; self.condition.notify_all()
        if self.threads: self.threads[-1].join(timeout=.3)
        if self.datagram is not None: self.datagram.close()


def run_robot(*, motion_enabled=False, reader=None, writer=None, binding_factory=BaseRosBinding,
              clock=time.monotonic_ns, sleep=time.sleep, max_duration_s=65.,
              lock_path='/tmp/teleop-kuavo5-base.lock',speed_profile='vendor_vr',
              input_transport='ssh',input_key=None,udp_bind_address='192.168.26.1'):
    if motion_enabled is not True: raise ValueError('explicit base execution permission required')
    if input_transport not in ('ssh','udp'): raise ValueError('invalid input transport')
    limits=base_speed_limits(speed_profile)
    reader=sys.stdin if reader is None else reader
    writer=sys.stdout if writer is None else writer
    stream=BaseStream(reader,writer,clock=clock)
    stream.udp_required=input_transport=='udp'
    binding=None; session=None; report=None
    stopped=threading.Event(); previous={}; lockfile=None
    exit_code=1
    try:
        # The reporting stream must also explain refusal by the owner lock.
        stream.start()
        # Local process lock complements ROS graph inspection; never evict owner.
        lockfile=open(lock_path,'a')
        try: fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('another base robot session owns the control lock')
        if input_transport=='udp':
            stream.datagram=robot_channel(input_key,bind_address=udp_bind_address)
            stream.send(dict(event='base_input_endpoint',input_transport='udp',
                             port=stream.datagram.sock.getsockname()[1]))
            handshake_deadline=clock()+5_000_000_000
            while stream.datagram.peer is None:
                _,closing,error=stream.poll()
                if closing or error: raise RuntimeError(error or 'closed during UDP handshake')
                if clock()>=handshake_deadline: raise RuntimeError('UDP handshake timeout; no controller binding started')
                sleep(.01)
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT,signal.SIGTERM):
                previous[signum]=signal.signal(signum,lambda *_:stopped.set())
        binding=binding_factory(motion_enabled=True,clock=clock,speed_profile=speed_profile)
        binding.start()
        startup_deadline=clock()+8_000_000_000
        while True:
            _,closing,error=stream.poll()
            if stopped.is_set() or closing or error: raise RuntimeError(error or 'closed during preflight')
            health,feedback=binding.guard.snapshot(); now=clock()
            if not health.error and 0<=now-health.observed_ns<500_000_000 and feedback is not None \
                    and 0<=now-feedback.observed_ns<200_000_000 \
                    and all(v is True for v in (health.control_enabled,health.velocity_enabled,health.mpc_enabled)):
                break
            if binding.guard.error: raise ValueError(binding.guard.error)
            if now>=startup_deadline: raise ValueError('base preflight incomplete: '+str(health))
            sleep(.02)
        def publish(velocity):
            binding.create_publisher()
            binding.publish(velocity)
        session=BaseSession(publish=publish,speed_profile=speed_profile)
        lease=InputLease(); start=clock(); next_status=0; ready=False; startup_quiet_deadline=start+5_000_000_000
        last_input=None
        last_status=None
        input_diagnostics=dict(accepted_frames=0,rejected_replies=0,duplicate_device_frames=0,expired_before_tick=0,
                               last_accepted_timing=None,last_rejection=None)
        while True:
            cycle=clock()
            stream.timing.mark('control_cycle',cycle)
            packet,closing,error=stream.poll()
            if error: session.fail(error)
            if stopped.is_set() or closing or cycle-start>=int(max_duration_s*1e9):
                session.request_close('operator/connection/duration closed')
            frame=None
            if packet is not None and not session.closing:
                try:
                    consumed=clock()
                    stream.timing.mark('reply_consumed',consumed,record=True,challenge_id=packet.get('challenge_id'))
                    frame=lease.accept(packet,now_ns=consumed)
                    if frame is None:
                        input_diagnostics['duplicate_device_frames']+=1
                except StaleInputLease as exc:
                    # Keep evidence without renewing the expired authority.
                    input_diagnostics['rejected_replies']+=1
                    input_diagnostics['last_rejection']=dict(reason=str(exc),robot_time_ns=cycle,
                                                             timing=lease.last_timing)
                except Exception as exc:
                    session.fail('input rejected: '+str(exc))
            snapshot_started=clock()
            health,feedback=binding.guard.snapshot()
            stream.timing.span('state_snapshot',clock()-snapshot_started)
            # Do not begin while manufacturer output is already commanding motion.
            if not session.started and binding.guard.downstream_velocity is not None \
                    and any(abs(v)>.01 for v in binding.guard.downstream_velocity):
                session.fail('manufacturer base output nonzero before takeover')
            tick_now=clock()
            # A reply can expire while the state snapshot is being obtained.
            # Discard it before the mapper; do not overwrite the deadline of a
            # previous still-valid intent or classify local delay as bad input.
            if frame is not None and not 0<=tick_now-frame.received_monotonic_ns<INPUT_TTL_NS:
                input_diagnostics['expired_before_tick']+=1
                input_diagnostics['last_rejection']=dict(reason='reply expired before session tick',
                    robot_time_ns=tick_now,timing=lease.last_timing)
                frame=None
            if frame is not None:
                last_input=frame
                input_diagnostics['accepted_frames']+=1
                input_diagnostics['last_accepted_timing']=lease.last_timing
                binding.input_deadline_ns=frame.received_monotonic_ns+INPUT_TTL_NS
            report=session.tick(frame,now_ns=tick_now,health=health,feedback=feedback)
            stream.timing.span('session_tick',clock()-tick_now)
            if session.error: session.request_close(session.error)
            if not ready and session.started:
                stream.send(dict(event='base_ready',profile=list(limits),speed_profile=speed_profile,topic='/cmd_vel',
                    input_transport=input_transport,
                    input_ttl_ms=100,mode_services_called=False,activation='neutral then X+A',
                    physical_stop_verified=False))
                ready=True
            if not session.started and clock()>=startup_quiet_deadline:
                session.fail('base did not become quiet; takeover refused')
                session.request_close(session.error)
            if not session.closing:
                issued=clock(); challenge=lease.challenge(issued)
                stream.send(challenge)
                stream.timing.mark('challenge_issued',issued,record=True,challenge_id=challenge['challenge_id'])
            stream.timing.span('control_to_challenge',clock()-cycle)
            # A one-tick timeout used to disappear between 5Hz status samples.
            # Record state changes immediately; routine reports remain at 5Hz.
            if cycle>=next_status or report['status']!=last_status:
                report['input']=None if last_input is None else dict(
                    sequence=last_input.sequence_id,
                    age_ms=(clock()-last_input.received_monotonic_ns)/1e6,
                    left_axis=last_input.left_controller.axis,right_axis=last_input.right_controller.axis,
                    x=last_input.left_controller.buttons.primary,a=last_input.right_controller.buttons.primary)
                report['feedback']=None if feedback is None else dict(
                    age_ms=(clock()-feedback.observed_ns)/1e6,
                    velocity=[feedback.vx,feedback.vy,feedback.wz])
                report['downstream_velocity']=binding.guard.downstream_velocity
                report['input_diagnostics']=dict(input_diagnostics,timing=stream.timing.snapshot(clock()))
                report['odometry_timing']=getattr(binding.guard,'last_odometry_timing',None)
                if stream.datagram is not None:
                    report['input_diagnostics']['datagram']=dict(stream.datagram.stats)
                stream.send(report); next_status=cycle+200_000_000
                last_status=report['status']
            if session.closed:
                exit_code=0 if not session.error and report['stop_observed'] else 1
                break
            sleep(max(0.,.02-(clock()-cycle)*1e-9))
    except Exception as exc:
        report=dict(event='base_status',status='fault',reason=str(exc),stop_observed=False,
                    physical_stop_verified=False,publish_attempts=0 if session is None else session.publish_attempts,
                    odometry_timing=getattr(binding.guard,'last_odometry_timing',None) if binding is not None else None)
    finally:
        # Normal exits reach here only after the bounded zero/feedback phase.
        # Unexpected exceptions get a best-effort zero, never a success claim.
        if session is not None and session.started and not session.closed and binding is not None:
            try: binding.publish((0.,0.,0.))
            except Exception: pass
        if binding is not None:
            try: binding.close()
            except Exception as exc:
                exit_code=1
                report=dict(report or {},cleanup_error=str(exc))
        stream.send(dict(event='base_closed',exit_code=exit_code,report=report,
                         physical_stop_verified=False))
        stream.close()
        for signum,handler in previous.items(): signal.signal(signum,handler)
        if lockfile is not None: lockfile.close()
    return exit_code
