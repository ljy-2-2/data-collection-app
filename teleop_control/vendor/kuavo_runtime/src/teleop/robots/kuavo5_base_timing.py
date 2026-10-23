"""Bounded, local-clock diagnostics only; never used to extend input authority."""
from collections import deque
import threading


class InputTiming:
    def __init__(self):
        self._lock = threading.Lock()
        self._stages = {}
        self._recent = deque(maxlen=16)

    def mark(self, stage, now_ns, *, record=False, **details):
        with self._lock:
            data = self._stages.setdefault(stage, dict(count=0, last_ns=None, max_gap_ms=0.))
            if data['last_ns'] is not None:
                data['max_gap_ms'] = max(data['max_gap_ms'], (now_ns-data['last_ns'])/1e6)
            data['last_ns'] = now_ns
            data['count'] += 1
            if record:
                self._recent.append(dict(stage=stage, local_time_ns=now_ns, **details))

    def span(self, stage, duration_ns):
        with self._lock:
            data = self._stages.setdefault(stage, dict(count=0, last_duration_ms=0., max_duration_ms=0.))
            data['count'] += 1
            data['last_duration_ms'] = duration_ns/1e6
            data['max_duration_ms'] = max(data['max_duration_ms'], duration_ns/1e6)

    def snapshot(self, now_ns):
        with self._lock:
            return dict(clock='local_monotonic_only', snapshot_ns=now_ns,
                        stages={k:dict(v) for k,v in self._stages.items()},
                        recent=[dict(v) for v in self._recent])


class InputLeaseMonitor:
    """Non-motion probe observes the SAME conservative expiry as live control.

    Inter-arrival gap alone understates expiry: a received frame may already
    be 20ms old. A fresh accepted frame is considered before checking silence.
    """
    def __init__(self):
        self.accepted=0
        self.last_arrival_ns=None
        self.deadline_ns=None
        self.timeouts=0
        self.timeout_open=False
        self.max_age_ms=0.
        self.max_gap_ms=0.
        self.max_silence_ms=0.

    def observe(self, now_ns, frame=None):
        from teleop.robots.kuavo5_live_protocol import INPUT_TTL_NS
        if self.last_arrival_ns is not None:
            self.max_silence_ms=max(self.max_silence_ms,(now_ns-self.last_arrival_ns)/1e6)
        if frame is not None:
            self.accepted+=1
            if self.last_arrival_ns is not None:
                self.max_gap_ms=max(self.max_gap_ms,(now_ns-self.last_arrival_ns)/1e6)
            self.last_arrival_ns=now_ns
            self.deadline_ns=frame.received_monotonic_ns+INPUT_TTL_NS
            self.max_age_ms=max(self.max_age_ms,(now_ns-frame.received_monotonic_ns)/1e6)
        expired=self.deadline_ns is not None and now_ns>=self.deadline_ns
        if expired and not self.timeout_open: self.timeouts+=1
        self.timeout_open=expired
