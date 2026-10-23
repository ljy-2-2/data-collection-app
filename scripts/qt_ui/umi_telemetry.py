"""Receive live UMI samples without invalidating the entire application state."""
from __future__ import annotations

import json
import math

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkRequest


class UmiTelemetry(QObject):
    changed = Signal()

    def __init__(self, network, parent=None):
        super().__init__(parent)
        self.network = network
        self.url = ""
        self.reply = None
        self.buffer = b""
        self.value = dict(monitor_connected=False, vr={}, encoders={})
        self.history = dict(left=[], right=[])
        self.retry = QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.setInterval(1000)
        self.retry.timeout.connect(self._connect)
        self.stale = QTimer(self)
        self.stale.setSingleShot(True)
        self.stale.setInterval(2000)
        self.stale.timeout.connect(self._expire)

    def start(self, url):
        if self.url == url:
            return
        self.stop()
        self.url = url
        self._connect()

    def stop(self):
        if not self.url and self.reply is None:
            return
        self.url = ""
        self.retry.stop()
        self.stale.stop()
        reply, self.reply = self.reply, None
        if reply is not None:
            reply.abort()
        self.buffer = b""
        self.value = dict(monitor_connected=False, vr={}, encoders={})
        self.history = dict(left=[], right=[])
        self.changed.emit()

    def _connect(self):
        if not self.url or self.reply is not None:
            return
        request = QNetworkRequest(QUrl(self.url))
        request.setRawHeader(b"Accept", b"text/event-stream")
        request.setTransferTimeout(2000)
        reply = self.network.get(request)
        self.reply = reply
        self.buffer = b""
        self.stale.start()
        reply.readyRead.connect(lambda: self._read(reply))
        reply.finished.connect(lambda: self._finished(reply))

    def _expire(self):
        self._offline()
        if self.reply is not None:
            self.reply.abort()

    def _offline(self):
        self.value = dict(monitor_connected=False, vr={}, encoders={})
        self.changed.emit()

    def _finished(self, reply):
        if reply is self.reply:
            self.reply = None
            self.stale.stop()
            self._offline()
            if self.url:
                self.retry.start()
        reply.deleteLater()

    def _read(self, reply):
        if reply is not self.reply:
            return
        self.buffer += bytes(reply.readAll())
        if len(self.buffer) > 256 * 1024:
            self._expire()
            return
        latest = None
        while b"\n\n" in self.buffer:
            event, self.buffer = self.buffer.split(b"\n\n", 1)
            if not event.startswith(b"data:"):
                continue
            try:
                value = json.loads(event[5:])
                if (isinstance(value, dict)
                        and isinstance(value.get("vr"), dict)
                        and isinstance(value.get("encoders"), dict)):
                    latest = value
            except (ValueError, UnicodeDecodeError):
                continue
        if latest is None:
            return
        # If rendering stalls, paint the newest sample instead of replaying
        # obsolete frames. The recorder is a separate ROS subscriber.
        self.value = latest
        self.stale.start()
        histories = {}
        for side in ("left", "right"):
            reading = latest["encoders"].get(side, {})
            value = reading.get("opening_angle")
            if value is None and isinstance(reading.get("position"), (int, float)):
                value = 2.0 * reading["position"]
            valid = (latest.get("monitor_connected") and reading.get("online")
                     and isinstance(value, (int, float)) and math.isfinite(value))
            histories[side] = [*self.history[side], value if valid else None][-240:]
        self.history = histories
        self.changed.emit()
