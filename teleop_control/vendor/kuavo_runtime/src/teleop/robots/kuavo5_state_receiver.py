"""Receive Kuavo sensorsData snapshots without importing or starting ROS.

The subscription factory and message type are injected. Offline demos use a fake
subscription; a later audited entry point may supply rospy.Subscriber/sensorsData.
Only joint_q is consumed. Source sensor_time orders messages; monotonic time
measures local freshness. No actuator, publisher or control service is created.
"""

import math
from threading import Lock
import time
from types import SimpleNamespace
from typing import Callable, Iterable

from teleop.robots.kuavo5 import Kuavo5Adapter, KuavoStateUnavailable


def _source_timestamp(message) -> int:
    stamp = message.sensor_time
    seconds, nanos = stamp.secs, stamp.nsecs
    if type(seconds) is not int or seconds < 0:
        raise ValueError("sensor_time.secs must be a non-negative integer")
    if type(nanos) is not int or not 0 <= nanos < 1_000_000_000:
        raise ValueError("sensor_time.nsecs must be an integer in [0, 1e9)")
    result = seconds * 1_000_000_000 + nanos
    if result == 0:
        raise ValueError("sensor_time must be nonzero; source clock needs audit")
    return result


class Kuavo5StateReceiver:
    """Own one read-only adapter and one optional latest-only subscription.

    Metadata must describe the exact joint_q order. Do not derive that order by
    sorting URDF joint names. Constructor does not connect to anything.
    """

    def __init__(self, adapter: Kuavo5Adapter, *, joint_names: Iterable[str],
                 robot_version: str, clock_ns: Callable[[], int] = time.monotonic_ns):
        names = tuple(joint_names)
        if not isinstance(adapter, Kuavo5Adapter):
            raise TypeError("receiver requires a Kuavo5Adapter")
        if robot_version != adapter.robot_version:
            raise ValueError("receiver version must match adapter")
        if (len(names) != len(set(names)) or set(names) != set(adapter.get_joint_schema().names)):
            raise ValueError("receiver joint names must match adapter schema exactly")
        self._adapter, self._names, self._version = adapter, names, robot_version
        self._clock = clock_ns
        self._lock = Lock()
        self._subscription = None
        self._starting = False
        self._stopped = False
        self._error = ""
        self._received = self._accepted = self._rejected = 0
        self._source_ns = None
        self._receive_ns = None

    def on_message(self, message) -> bool:
        """A receive callback: copy, validate and replace the latest observation."""
        received_ns = self._clock()
        with self._lock:
            if self._stopped:
                return False
            self._received += 1
            try:
                source_ns = _source_timestamp(message)
                if self._source_ns is not None and source_ns <= self._source_ns:
                    raise ValueError("source timestamp repeated or moved backwards; audit clock/restart receiver")
                values = tuple(message.joint_data.joint_q)
                self._adapter.accept_sdk_joint_state(SimpleNamespace(position=values),
                    joint_names=self._names, robot_version=self._version,
                    received_monotonic_ns=received_ns)
            except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                self._adapter.invalidate_state()
                self._error = str(exc)
                self._rejected += 1
                return False
            self._accepted += 1
            self._source_ns, self._receive_ns = source_ns, received_ns
            self._error = ""
            return True

    def invalidate_input(self, reason: str) -> None:
        """Discard cached angles on an external transport/protocol error."""
        with self._lock:
            if not self._stopped:
                self._adapter.invalidate_state()
                self._error = str(reason)

    def snapshot(self) -> dict:
        """Status for display. Never present stale/invalid angles as current."""
        with self._lock:
            now = self._clock()
            positions = {}
            reason = self._error
            if self._stopped:
                status = "stopped"
            elif self._error:
                status = "invalid"
            elif self._receive_ns is None:
                status = "waiting"
            else:
                try:
                    positions = dict(self._adapter.read_state(now_ns=now).positions)
                    status = "fresh"
                except KuavoStateUnavailable as exc:
                    status, reason = "stale", str(exc)
            return {
                "status": status, "reason": reason,
                "received": self._received, "accepted": self._accepted, "rejected": self._rejected,
                "received_monotonic_ns": self._receive_ns, "source_timestamp_ns": self._source_ns,
                "age_ms": None if self._receive_ns is None else (now - self._receive_ns) / 1e6,
                "positions_rad": positions,
                "positions_deg": {name: math.degrees(value) for name, value in positions.items()},
                "physical_robot_command_sent": False,
            }

    def start(self, subscribe: Callable, *, message_type, topic: str):
        """Bind an explicit subscriber factory; caller owns ROS startup/audit.

        Never called automatically. Factory must return an unregister() handle.
        Registration occurs outside the callback lock because a latched message
        may invoke the callback synchronously.
        """
        if topic not in ("/sensors_data_raw", "/sensors_data_raw_shm"):
            raise ValueError("only audited Kuavo sensor topics are supported")
        with self._lock:
            if self._stopped or self._starting or self._subscription is not None:
                raise RuntimeError("receiver already started, starting or stopped")
            self._starting = True
        try:
            handle = subscribe(topic, message_type, self.on_message,
                               queue_size=1, tcp_nodelay=True)
        except Exception as exc:
            with self._lock:
                self._starting = False
                self._error = f"subscription failed: {exc}"
                self._adapter.invalidate_state()
            raise
        with self._lock:
            self._starting = False
            stopped = self._stopped
            if not stopped:
                self._subscription = handle
        if stopped:
            handle.unregister()
            raise RuntimeError("receiver stopped during subscription registration")
        return handle

    def stop(self) -> None:
        """Unsubscribe and close local state; NOT a physical emergency stop."""
        with self._lock:
            self._stopped = True
            handle, self._subscription = self._subscription, None
            self._adapter.stop()
        if handle is not None:
            handle.unregister()
