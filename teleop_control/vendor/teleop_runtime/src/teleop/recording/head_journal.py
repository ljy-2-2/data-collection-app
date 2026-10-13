"""Bounded async control journal: no disk waits on the control thread.

Enqueued is NOT durable: a crash may lose the unflushed tail. Health failure
stops subsequent sends, rather than extending command TTL to cover slow disks.
"""
import json
import os
import queue
import threading
import time


def _sync(file):
    file.flush()
    os.fsync(file.fileno())


class AsyncHeadJournal:
    def __init__(self, path, *, capacity=2048, sync=_sync):
        self.file = open(path, 'x', encoding='utf-8')
        self.queue = queue.Queue(maxsize=capacity)
        self.stop = threading.Event()
        self.failure = None
        self.last_sync = time.monotonic()
        self.sync = sync
        self.thread = threading.Thread(target=self._run, daemon=True, name='head-journal')
        try:
            _sync(self.file)  # before any hardware initialization
            self.thread.start()
        except BaseException:
            self.file.close()
            raise

    def _run(self):
        try:
            while not self.stop.is_set() or not self.queue.empty():
                try:
                    self.file.write(self.queue.get(timeout=.05))
                except queue.Empty:
                    pass
                if time.monotonic()-self.last_sync >= .1:
                    self.sync(self.file)
                    self.last_sync = time.monotonic()
            self.sync(self.file)
            self.last_sync = time.monotonic()
        except BaseException as error:
            self.failure = error
        finally:
            try: self.file.close()
            except BaseException as error: self.failure = error

    def check_health(self):
        if self.failure is not None:
            raise RuntimeError(f'head journal failed: {self.failure}')
        if self.stop.is_set(): raise RuntimeError('head journal closed')
        if time.monotonic()-self.last_sync > .5:
            self.failure = RuntimeError('head journal persistence stalled for 0.5 seconds')
            raise self.failure

    def write(self, event, **data):
        self.check_health()
        line = json.dumps(dict(event=event, host_monotonic_ns=time.monotonic_ns(), **data),
                          ensure_ascii=False, allow_nan=False) + '\n'
        try: self.queue.put_nowait(line)
        except queue.Full:
            self.failure = RuntimeError('head journal queue full; no further sends')
            raise self.failure

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive(): raise RuntimeError('head journal did not finish within 2 seconds')
        if self.failure is not None: raise RuntimeError(f'head journal failed: {self.failure}')
