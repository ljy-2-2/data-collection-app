"""Bounded last-session evidence, consumed outside the robot control process."""
from collections import deque
import json
import os
from pathlib import Path
import threading
import time


class MotionTrace:
    def __init__(self, path, capacity=2000):
        self.path = Path(path)
        self.rows = deque(maxlen=capacity)
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.session = None
        self.dirty = False
        self.was_running = False
        self.error = ''
        self.saved_at = None
        self.dropped = 0

    def accept(self, row):
        running = row.get('teleop_state') == 'running'
        debug = row.get('motion_debug')
        with self.lock:
            if running and debug is not None:
                if not self.was_running or row.get('session') != self.session:
                    self.rows.clear()
                    self.session = row.get('session')
                self.rows.append({k: row.get(k) for k in (
                    'sequence', 'pc_monotonic_ns', 'pc_unix_ns', 'joints_deg',
                    'joint_feedback', 'joint_command', 'motion_debug', 'publisher_dropped')})
                self.dirty = True
                self.dropped = row.get('publisher_dropped', 0)
            ended = self.was_running and not running
            self.was_running = running
        if ended:
            self.flush('control_stopped')

    def flush(self, reason='app_closed'):
        # This runs on the backend IPC reader/shutdown thread, never on the
        # robot's timing thread. A slow disk cannot block SDK control writes.
        if not self.write_lock.acquire(timeout=2):
            self.error = '诊断保存仍在进行'
            return
        temporary = self.path.with_suffix('.tmp')
        try:
            with self.lock:
                if not self.dirty:
                    return
                rows = list(self.rows)
                self.dirty = False
                session = self.session
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(schema='tianji_motion_trace_v1', reason=reason,
                           session=session, saved_at=time.time(), rows=rows)
            temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
            os.replace(temporary, self.path)
            self.saved_at = payload['saved_at']
            self.error = ''
        except (OSError, ValueError, TypeError) as exc:
            self.error = str(exc)
            with self.lock:
                self.dirty = True
        finally:
            self.write_lock.release()

    def status(self):
        with self.lock:
            return dict(path=str(self.path), samples=len(self.rows), saved_at=self.saved_at,
                        error=self.error, publisher_dropped=self.dropped)
