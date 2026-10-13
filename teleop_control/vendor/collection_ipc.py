"""Small newline-JSON IPC used between teleop and the collector process."""

from __future__ import annotations

import json
import socket
import threading
import time


class CollectionClient:
    def __init__(self, socket_path: str, timeout_s: float = 5.0):
        self.path = socket_path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self._sock.connect(socket_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    self._sock.close()
                    raise RuntimeError(f"collector IPC not ready: {socket_path}")
                time.sleep(0.1)
        self._lock = threading.Lock()
        self._closed = False

    def send(self, kind: str, **payload) -> bool:
        if self._closed:
            return False
        row = {"type": kind, **payload}
        data = (json.dumps(row, separators=(",", ":")) + "\n").encode()
        try:
            with self._lock:
                self._sock.sendall(data)
            return True
        except OSError:
            self._closed = True
            return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass
