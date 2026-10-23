"""PC input selection/scaling around the unmodified original KUAVO policy."""
from dataclasses import replace
import math
import threading
import time
from .kuavo_preview import measured_feedback


class InputAdapter:
    def __init__(self, source, arm_mode='dual', position_scale=1., observation=None):
        if arm_mode not in ('dual','left','right'):raise ValueError('无效手臂模式')
        if type(position_scale) not in (int,float) or not math.isfinite(position_scale) or not .1<=position_scale<=1:
            raise ValueError('位移倍率须为 0.1–1')
        self.source, self.mode, self.scale, self.observation = source, arm_mode, position_scale, observation

    def start(self):return self.source.start()
    def close(self):return self.source.close()

    def read_frame(self):
        frame = self.source.read_frame()
        if self.observation is not None:self.observation.input(frame)
        if frame is None or (self.mode=='dual' and self.scale==1):return frame
        def pose(value):return replace(value,position_m=tuple(v*self.scale for v in value.position_m))
        def controller(side):
            value = getattr(frame,side+'_controller')
            buttons = value.buttons
            if side=='left' and self.mode!='dual':buttons=replace(buttons,secondary=False)
            active = self.mode in ('dual',side)
            # Right B cancellation remains available even in left-only mode.
            return replace(value,pose=pose(value.pose),buttons=buttons,
                           grip=value.grip if active else 0.,trigger=value.trigger if active else 0.)
        return replace(frame,head_pose=pose(frame.head_pose),left_controller=controller('left'),right_controller=controller('right'))


class Observation:
    """Latest samples only. Control readers never wait for JSON/UI rendering."""
    def __init__(self):
        self.lock=threading.Lock();self.feedback=None;self.feedback_at=0.;self.pico_at=0.;self.pico_stamp=0

    def input(self, frame):
        if frame is not None and not frame.stale:
            with self.lock:
                if frame.device_timestamp_ns>self.pico_stamp:
                    self.pico_stamp=frame.device_timestamp_ns;self.pico_at=time.monotonic()

    def packet(self, packet):
        value=measured_feedback(packet)
        if value is not None:
            with self.lock:
                if self.feedback is None or value['timestamp_ns']>self.feedback['timestamp_ns']:
                    self.feedback=value;self.feedback_at=time.monotonic()

    def snapshot(self):
        with self.lock:
            now=time.monotonic()
            return dict(telemetry={**(self.feedback or {}), 'age_s':now-self.feedback_at if self.feedback_at else None},
                        pico_live=bool(self.pico_at and now-self.pico_at<.5))


class ObservedChannel:
    def __init__(self, channel, observation):self.channel,self.observation=channel,observation
    def __getattr__(self,name):return getattr(self.channel,name)
    def receive(self):
        packet=self.channel.receive()
        self.observation.packet(packet)
        return packet
