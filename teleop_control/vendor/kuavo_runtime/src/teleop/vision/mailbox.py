"""A bounded one-frame mailbox for low-latency video delivery."""

from __future__ import annotations

import threading
import time

from .contracts import VideoFrame


class LatestFrameMailbox:
    """Keep one newest frame and overwrite older unpublished work."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest: VideoFrame | None = None
        self._published_count = 0
        self._overwritten_count = 0
        self._last_delivered_sequence = -1
        self._closed = False

    @property
    def published_count(self) -> int:
        with self._condition:
            return self._published_count

    @property
    def overwritten_count(self) -> int:
        with self._condition:
            return self._overwritten_count

    @property
    def latest_frame(self) -> VideoFrame | None:
        """Return the newest frame without marking it as delivered."""

        with self._condition:
            return self._latest

    def publish(self, frame: VideoFrame) -> None:
        if not isinstance(frame, VideoFrame):
            raise TypeError("frame must be VideoFrame")
        with self._condition:
            if self._closed:
                raise RuntimeError("latest-frame mailbox is closed")
            if self._latest is not None:
                if frame.sequence_id <= self._latest.sequence_id:
                    raise ValueError("video sequence_id must increase monotonically")
                if self._latest.sequence_id > self._last_delivered_sequence:
                    self._overwritten_count += 1
            self._latest = frame
            self._published_count += 1
            self._condition.notify_all()

    def wait_for_newer(
        self, after_sequence_id: int, *, timeout: float | None = None
    ) -> VideoFrame | None:
        if isinstance(after_sequence_id, bool) or not isinstance(after_sequence_id, int):
            raise ValueError("after_sequence_id must be an integer")
        if timeout is not None and timeout < 0.0:
            raise ValueError("timeout must be non-negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._closed:
                if (
                    self._latest is not None
                    and self._latest.sequence_id > after_sequence_id
                ):
                    self._last_delivered_sequence = max(
                        self._last_delivered_sequence, self._latest.sequence_id
                    )
                    return self._latest
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return None
                self._condition.wait(remaining)
            return None

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
