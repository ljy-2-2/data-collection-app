"""PC input selection/scaling around the unmodified original KUAVO policy."""
from dataclasses import replace
import math
import queue
import threading
import time
from .kuavo_preview import measured_feedback, measured_grippers


class InputAdapter:
    def __init__(self, source, arm_mode='dual', position_scale=1., observation=None, activation=None):
        if arm_mode not in ('dual','left','right','dual_base'):raise ValueError('无效遥操作范围')
        if type(position_scale) not in (int,float) or not math.isfinite(position_scale) or not .1<=position_scale<=1:
            raise ValueError('位移倍率须为 0.1–1')
        self.source, self.mode, self.scale, self.observation = source, arm_mode, position_scale, observation
        self.activation=activation
        self.base_gate=ModeBaseGate(observation)

    def start(self):return self.source.start()
    def close(self):return self.source.close()

    def read_frame(self):
        frame = self.source.read_frame()
        if self.observation is not None:self.observation.input(frame)
        if frame is None:return frame
        if self.activation is not None:
            frame=self.activation.filter(frame)
            self.mode=self.activation.mode or self.mode
        base_enabled=self.activation is None or self.activation.enabled
        if self.observation is not None and hasattr(self.observation,'collection_input'):self.observation.collection_input(frame,base_enabled)
        base_pulse=self.base_gate.pulse(frame) if self.mode=='dual_base' and base_enabled else False
        def pose(value):return replace(value,position_m=tuple(v*self.scale for v in value.position_m))
        def controller(side):
            value = getattr(frame,side+'_controller')
            buttons = value.buttons
            if side=='left':
                # Y intentionally returns both arms/grippers in every mode.
                # Physical X is ignored; mode selection owns base admission.
                buttons=replace(buttons, primary=base_pulse)
            active = self.mode in ('dual','dual_base',side)
            # Right B cancellation remains available even in left-only mode.
            return replace(value,pose=pose(value.pose),buttons=buttons,
                           axis=value.axis if self.mode=='dual_base' else (0.,0.),
                           grip=value.grip if active else 0.,trigger=value.trigger if active else 0.)
        return replace(frame,head_pose=pose(frame.head_pose),left_controller=controller('left'),right_controller=controller('right'))


def neutral(frame):
    return frame is not None and not frame.stale and all(
        h.grip<=.05 and h.trigger<=.05 and all(abs(x)<=.01 for x in h.axis)
        and not h.buttons.primary and not h.buttons.secondary
        for h in (frame.left_controller,frame.right_controller))


class ActivationGate:
    """Prepare the real session with neutral inputs; explicit start releases it."""
    def __init__(self, standby=False):
        self.requested=threading.Event()
        self.enabled=not standby
        self.input_neutral=False
        self.mode=None
        if not standby:self.requested.set()

    def activate(self, mode=None):
        if mode is not None:
            if mode not in ('dual','left','right','dual_base'):raise ValueError('无效遥操作范围')
            self.mode=mode
        self.requested.set()

    def filter(self, frame):
        self.input_neutral=neutral(frame)
        if self.requested.is_set() and self.input_neutral:self.enabled=True
        if self.enabled:return frame
        def released(hand):
            return replace(hand,axis=(0.,0.),grip=0.,trigger=0.,
                           buttons=replace(hand.buttons,primary=False,secondary=False))
        return replace(frame,left_controller=released(frame.left_controller),right_controller=released(frame.right_controller))


class ModeBaseGate:
    """Translate selected mode into the installed base admission handshake.

    Never re-toggle an unlocked base. A fault/timeout needs a new session;
    only initial admission or a completed/cancelled Y return can re-arm.
    """
    def __init__(self, observation):
        self.observation=observation
        self.sent=False;self.neutral_since=None;self.return_generation=0

    def pulse(self, frame):
        if self.observation is None:return False
        info=self.observation.snapshot()
        generation=info.get('base_return_generation',0)
        if generation!=self.return_generation:
            self.return_generation=generation;self.sent=False;self.neutral_since=None
        base=info.get('base_status') or {}
        age=info.get('base_status_age_s')
        allowed=(age is not None and age<.5 and base.get('status')=='locked'
                 and not base.get('error') and base.get('stop_observed') is True
                 and 'authority revoked' in str(base.get('reason','')))
        if self.sent or not allowed or not neutral(frame):
            self.neutral_since=None
            return False
        if self.neutral_since is None:self.neutral_since=frame.device_timestamp_ns
        if frame.device_timestamp_ns-self.neutral_since<100_000_000:return False
        self.sent=True
        return True


