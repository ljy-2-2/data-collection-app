"""Read GVID frames from a bounded subprocess pipe, not a control channel."""

from dataclasses import replace
import time
from typing import BinaryIO

from .contracts import VideoFrame
from .protocol import read_frame


class _PipeReader:
    def __init__(self, stream: BinaryIO):
        self.stream = stream

    def recv(self, size: int) -> bytes:
        return self.stream.read(size)


def read_pipe_frame(stream: BinaryIO, *, expected_camera: str,
                    received_ns: int | None = None) -> VideoFrame | None:
    frame = read_frame(_PipeReader(stream))
    if frame is None:
        return None
    if frame.camera_name != expected_camera:
        raise ValueError(f"unexpected camera identity: {frame.camera_name}")
    return replace(frame, pc_received_monotonic_ns=(
        time.monotonic_ns() if received_ns is None else received_ns))
