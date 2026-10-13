"""Adopt an already running preview stream; retain its real audited end."""
from collections import deque
import copy
import threading
import time

from fusion_camera import CameraError, StreamEvent


class CameraSession:
    def __init__(self, client, clock, client_factory):
        self.client, self.clock, self.client_factory = client, copy.deepcopy(clock), client_factory
        self.lock = threading.RLock()
        self.finished = threading.Event()
        self.start = self.end = self.consumer = None
        self.fresh = {}
        self.frame_counts = dict.fromkeys(('usb-1', 'usb-2', 'usb-4'), 0)
        self.joint_count = 0
        self.joint_time = None
        self.attached = False
        self.failure = None

    def publish(self, event):
        stop = False
        with self.lock:
            kind = event.metadata['type']
            if kind == 'start':
                self.start = event
            elif kind == 'image':
                self.fresh[event.metadata['camera_id']] = time.monotonic()
                self.frame_counts[event.metadata['camera_id']] += 1
            elif kind == 'joints':
                self.joint_time = time.monotonic()
                self.joint_count += 1
            elif kind == 'end':
                self.end = event
            if self.consumer is not None:
                stop = self.consumer._put(event)
        if stop:
            self.consumer.request_stop()

    def subscribe(self):
        with self.lock:
            now = time.monotonic()
            if self.attached or self.end or self.failure or self.finished.is_set():
                raise RuntimeError('相机数据流当前无法接入采集，请检查设备')
            if (self.start is None or self.start.metadata.get('boot_id') != self.clock.get('boot_id')
                    or any(now-self.fresh.get(name, -1e9) >= 2 for name in ('usb-1','usb-2','usb-4'))
                    or self.joint_time is None or now-self.joint_time >= .3):
                raise RuntimeError('请等待三路相机和关节反馈正常后再按 A')
            self.consumer = AdoptedCamera(self)
            self.attached = True
            return self.consumer

    def finish(self, error=None):
        with self.lock:
            if error and self.end is None:
                self.failure = str(error)
            if self.end is None:
                self.failure = self.failure or '相机流结束但未收到真实审计结果'
            if self.consumer:
                self.consumer.changed.notify_all()
            self.finished.set()


class AdoptedCamera:
    MAX_EVENTS = 256
    MAX_BYTES = 32*1024*1024

    def __init__(self, session):
        self.session = session
        self.changed = threading.Condition(session.lock)
        self.queue = deque()
        self.bytes = 0
        self.closed = self.stop_requested = self.stream_started = self.end_received = False
        self.error = None
        self.clock_calls = 0
        metadata = copy.deepcopy(session.start.metadata)
        metadata['adopted_preview_stream'] = True
        metadata['episode_subscription_pc_monotonic_ns'] = time.monotonic_ns()
        metadata['episode_count_baseline'] = dict(
            frames=dict(session.frame_counts), joint_samples=session.joint_count,
            source='received_before_subscription')
        # This is the actual earlier stream start, not a fabricated new hardware start.
        self._put(StreamEvent(metadata))

    def _put(self, event):
        if self.closed or self.error:
            return False
        if len(self.queue) >= self.MAX_EVENTS or self.bytes+len(event.data) > self.MAX_BYTES:
            self.error = '采集接收队列已满，本轮中止；未静默丢弃图像'
            self.changed.notify_all()
            return True
        self.queue.append(event)
        self.bytes += len(event.data)
        self.changed.notify_all()
        return False

    def validate_stream(self):
        with self.session.lock:
            if self.closed or self.session.failure or self.session.end:
                raise RuntimeError(self.session.failure or '预览流已结束，无法开始本轮采集')

    def clock_sync(self, samples=3):
        self.clock_calls += 1
        if self.clock_calls == 1:
            return copy.deepcopy(self.session.clock)
        # The original owner must close its stream before the serial service
        # can answer a new clock request. No service queries during streaming.
        if not self.end_received or not self.session.finished.wait(12):
            raise RuntimeError('相机尚未完成真实停止，无法读取结束时钟')
        client = self.session.client_factory()
        try:
            return client.clock_sync(samples=samples)
        finally:
            client.close()

    def stream(self, seconds=600, hz=30, joint_hz=100, mode='teleop_observation'):
        if self.stream_started or mode != 'teleop_observation' or hz != 30 or joint_hz != 100:
            raise ValueError('Invalid adopted camera stream request')
        self.stream_started = True
        while True:
            with self.changed:
                while not self.queue and not self.error and not self.session.failure and not self.session.finished.is_set():
                    if not self.changed.wait(timeout=3):
                        self.error = '采集相机事件超过 3 秒未更新'
                if self.error or self.session.failure:
                    raise RuntimeError(self.error or self.session.failure)
                if not self.queue:
                    raise RuntimeError('采集未收到真实的相机结束审计')
                event = self.queue.popleft()
                self.bytes -= len(event.data)
            if event.metadata['type'] == 'end':
                self.end_received = True
                yield event
                if not event.metadata.get('ok'):
                    raise CameraError(event.metadata)
                return
            yield event

    def request_stop(self):
        with self.session.lock:
            if not self.stop_requested and self.session.end is None:
                self.stop_requested = True
                self.session.client.request_stop()

    def close(self):
        self.request_stop()
        with self.session.lock:
            self.closed = True
            self.queue.clear()
            self.bytes = 0
        if not self.session.finished.wait(12):
            raise RuntimeError('采集结束后相机仍未释放，请检查设备')
