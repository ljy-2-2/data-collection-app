"""Bounded FIFO separating ROS reception from compression and storage.

No resampling, timestamp rewriting or latest-only replacement. Overflow is an
explicit recording fault; already accepted data are drained before save.
"""
from collections import deque
import threading
import time


class RecordingQueue:
    def __init__(self, write, *, max_bytes=64 * 1024 * 1024, max_items=4096):
        self.write = write
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.condition = threading.Condition()
        self.items = deque()
        self.bytes = 0
        self.peak_bytes = 0
        self.peak_items = 0
        self.accepted = 0
        self.written = 0
        self.max_wait_ns = 0
        self.failure = None
        self.closing = False
        self.worker = threading.Thread(target=self._run, name='recording-writer', daemon=True)
        self.worker.start()

    def check(self):
        with self.condition:
            if self.failure is not None:
                raise RuntimeError('录制写入失败，保留已接收数据') from self.failure

    def submit(self, item, size):
        with self.condition:
            self.check()
            if self.closing:
                raise RuntimeError('Recording queue already closing')
            if size < 0 or size > self.max_bytes - self.bytes or len(self.items) >= self.max_items:
                raise BufferError('采集写入队列已满，结束并保存已接收数据，不丢弃旧帧继续录制')
            self.items.append((item, size, time.monotonic_ns()))
            self.accepted += 1
            self.bytes += size
            self.peak_bytes = max(self.peak_bytes, self.bytes)
            self.peak_items = max(self.peak_items, len(self.items))
            self.condition.notify()

    def _run(self):
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.items or self.closing)
                    if not self.items:
                        return
                    item, size, enqueued_ns = self.items.popleft()
                    self.max_wait_ns = max(self.max_wait_ns, time.monotonic_ns() - enqueued_ns)
                try:
                    self.write(item)
                    with self.condition:
                        self.written += 1
                finally:
                    with self.condition:
                        self.bytes -= size
        except BaseException as exc:
            with self.condition:
                self.failure = exc
                self.closing = True
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        self.worker.join()
        self.check()

    def statistics(self):
        with self.condition:
            return dict(accepted=self.accepted, written=self.written,
                        pending_bytes=self.bytes, peak_bytes=self.peak_bytes,
                        peak_items=self.peak_items, max_wait_ns=self.max_wait_ns,
                        capacity_bytes=self.max_bytes, capacity_items=self.max_items)
