"""Read the existing host journal without importing a robot or CAN backend.

The journal records a laptop monotonic receipt time; each gripper report carries
its own robot monotonic time. Only differences within one clock domain are used.
Repeated browser polls never create feedback samples or renew their freshness.
"""

from collections import deque
import copy
import json
import math
import os
from pathlib import Path
import stat
import threading
import time


SIDES = ('left', 'right')
MAX_LINE_BYTES = 256 * 1024
READ_BUDGET = 2 * 1024 * 1024
DEFAULT_HISTORY = 300


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def integer(value):
    return value if type(value) is int and value >= 0 else None


def finite_json(value):
    """Keep older/corrupt nonfinite telemetry from producing invalid JSON."""
    if isinstance(value, dict):
        return {str(k): finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(v) for v in value]
    if type(value) is float and not math.isfinite(value):
        return None
    return value


class GripperJournalView:
    def __init__(self, path, *, clock=time.monotonic_ns, history=DEFAULT_HISTORY):
        if type(history) is not int or not 1 <= history <= 10000:
            raise ValueError('history must be 1..10000')
        self.path = Path(path).expanduser().resolve()
        self.clock = clock
        self._lock = threading.Lock()
        self._history_size = history
        self._identity = None
        self._offset = 0
        self._pending = b''
        self._skip_until_newline = False
        self._reset()

    def _reset(self):
        self.latest = None
        self.local_received_ns = None
        self.latest_event = None
        self.journal_closed = False
        self.malformed_records = 0
        self.records_seen = 0
        self.status_records = 0
        self.skipped_initial_bytes = 0
        self.error = None
        self.history = {s: deque(maxlen=self._history_size) for s in SIDES}
        self._seen_feedback = dict.fromkeys(SIDES)
        self._sample_counts = dict.fromkeys(SIDES, 0)
        self._generation = None

    def _consume(self, raw):
        try:
            row = json.loads(raw)
            if not isinstance(row, dict) or not isinstance(row.get('packet'), dict):
                raise ValueError('expected a host journal record')
            packet = row['packet']
            stamp = integer(row.get('local_monotonic_ns'))
            if stamp is None:
                raise ValueError('host receipt timestamp missing')
        except (ValueError, UnicodeError):
            self.malformed_records += 1
            return
        self.records_seen += 1
        event = packet.get('event')
        if event in ('closed', 'refused', 'eof'):
            self.journal_closed = True
            self.latest_event = str(event)
        report = packet.get('grippers')
        if not isinstance(report, dict) or not isinstance(report.get('motors'), dict):
            return
        if self.local_received_ns is not None and stamp < self.local_received_ns:
            # Concatenated logs cannot renew a newer session with older rows.
            self.malformed_records += 1
            return
        generation = report.get('generation')
        if self.latest is not None and generation != self._generation:
            self._seen_feedback = dict.fromkeys(SIDES)
            for rows in self.history.values():
                rows.clear()
        self._generation = generation
        self.latest = finite_json(copy.deepcopy(report))
        self.local_received_ns = stamp
        self.latest_event = str(event or 'unknown')
        self.status_records += 1
        for side in SIDES:
            motor = report['motors'].get(side)
            if not isinstance(motor, dict):
                continue
            feedback = motor.get('feedback')
            feedback = feedback if isinstance(feedback, dict) else {}
            seq, received = integer(feedback.get('sequence')), integer(feedback.get('received_ns'))
            identity = (seq, received) if seq is not None and received is not None else None
            previous = self._seen_feedback[side]
            fresh_sample = (identity is not None and
                            (previous is None or (identity[0] > previous[0] and identity[1] > previous[1])))
            if fresh_sample:
                self._seen_feedback[side] = identity
                self._sample_counts[side] += 1
            # Every row is a status snapshot, not a synthetic 100 Hz motor frame.
            self.history[side].append(dict(
                local_monotonic_ns=stamp,
                desired_closure_fraction=number(motor.get('desired_closure_fraction', motor.get('trigger'))),
                target_closure_fraction=number(motor.get('target_closure_fraction')),
                measured_closure_fraction=number(motor.get('measured_closure_fraction')) if fresh_sample else None,
                target_rad=number(motor.get('target_rad')),
                position_rad=number(feedback.get('position_rad')) if fresh_sample else None,
                feedback_sequence=seq if fresh_sample else None,
                new_feedback_sample=fresh_sample,
            ))

    def _refresh(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                file_stat = os.fstat(source.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError('journal must be a regular local file')
                identity = (file_stat.st_dev, file_stat.st_ino)
                if self._identity != identity or file_stat.st_size < self._offset:
                    self._reset()
                    self._identity = identity
                    self._offset = max(0, file_stat.st_size - READ_BUDGET)
                    self.skipped_initial_bytes = self._offset
                    self._pending = b''
                    self._skip_until_newline = self._offset > 0
                source.seek(self._offset)
                data = source.read(READ_BUDGET)
                self._offset += len(data)
            self.error = None
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            return
        data = self._pending + data
        self._pending = b''
        if self._skip_until_newline:
            delimiter = data.find(b'\n')
            if delimiter < 0:
                return
            data = data[delimiter+1:]
            self._skip_until_newline = False
        lines = data.split(b'\n')
        self._pending = lines.pop()
        for line in lines:
            if not line.strip():
                continue
            if len(line) > MAX_LINE_BYTES:
                self.malformed_records += 1
            else:
                self._consume(line)
        if len(self._pending) > MAX_LINE_BYTES:
            self.malformed_records += 1
            self._pending = b''
            self._skip_until_newline = True

    def snapshot(self):
        with self._lock:
            self._refresh()
            now = self.clock()
            elapsed = ((now-self.local_received_ns)/1e6
                       if self.local_received_ns is not None and now >= self.local_received_ns else None)
            report = self.latest or {}
            observed_ns = integer(report.get('observed_ns'))
            motors = {}
            for side in SIDES:
                original = report.get('motors', {}).get(side)
                motor = copy.deepcopy(original) if isinstance(original, dict) else {}
                feedback = motor.get('feedback')
                feedback = feedback if isinstance(feedback, dict) else None
                received_ns = integer(feedback.get('received_ns')) if feedback else None
                base_age = None
                if observed_ns is not None and received_ns is not None and observed_ns >= received_ns:
                    base_age = (observed_ns-received_ns)/1e6
                age = base_age+elapsed if base_age is not None and elapsed is not None else None
                limit = number(motor.get('feedback_timeout_ms'))
                fresh = (None if feedback is not None and (age is None or limit is None) else
                         bool(feedback is not None and age < limit and not self.journal_closed and not self.error))
                fresh_at_report = (None if feedback is not None and (base_age is None or limit is None) else
                                   bool(feedback is not None and base_age < limit))
                if feedback is None:
                    motor['measured_closure_fraction'] = None
                motor.update(
                    feedback_age_now_ms=age,
                    feedback_fresh=fresh,
                    feedback_fresh_at_report=fresh_at_report,
                    observed_feedback_samples=self._sample_counts[side],
                    calibration=(motor.get('calibration') if isinstance(motor.get('calibration'), dict)
                                 else report.get('calibration', {}).get(side)
                                 if isinstance(report.get('calibration'), dict) else None),
                )
                motors[side] = motor
            return dict(
                observer='local_journal_only', robot_connection_opened=False,
                can_frames_sent=0, path=str(self.path), error=self.error,
                journal_closed=self.journal_closed, latest_event=self.latest_event,
                report_age_ms=elapsed, clock_comparable=elapsed is not None,
                mode=report.get('mode', 'no_data'), closed=report.get('closed'),
                status_records=self.status_records, malformed_records=self.malformed_records,
                skipped_initial_bytes=self.skipped_initial_bytes,
                observed_ns=observed_ns, generation=self._generation,
                motors=motors, history={s: list(rows) for s, rows in self.history.items()},
                sampling='status snapshots; repeated feedback is not resampled',
                freshness_scope='age of recorded feedback; not a direct live CAN health measurement',
                raw_report=copy.deepcopy(self.latest),
            )
