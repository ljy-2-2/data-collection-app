"""Record disjoint episodes on one transport using real controller checkpoints."""
from collections import deque
import copy
import threading
import time

from fusion_camera import CameraError, StreamEvent
from fusion_camera.stream import joint_timing_gap


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
        self.joint_error = None
        self.attached = False
        self.failure = None
        self.clock_lock = threading.Lock()
        self.clock_pending = None
        self.clock_failure = None
        self.last_checkpoint_id = 0

    def publish(self, event):
        stop = False
        consumer = None
        with self.lock:
            kind = event.metadata['type']
            if kind == 'clock':
                pending = self.clock_pending
                if pending is not None:
                    pending['event'] = event
                    pending['done'].set()
                return
            if kind == 'checkpoint':
                consumer = self.consumer
                if consumer is None or not consumer.continuous or not consumer.stop_requested:
                    raise RuntimeError('收到不匹配的采集分段确认')
                metadata = event.metadata
                ident = metadata.get('checkpoint_id')
                if (metadata.get('ok') is not True or type(ident) is not int
                        or ident <= self.last_checkpoint_id
                        or metadata.get('boot_id') != self.clock['boot_id']
                        or metadata.get('transport_continues') is not True
                        or metadata.get('native_exit') is not None
                        or metadata.get('native_cleanup_performed') is not False):
                    raise RuntimeError('控制器分段标识不匹配')
                expected = [self.frame_counts[k] for k in ('usb-1','usb-2','usb-4')]
                if event.metadata.get('frames') != expected or event.metadata.get('joint_samples') != self.joint_count:
                    raise RuntimeError('控制器分段计数与实际接收数量不一致')
                consumer._seal_checkpoint(event)
                self.last_checkpoint_id = ident
                self.consumer = None
                self.attached = False
                return
            if kind == 'start':
                self.start = event
            elif kind == 'image':
                self.fresh[event.metadata['camera_id']] = time.monotonic()
                self.frame_counts[event.metadata['camera_id']] += 1
            elif kind == 'joints':
                self.joint_time = time.monotonic()
                self.joint_count += 1
            elif kind == 'health' and 'joint_feedback_ok' in event.metadata:
                if event.metadata['joint_feedback_ok'] is False and not joint_timing_gap(event.metadata):
                    self.joint_error = event.metadata.get('joint_feedback_error') or '关节反馈不可用'
                    self.joint_time = None
                elif event.metadata['joint_feedback_ok'] is True:
                    self.joint_error = None
            elif kind == 'end':
                self.end = event
            consumer = self.consumer
            if consumer is not None:
                stop = consumer._put(event)
        if stop:
            consumer.request_stop()

    def live_clock(self, samples=3):
        with self.clock_lock:
            rows = []
            for _ in range(samples):
                with self.lock:
                    if self.clock_failure or self.failure or self.end or self.finished.is_set():
                        raise RuntimeError(self.clock_failure or self.failure or '相机流已结束，无法同步时钟')
                    pending = {'done': threading.Event(), 'start': time.monotonic_ns()}
                    self.clock_pending = pending
                    try:
                        self.client.request_live_clock()
                    except Exception:
                        self.clock_pending = None
                        self.clock_failure = '连续流时钟请求失败'
                        raise
                if not pending['done'].wait(5):
                    with self.lock:
                        self.clock_pending = None
                        self.clock_failure = '连续流时钟响应超时'
                    raise RuntimeError(self.clock_failure)
                with self.lock:
                    self.clock_pending = None
                    if 'event' not in pending:
                        raise RuntimeError(self.failure or '时钟同步期间相机流结束')
                    metadata = pending['event'].metadata
                end = metadata['pc_recv_monotonic_ns']
                start = pending['start']
                if metadata.get('boot_id') != self.clock['boot_id'] or end < start:
                    raise RuntimeError('连续流时钟标识或时间顺序不匹配')
                rows.append(dict(offset_ns=metadata['controller_monotonic_ns']-(start+end)//2,
                    rtt_ns=end-start,uncertainty_ns=(end-start)//2,boot_id=metadata['boot_id'],
                    pc_midpoint_ns=(start+end)//2))
            return dict(min(rows,key=lambda row:row['rtt_ns']),measurements=rows,
                        method='live_stream_minimum_RTT_midpoint_estimate')

    def subscribe(self):
        with self.lock:
            if self.attached or self.end or self.failure or self.clock_failure or self.finished.is_set():
                raise RuntimeError('相机数据流当前无法接入采集，请检查设备')
            if self.joint_error:
                raise RuntimeError('视频正常，关节反馈尚未恢复，暂不能采集：'+self.joint_error)
            selected = self.start.metadata.get('cameras', list(self.frame_counts)) if self.start else []
            if (not isinstance(selected, list) or not selected or len(set(selected)) != len(selected)
                    or not set(selected) <= set(self.frame_counts)):
                raise RuntimeError('相机流未提供有效的本轮相机清单')
            if (self.start is None or self.start.metadata.get('boot_id') != self.clock.get('boot_id')
                    or any(name not in self.fresh for name in selected)
                    or self.joint_time is None):
                raise RuntimeError('请等待本轮 %d 路相机和关节反馈正常后再按 A' % len(selected))
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
            if self.clock_pending:
                self.clock_pending['done'].set()
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
        self.continuous = bool(session.start.metadata.get('stream_checkpoints') is True
                               and session.start.metadata.get('stream_live_clock') is True)
        self.boundary_received = threading.Event()
        metadata = copy.deepcopy(session.start.metadata)
        metadata['adopted_preview_stream'] = True
        metadata['continuous_episode'] = self.continuous
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

    def _seal_checkpoint(self, event):
        metadata = copy.deepcopy(event.metadata)
        metadata['type'] = 'end'
        metadata['source_event_type'] = 'checkpoint'
        metadata['continuous_episode'] = True
        # This is a real controller boundary, never a claimed native process exit.
        self._put(StreamEvent(metadata))
        self.boundary_received.set()
        self.changed.notify_all()

    def validate_stream(self):
        with self.session.lock:
            if self.closed or self.session.failure or self.session.end:
                raise RuntimeError(self.session.failure or '预览流已结束，无法开始本轮采集')

    def clock_sync(self, samples=3):
        self.clock_calls += 1
        if self.continuous:
            return self.session.live_clock(samples=samples)
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

    def stream(self, seconds=0, hz=30, joint_hz=100, mode='teleop_observation', camera_ids=None):
        if self.stream_started or mode != 'teleop_observation' or hz != 30 or joint_hz != 100:
            raise ValueError('Invalid adopted camera stream request')
        if seconds == 0 and self.session.start.metadata.get('seconds') != 0:
            raise ValueError('Unlimited recording requires an unlimited preview stream')
        selected = self.session.start.metadata.get('cameras', list(self.session.frame_counts))
        if camera_ids is not None and set(camera_ids) != set(selected):
            raise ValueError('Requested camera selection differs from adopted stream')
        self.stream_started = True
        while True:
            with self.changed:
                while not self.queue and not self.closed and not self.error and not self.session.failure and not self.session.finished.is_set():
                    self.changed.wait(timeout=1)
                if self.closed:
                    raise RuntimeError('采集相机订阅已关闭')
                if self.error or self.session.failure:
                    raise RuntimeError(self.error or self.session.failure)
                if not self.queue:
                    raise RuntimeError('采集未收到真实的相机结束审计')
                event = self.queue.popleft()
                self.bytes -= len(event.data)
            if event.metadata['type'] == 'end':
                self.end_received = True
                self.boundary_received.set()
                yield event
                if not event.metadata.get('ok'):
                    raise CameraError(event.metadata)
                return
            yield event

    def request_stop(self):
        with self.session.lock:
            if not self.stop_requested and self.session.end is None:
                self.stop_requested = True
                if self.continuous:
                    self.session.client.request_checkpoint()
                else:
                    self.session.client.request_stop()

    def close(self):
        self.request_stop()
        if self.continuous:
            with self.changed:
                until = time.monotonic()+12
                while not self.boundary_received.is_set() and not self.session.finished.is_set():
                    left = until-time.monotonic()
                    if left <= 0:
                        self.session.failure = '采集分段未收到控制器确认'
                        self.session.client.request_stop()
                        raise RuntimeError(self.session.failure)
                    self.changed.wait(left)
        with self.session.lock:
            self.closed = True
            self.queue.clear()
            self.bytes = 0
            self.changed.notify_all()
        if not self.continuous and not self.session.finished.wait(12):
            raise RuntimeError('采集结束后相机仍未释放，请检查设备')