class Observation:
    """Latest samples only. Control readers never wait for JSON/UI rendering."""
    def __init__(self):
        self.lock=threading.Lock();self.feedback=None;self.feedback_at=0.;self.pico_at=0.;self.pico_stamp=0
        self.execution_stage='connecting';self.robot_refusal=''
        self.base_status={};self.base_at=None;self.base_return_generation=0
        self.grippers={};self.grippers_at={}
        self.collection_queue=queue.Queue(maxsize=1024);self.collection_overflow=False
        self.collection_stamp=None

    def collection_event(self, value):
        try:self.collection_queue.put_nowait(value)
        except queue.Full:self.collection_overflow=True

    def collection_input(self,frame,enabled):
        if frame is None:return
        if frame.device_timestamp_ns==self.collection_stamp:return
        self.collection_stamp=frame.device_timestamp_ns
        self.collection_event(dict(kind='input',pc_monotonic_ns=time.monotonic_ns(),fresh=not frame.stale,enabled=enabled,
            buttons=dict(A=frame.right_controller.buttons.primary,B=frame.right_controller.buttons.secondary,
                         X=frame.left_controller.buttons.primary,Y=frame.left_controller.buttons.secondary)))

    def drain_collection(self):
        rows=[]
        while True:
            try:rows.append(self.collection_queue.get_nowait())
            except queue.Empty:break
        overflow,self.collection_overflow=self.collection_overflow,False
        return dict(collection_events=rows,collection_overflow=overflow)

    def input(self, frame):
        if frame is not None and not frame.stale:
            with self.lock:
                if frame.device_timestamp_ns>self.pico_stamp:
                    self.pico_stamp=frame.device_timestamp_ns;self.pico_at=time.monotonic()

    def packet(self, packet):
        if isinstance(packet,dict) and packet.get('event') in ('control_trace','ik_trace','status','gripper_trace'):
            self.collection_event(dict(kind='packet',pc_monotonic_ns=time.monotonic_ns(),packet=packet))
        grippers=measured_grippers(packet)
        with self.lock:
            for side, sample in grippers.items():
                if sample['timestamp_ns'] > self.grippers.get(side,{}).get('timestamp_ns',0):
                    self.grippers[side]=sample;self.grippers_at[side]=time.monotonic()
        if isinstance(packet,dict):
            with self.lock:
                if packet.get('event')=='compute_preparing':self.execution_stage='compute_preparing'
                elif packet.get('event')=='ready':self.execution_stage='verifying_ready'
                elif packet.get('event')=='refused':self.robot_refusal=str(packet.get('reason') or '机器人拒绝会话')
                if isinstance(packet.get('base'),dict):
                    self.base_status=dict(packet['base']);self.base_at=time.monotonic()
                if packet.get('event') in ('return_completed','return_cancelled'):
                    self.base_return_generation+=1
        value=measured_feedback(packet)
        if value is not None:
            with self.lock:
                if self.feedback is None or value['timestamp_ns']>self.feedback['timestamp_ns']:
                    self.feedback=value;self.feedback_at=time.monotonic()

    def snapshot(self):
        with self.lock:
            now=time.monotonic()
            return dict(telemetry={**(self.feedback or {}), 'age_s':now-self.feedback_at if self.feedback_at else None,
                                  'grippers':{side:{**sample,'age_s':sample['age_s']+now-self.grippers_at[side]}
                                              for side,sample in self.grippers.items()}},
                        pico_live=bool(self.pico_at and now-self.pico_at<.5),
                        execution_stage=self.execution_stage,robot_refusal=self.robot_refusal,
                        base_status=self.base_status.copy(),base_status_age_s=now-self.base_at if self.base_at is not None else None,
                        base_return_generation=self.base_return_generation)


class ObservedChannel:
    def __init__(self, channel, observation):self.channel,self.observation=channel,observation
    def __getattr__(self,name):return getattr(self.channel,name)
    def receive(self):
        packet=self.channel.receive()
        self.observation.packet(packet)
        return packet
