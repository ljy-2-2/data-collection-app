"""Bounded PC-only journal writer; never performs robot or SDK I/O."""
import copy
import json
from queue import Empty, Full, Queue
import threading
import time


class AsyncJournal:
    """Keep disk serialization/flush outside the input challenge response path.

    Packets are snapshotted before enqueue. Overflow is an error, not a
    silent loss of evidence. close() drains normally; a blocked sink is reported
    and no subsequent writes are started. The caller still owns the sink.
    """
    def __init__(self, sink, *, capacity=4096):
        self._sink = sink
        self._queue = Queue(maxsize=capacity)
        self._closing = threading.Event()
        self._abort = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, name='kuavo-journal', daemon=True)
        self._thread.start()

    def check(self):
        if self._error is not None:
            raise RuntimeError('journal failed: '+str(self._error))

    def record(self, packet, *, now_ns=None):
        self.check()
        if self._closing.is_set():
            raise RuntimeError('journal already closing')
        record = dict(local_monotonic_ns=time.monotonic_ns() if now_ns is None else now_ns,
                      packet=copy.deepcopy(packet))
        try:
            self._queue.put_nowait(record)
        except Full:
            raise RuntimeError('journal backpressure; input session must close')

    def _run(self):
        try:
            while not self._abort.is_set():
                try:
                    record = self._queue.get(timeout=.02)
                except Empty:
                    if self._closing.is_set():
                        return
                    continue
                if self._abort.is_set():
                    return
                self._sink.write(json.dumps(record, allow_nan=False)+'\n')
                if not self._abort.is_set():
                    self._sink.flush()
        except Exception as exc:
            self._error = exc

    def close(self):
        self._closing.set()
        self._thread.join(timeout=5.)
        if self._thread.is_alive():
            self._abort.set()
            raise RuntimeError('journal sink blocked; final evidence not fully persisted')
        self.check()


class AsyncConsole:
    """Bounded, best-effort display; authoritative packets stay in the journal.

    A slow terminal must not stall the input or stderr-draining threads. Keep
    recent lines when full; report omissions/errors on close. Never close the
    caller's stream, or wait indefinitely for an in-progress terminal write.
    """
    def __init__(self, sink, *, capacity=16):
        from collections import deque
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('positive console capacity required')
        self._sink, self._capacity = sink, capacity
        self._pending = deque()
        self._condition = threading.Condition()
        self._closing = False
        self._dropped = 0
        self._error = None
        self._thread = threading.Thread(target=self._run, name='kuavo-console', daemon=True)
        self._thread.start()

    def write(self, text):
        if not isinstance(text, str):
            raise ValueError('console line must be text')
        with self._condition:
            if self._closing or self._error is not None:
                self._dropped += 1
                return
            if len(self._pending) >= self._capacity:
                self._pending.popleft()
                self._dropped += 1
            self._pending.append(text)
            self._condition.notify()

    def _run(self):
        while True:
            with self._condition:
                while not self._pending and not self._closing:
                    self._condition.wait()
                if not self._pending:
                    return
                line = self._pending.popleft()
            try:
                self._sink.write(line+'\n')
                self._sink.flush()
            except Exception as exc:
                with self._condition:
                    self._error = str(exc)
                    self._dropped += 1+len(self._pending)
                    self._pending.clear()
                return

    def close(self):
        with self._condition:
            self._closing = True
            self._condition.notify()
        self._thread.join(timeout=.1)
        with self._condition:
            blocked = self._thread.is_alive()
            if blocked:
                self._dropped += len(self._pending)
                self._pending.clear()
            return dict(dropped_lines=self._dropped, writer_blocked=blocked,
                        error=self._error)
