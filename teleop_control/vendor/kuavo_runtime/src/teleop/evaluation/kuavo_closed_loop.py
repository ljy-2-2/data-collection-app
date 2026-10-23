"""Offline experiment using the unchanged live controller and a virtual plant.

No ROS, transport binding, device discovery or network code. A serial solve is
computed synchronously but delivered only after its configured virtual latency.
Mode acknowledgements are instantaneous *simulated* events. This does not model
WBC, vendor interpolation, dynamics, collisions, noise or real transport timing.
"""
from collections import Counter
from dataclasses import replace
import math
from queue import Empty

from teleop.contracts.robot_state import RobotState
from teleop.control.kuavo5_live_controller import KuavoLiveController
from teleop.control.kuavo5_tracking_governor import KuavoTrackingGovernor
from teleop.robots.kuavo5_native_ik import ARM_NAMES, IKCandidateRejected


def _joints(q):
    if set(q) != set(ARM_NAMES):raise ValueError('exactly 14 named arm joints required')
    result={n:float(q[n]) for n in ARM_NAMES}
    if not all(math.isfinite(v) for v in result.values()):raise ValueError('nonfinite joint')
    return result


class JointResponse:
    """Assumed first-order position response with an independent speed cap."""
    def __init__(self,initial,*,tau_s=.06,velocity_rad_s=1.):
        if not math.isfinite(tau_s) or tau_s<0 or not math.isfinite(velocity_rad_s) or velocity_rad_s<=0:
            raise ValueError('finite nonnegative lag and positive speed required')
        self.positions=_joints(initial);self.target=dict(self.positions)
        self.tau_s,self.velocity_rad_s=tau_s,velocity_rad_s

    def command(self,q):self.target=_joints(q)

    def hold(self):self.target=dict(self.positions)

    def advance(self,seconds):
        if not math.isfinite(seconds) or seconds<0:raise ValueError('invalid elapsed time')
        fraction=1-math.exp(-seconds/self.tau_s) if self.tau_s else 1.
        limit=self.velocity_rad_s*seconds
        self.positions={n:q+max(-limit,min(limit,(self.target[n]-q)*fraction))
                        for n,q in self.positions.items()}


class _MemoryTransport:
    """Controller protocol double, not a ROS transport emulator or watchdog test."""
    def __init__(self,plant,clock):
        self.plant,self.clock=plant,clock
        self.external=False;self.closed=False;self.stamp=None;self.baseline=None
        self.messages=[];self.pending=None;self.wire=None

    def status(self):
        return dict(mode='external' if self.external else 'hold',desired_mode=2 if self.external else 0,
                    fault=None,requires_release=self.external,mode_request_inflight=None,
                    mode_request_pending=None,hold_acknowledged_time_ns=self.stamp)

    def update_feedback(self,state):return state
    def constrain_recovery_deadline(self,deadline):pass
    def refresh_input(self,**kwargs):pass
    def binding_output_baseline(self):return self.baseline
    def publication_evidence(self):return self.wire

    def arm(self):
        if self.closed:raise RuntimeError('closed memory transport')
        self.external=True;self.baseline=(dict(self.plant.positions),self.clock())
        self.wire=None

    def release(self):
        if self.external:
            self.stamp=self.clock();self.plant.hold()
        self.external=False;self.pending=None

    def submit(self,command,*,input_deadline_ns):
        if self.closed or not self.external or self.clock()>=input_deadline_ns:
            raise RuntimeError('inactive or expired memory publication')
        self.pending=command

    def pump(self):
        if self.pending is None:return False
        c,self.pending=self.pending,None
        q=_joints(c.targets);now=self.clock()
        previous,stamp=self.baseline if not self.messages or self.messages[-1]['time_ns']<self.baseline[1] else (
            self.messages[-1]['q_rad'],self.messages[-1]['time_ns'])
        dt=(now-stamp)/1e9
        # A first command at the baseline stamp must equal the baseline.
        if dt<=0 and any(abs(q[n]-previous[n])>1e-10 for n in ARM_NAMES):
            raise RuntimeError('nonzero simulated step without elapsed time')
        velocity={n:(q[n]-previous[n])/dt if dt>0 else 0. for n in ARM_NAMES}
        self.wire=dict(command_sequence_id=c.sequence_id,
                       positions_deg=[math.degrees(q[n]) for n in ARM_NAMES],
                       velocities_deg_s=[math.degrees(velocity[n]) for n in ARM_NAMES])
        self.messages.append(dict(time_ns=now,q_rad=q,velocity_rad_s=velocity))
        self.plant.command(q)
        return True

    def close(self):
        self.release();self.closed=True
        return dict(closed=True,physical_robot_command_sent=False)


