"""Optional read-only localhost telemetry. Never opens a second robot SDK.

Control thread only queues immutable references. JSON/socket work runs separately;
loss is counted, never blocks or raises into robot control. No incoming commands.
"""
import json
from collections import deque
from itertools import islice
import queue
import socket
import threading
import time
import uuid

PORT = 28765
SIDES = ("left", "right")
FEEDBACK_CAPACITY = 1024
FEEDBACK_PER_PACKET = 32


def append_feedback(pending, incoming, dropped):
    """Keep the newest bounded real samples and account for every evicted sample."""
    for side in SIDES:
        for sample in incoming.get(side, ()):
            if len(pending[side]) >= FEEDBACK_CAPACITY:
                pending[side].popleft()
                dropped[side] += 1
            pending[side].append(sample)


def feedback_head(pending):
    return {side: list(islice(pending[side], FEEDBACK_PER_PACKET)) for side in SIDES}


def consume_feedback(pending, batch):
    for side in SIDES:
        for _ in batch[side]:
            pending[side].popleft()


class Publisher:
    def __init__(self, sink=None):
        self.sink = sink
        self.session = uuid.uuid4().hex
        self.sequence = 0
        self.dropped = 0
        self.error = None
        self.counts = {name: 0 for name in ("A", "B", "X", "Y")}
        self.previous = None
        self.vr_session = None
        self.feedback_batches = False
        self.pending_feedback = {side: deque() for side in SIDES}
        self.feedback_dropped = dict.fromkeys(SIDES, 0)
        self.send_feedback = {side: deque() for side in SIDES}
        self.send_feedback_dropped = dict.fromkeys(SIDES, 0)
        self.queue = queue.Queue(maxsize=8)
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, name="collection-telemetry", daemon=True)
        self.thread.start()

    def submit(self, value, frame, connected):
        try:
            if "gripper_feedback" in value:
                self.feedback_batches = True
                motors = (value.get("grippers") or {}).get("motors") or {}
                batch = {}
                for side in SIDES:
                    motor = motors.get(side, {})
                    endpoints = motor.get("calibration_endpoints_rad") or motor.get("display_endpoints_rad") or {}
                    batch[side] = [dict(sample, calibration_endpoints_rad=dict(
                        sample.get("calibration_endpoints_rad", endpoints)))
                        for sample in value["gripper_feedback"].get(side, ())]
                append_feedback(self.pending_feedback, batch, self.feedback_dropped)
            buttons = frame.buttons if frame is not None else {}
            valid = bool(connected and frame is not None and frame.focused and frame.tracked
                         and 0 <= time.monotonic() - frame.received <= .2)
            vr_session = frame.session if frame else None
            if not valid or vr_session != self.vr_session:
                self.previous = None
            self.vr_session = vr_session
            if valid and self.previous is not None:
                for name in self.counts:
                    if buttons.get(name) is True and self.previous.get(name) is False:
                        self.counts[name] += 1
            self.previous = dict(buttons) if valid else None
            self.sequence += 1
            row = {
                "schema": "fusion_collection_telemetry_v1", "session": self.session,
                "sequence": self.sequence, "publisher_dropped": self.dropped,
                "pc_monotonic_ns": time.monotonic_ns(), "pc_unix_ns": time.time_ns(),
                "mode": value["mode"], "teleop_state": value["state"],
                "vr_connected": valid, "vr_session": vr_session,
                "buttons": dict(buttons), "button_press_counts": self.counts.copy(),
                "joints_deg": value["joints_deg"],
                "joint_feedback": value["robot_feedback_details"],
                "installation": value.get("installation"),
                "grippers": value["grippers"],
                "control_timing": value.get("control_timing", {}),
                "joint_command": value.get("joint_command"),
                "b_also_emergency_stop": False,
                "emergency_buttons": [],
                "collection_start_buttons": ["A", "X"],
                "collection_stop_buttons": ["B", "Y"],
            }
            if self.feedback_batches:
                row["gripper_feedback"] = feedback_head(self.pending_feedback)
                row["gripper_feedback_dropped"] = self.feedback_dropped.copy()
            self.queue.put_nowait(row)
            if self.feedback_batches:
                consume_feedback(self.pending_feedback, row["gripper_feedback"])
        except queue.Full:
            self.dropped += 1
        except Exception as exc:
            self.error = str(exc)  # Recording must never interrupt control.

    def _run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            while not self.done.is_set():
                try:
                    row = self.queue.get(timeout=.2)
                except queue.Empty:
                    continue
                try:
                    self._send(sock, row)
                except Exception as exc:
                    self.error = str(exc)
                    self.dropped += 1

    def _send(self, sock, row):
        """A failed send keeps its real feedback for the next fresh status packet."""
        row = dict(row)
        if "gripper_feedback" in row:
            append_feedback(self.send_feedback, row["gripper_feedback"], self.send_feedback_dropped)
            row["gripper_feedback"] = feedback_head(self.send_feedback)
            row["gripper_feedback_send_dropped"] = self.send_feedback_dropped.copy()
        data = json.dumps(row, allow_nan=False, separators=(",", ":")).encode()
        if len(data) > 60000:
            raise ValueError("telemetry packet too large")
        sent = sock.sendto(data, ("127.0.0.1", PORT)) if self.sink is None else (len(data) if self.sink(row) else 0)
        if sent != len(data):
            raise OSError("incomplete telemetry datagram")
        if "gripper_feedback" in row:
            consume_feedback(self.send_feedback, row["gripper_feedback"])

    def close(self):
        self.done.set()
        self.thread.join(timeout=.3)
