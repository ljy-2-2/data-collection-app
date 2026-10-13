"""Robot-independent encoded video frame contracts."""

from __future__ import annotations

from dataclasses import dataclass
import math


MAX_CAMERA_NAME_BYTES = 64
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_FRAME_DIMENSION = 8192


@dataclass(frozen=True)
class VideoStreamCapability:
    """One encoded camera stream a robot video source can provide."""

    camera_name: str
    width: int
    height: int
    encoding: str
    nominal_fps: float

    def __post_init__(self) -> None:
        _validate_camera_name(self.camera_name)
        _integer_in_range(
            self.width, "width", minimum=1, maximum=MAX_FRAME_DIMENSION
        )
        _integer_in_range(
            self.height, "height", minimum=1, maximum=MAX_FRAME_DIMENSION
        )
        if self.encoding != "jpeg":
            raise ValueError("encoding must be 'jpeg' in protocol version 1")
        if (
            isinstance(self.nominal_fps, bool)
            or not isinstance(self.nominal_fps, (int, float))
            or not math.isfinite(self.nominal_fps)
            or self.nominal_fps <= 0.0
        ):
            raise ValueError("nominal_fps must be a positive finite number")


@dataclass(frozen=True)
class VideoFrame:
    """One complete encoded camera image plus timing metadata.

    Version 1 deliberately supports JPEG only because the verified G2 camera
    already provides complete JPEG payloads. Keeping the original bytes avoids
    an unnecessary decode/re-encode step on the PC.
    """

    camera_name: str
    sequence_id: int
    source_timestamp_ns: int
    pc_received_monotonic_ns: int
    width: int
    height: int
    encoding: str
    payload: bytes

    def __post_init__(self) -> None:
        _validate_camera_name(self.camera_name)

        _integer_in_range(self.sequence_id, "sequence_id", minimum=0)
        _integer_in_range(self.source_timestamp_ns, "source_timestamp_ns", minimum=1)
        _integer_in_range(
            self.pc_received_monotonic_ns,
            "pc_received_monotonic_ns",
            minimum=1,
        )
        _integer_in_range(
            self.width, "width", minimum=1, maximum=MAX_FRAME_DIMENSION
        )
        _integer_in_range(
            self.height, "height", minimum=1, maximum=MAX_FRAME_DIMENSION
        )

        if self.encoding != "jpeg":
            raise ValueError("encoding must be 'jpeg' in protocol version 1")
        if not isinstance(self.payload, bytes):
            raise ValueError("payload must be immutable bytes")
        if len(self.payload) > MAX_FRAME_BYTES:
            raise ValueError(f"payload exceeds {MAX_FRAME_BYTES} bytes")
        if not self.payload.startswith(b"\xff\xd8") or not self.payload.endswith(
            b"\xff\xd9"
        ):
            raise ValueError("payload must be one complete JPEG")


def _validate_camera_name(camera_name: object) -> str:
    if not isinstance(camera_name, str) or not camera_name:
        raise ValueError("camera_name must be a non-empty string")
    if "\x00" in camera_name:
        raise ValueError("camera_name must not contain NUL")
    try:
        camera_name_bytes = camera_name.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("camera_name must be valid UTF-8") from error
    if len(camera_name_bytes) > MAX_CAMERA_NAME_BYTES:
        raise ValueError(
            f"camera_name must be at most {MAX_CAMERA_NAME_BYTES} UTF-8 bytes"
        )
    return camera_name


def _integer_in_range(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ValueError(f"{name} must be at least {minimum}")
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value
