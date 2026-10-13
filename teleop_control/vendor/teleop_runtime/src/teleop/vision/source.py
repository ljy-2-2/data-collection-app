"""Robot-independent video-source interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .contracts import VideoFrame, VideoStreamCapability


@runtime_checkable
class RobotVideoSource(Protocol):
    """The small contract every robot camera adapter must implement.

    A G2, Leju, or offline source may use a different vendor SDK internally, but
    the PC video core only sees these capabilities and complete latest frames.
    """

    @property
    def capabilities(self) -> tuple[VideoStreamCapability, ...]: ...

    def read_latest_frame(self, *, timeout_s: float) -> VideoFrame | None: ...

    def close(self) -> None: ...