class _ScheduledController(KuavoLiveController):
    def _work(self):
        # The constructor's thread exits without consuming anything. Only the
        # offline event loop below owns the original bounded request/result queues.
        return


class ClosedLoopReplay:
    def __init__(self,schema,initial,worker_factory,*,tau_s=.06,
                 plant_velocity_rad_s=1.,solve_latency_ms=30,reference_sync="all_joints",
                 max_velocity_rad_s=.5,max_acceleration_rad_s2=5.,tracking_soft_lead_rad=.15):
        if not math.isfinite(solve_latency_ms) or solve_latency_ms<=0:
            raise ValueError('positive finite simulated solve latency required')
        if reference_sync not in ('all_joints','per_arm'):
            raise ValueError('reference_sync must be all_joints or per_arm')
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0
               for v in (max_velocity_rad_s,max_acceleration_rad_s2,tracking_soft_lead_rad)):
            raise ValueError('finite positive explicit reference profile required')
        KuavoTrackingGovernor(lead_rad=tracking_soft_lead_rad).validate_profile(
            velocity=max_velocity_rad_s, acceleration=max_acceleration_rad_s2,
            period=.01, hard_tracking=.2)
        self.reference_profile=dict(max_velocity_rad_s=max_velocity_rad_s,
            max_acceleration_rad_s2=max_acceleration_rad_s2,tracking_soft_lead_rad=tracking_soft_lead_rad)
        self.reference_sync=reference_sync
        self.schema=schema;self.initial=_joints(initial);self.worker_factory=worker_factory
        if schema.names!=ARM_NAMES:raise ValueError('ordered 14-arm schema required')
        for j in schema.joints:
            if not j.position_min<=self.initial[j.name]<=j.position_max:raise ValueError('initial state outside model')
        self.tau_s=tau_s;self.plant_velocity=plant_velocity_rad_s
        self.latency_ns=round(solve_latency_ms*1e6)
        self.rows=[];self.solves=[];self._used=False

    def run(self,frames,*,progress=None):
        if self._used:raise ValueError('each route needs a fresh replay instance')
        frames=list(frames)
        if not frames or any(b.received_monotonic_ns<=a.received_monotonic_ns for a,b in zip(frames,frames[1:])):
            raise ValueError('strictly increasing saved input times required')
        self._used=True
        # One translation of the host receive clock, preserving every gap and
        # the original device timestamps/sequence/stale flags. Never robot-clock mix.
        origin=frames[0].received_monotonic_ns;base=30_000_000_000
        frames=[replace(f,received_monotonic_ns=base+f.received_monotonic_ns-origin) for f in frames]
        now=[base];clock=lambda:now[0]
        plant=JointResponse(self.initial,tau_s=self.tau_s,velocity_rad_s=self.plant_velocity)
        transport=_MemoryTransport(plant,clock)
        evidence=[None]
        def record(packet):
            if packet['event']=='control_trace':
                evidence[0]=dict(epoch=packet['epoch'],sequence=packet['source_sequence_id'],
                    time_ns=packet['publication_completed_monotonic_ns'],kind=packet['reference_kind'])
        controller=_ScheduledController(self.schema,transport,solve=self.worker_factory(clock),clock=clock,
            diagnostic=record,
            control_period_s=.01,max_velocity_rad_s=self.reference_profile["max_velocity_rad_s"],
            max_acceleration_rad_s2=self.reference_profile["max_acceleration_rad_s2"],
            reference_sync=self.reference_sync,
            reuse_valid_solution_seed=True,tracking_governor=KuavoTrackingGovernor(lead_rad=self.reference_profile["tracking_soft_lead_rad"]))
        index=0;pending=None;latest=None;next_tick=base;last=base;forced=False;episodes=0
        errors=Counter();max_run=run=0
        try:
            while next_tick<=frames[-1].received_monotonic_ns or index<len(frames):
                due=frames[index].received_monotonic_ns if index<len(frames) else next_tick
                now[0]=min(next_tick,due)
                plant.advance((now[0]-last)/1e9);last=now[0]
                while index<len(frames) and frames[index].received_monotonic_ns<=now[0]:
                    latest=frames[index];controller.observe(latest);index+=1
                if now[0]<next_tick:continue
                if pending is not None and pending[0]<=now[0]:
                    controller._results.put_nowait(pending[1]);pending=None
                feedback=RobotState(dict(plant.positions),now[0]);before=len(transport.messages)
                try:
                    result=controller.tick(feedback);status,reason=result.status,result.reason
                except TimeoutError as exc:
                    status,reason='waiting_release',str(exc)
                active=bool(latest and (latest.left_controller.grip>.90 or latest.right_controller.grip>.90))
                is_forced=active and status in ('waiting_release','fault')
                if is_forced and not forced:episodes+=1
                forced=is_forced
                self.rows.append(dict(time_ns=now[0],sequence=latest.sequence_id,status=status,reason=reason,
                    epoch=controller._session.epoch,
                    clutch_generations=[controller._clutch_generations[s] for s in ('left','right')],
                    reference_source=(dict(evidence[0]) if transport.external and evidence[0] else None),
                    grip_values={s:getattr(latest,s+'_controller').grip for s in ('left','right')},
                    active_sides=list(controller._sides),published=len(transport.messages)>before,
                    feedback_rad=[feedback.positions[n] for n in ARM_NAMES],
                    reference_rad=[plant.target[n] for n in ARM_NAMES],
                    tracking=controller.tracking_diagnostics))
                if pending is None:
                    try:ticket,work=controller._requests.get_nowait()
                    except Empty:pass
                    else:
                        candidate=error=None
                        try:candidate=controller._solve(work);run=0
                        except IKCandidateRejected as exc:
                            error=exc;errors[type(exc).__name__]+=1;run+=1;max_run=max(max_run,run)
                        ready=now[0]+self.latency_ns
                        self.solves.append(dict(sequence=work.frame.sequence_id,started_ns=now[0],ready_ns=ready,
                            epoch=work.epoch,clutch_generations=list(work.clutch_generations),
                            feedback_rad=[work.feedback.positions[n] for n in ARM_NAMES],
                            seed_rad=list(work.ik_seed_rad) if work.ik_seed_rad is not None else None,
                            candidate_rad=[candidate.targets[n] for n in ARM_NAMES] if candidate else None,
                            error=type(error).__name__ if error else None,reason=str(error) if error else None))
                        pending=(ready,(ticket,work,candidate,error))
                next_tick+=10_000_000
                if progress and len(self.rows)%1000==0:progress(len(self.rows))
        finally:closed=controller.close()
        self.messages=transport.messages
        return dict(scope='assumed_response_closed_loop_not_physical_acceptance',
            physical_robot_command_sent=False,feedback_source='own_published_output_only_after_initial_state',
            response=dict(tau_s=self.tau_s,velocity_rad_s=self.plant_velocity),
            reference_sync=self.reference_sync,reference_profile=dict(self.reference_profile),
            simulated_solve_latency_ms=self.latency_ns/1e6,period_ms=10,
            samples=len(self.rows),input_frames=len(frames),consumed_input_frames=index,
            solves=len(self.solves),solve_errors=dict(errors),
            longest_failed_solve_run=max_run,publications=len(transport.messages),forced_release_episodes=episodes,
            status_counts=dict(Counter(r['status'] for r in self.rows)),
            max_reference_speed_rad_s=max((abs(v) for m in transport.messages for v in m['velocity_rad_s'].values()),default=0.),
            close=closed)
