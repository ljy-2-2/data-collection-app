"""Versioned, length-aware wire protocol for encoded video frames."""

from __future__ import annotations

import socket
import struct
from typing import Protocol

from .contracts import MAX_CAMERA_NAME_BYTES, MAX_FRAME_BYTES, VideoFrame


MAGIC = b"GVID"
VERSION = 1
JPEG_ENCODING = 1

# magic, version, encoding, camera-name length, sequence, source timestamp,
# PC receive timestamp, width, height, JPEG length. Network byte order keeps
# the Python producer and future C# receiver unambiguous.
HEADER = struct.Struct("!4sBBHQQQIII")


class ProtocolError(ValueError):
    """Raised when a peer supplies a malformed or incomplete video frame."""


class SocketReader(Protocol):
    def recv(self, size: int) -> bytes: ...


class SocketWriter(Protocol):
    def sendall(self, data: bytes) -> None: ...


def encode_frame(frame: VideoFrame) -> bytes:
    camera_name = frame.camera_name.encode("utf-8")
    header = HEADER.pack(
        MAGIC,
        VERSION,
        JPEG_ENCODING,
        len(camera_name),
        frame.sequence_id,
        frame.source_timestamp_ns,
        frame.pc_received_monotonic_ns,
        frame.width,
        frame.height,
        len(frame.payload),
    )
    return header + camera_name + frame.payload


def decode_frame(data: bytes) -> VideoFrame:
    if not isinstance(data, bytes):
        raise ProtocolError("encoded frame must be bytes")
    if len(data) < HEADER.size:
        raise ProtocolError("video frame is shorter than its fixed header")
    metadata = _unpack_header(data[: HEADER.size])
    expected = HEADER.size + metadata.camera_name_length + metadata.payload_length
    if len(data) != expected:
        raise ProtocolError(
            f"video frame length is {len(data)}, expected exactly {expected}"
        )
    name_start = HEADER.size
    payload_start = name_start + metadata.camera_name_length
    return _build_frame(
        metadata,
        data[name_start:payload_start],
        data[payload_start:],
    )


def read_frame(reader: SocketReader) -> VideoFrame | None:
    header = _recv_exact(reader, HEADER.size, allow_clean_eof=True)
    if header is None:
        return None
    metadata = _unpack_header(header)
    camera_name = _recv_exact(reader, metadata.camera_name_length)
    payload = _recv_exact(reader, metadata.payload_length)
    assert camera_name is not None and payload is not None
    return _build_frame(metadata, camera_name, payload)


def send_frame(writer: SocketWriter, frame: VideoFrame) -> None:
    writer.sendall(encode_frame(frame))


class _HeaderMetadata:
    def __init__(
        self,
        *,
        camera_name_length: int,
        sequence_id: int,
        source_timestamp_ns: int,
        pc_received_monotonic_ns: int,
        width: int,
        height: int,
        payload_length: int,
    ) -> None:
        self.camera_name_length = camera_name_length
        self.sequence_id = sequence_id
        self.source_timestamp_ns = source_timestamp_ns
        self.pc_received_monotonic_ns = pc_received_monotonic_ns
        self.width = width
        self.height = height
        self.payload_length = payload_length


def _unpack_header(header: bytes) -> _HeaderMetadata:
    try:
        (
            magic,
            version,
            encoding,
            camera_name_length,
            sequence_id,
            source_timestamp_ns,
            pc_received_monotonic_ns,
            width,
            height,
            payload_length,
        ) = HEADER.unpack(header)
    except struct.error as error:
        raise ProtocolError("invalid video frame header") from error

    if magic != MAGIC:
        raise ProtocolError("invalid video frame magic")
    if version != VERSION:
        raise ProtocolError(f"unsupported video protocol version: {version}")
    if encoding != JPEG_ENCODING:
        raise ProtocolError(f"unsupported video encoding code: {encoding}")
    if camera_name_length < 1 or camera_name_length > MAX_CAMERA_NAME_BYTES:
        raise ProtocolError("camera-name length is outside the protocol limit")
    if payload_length < 4 or payload_length > MAX_FRAME_BYTES:
        raise ProtocolError("JPEG payload length is outside the protocol limit")

    return _HeaderMetadata(
        camera_name_length=camera_name_length,
        sequence_id=sequence_id,
        source_timestamp_ns=source_timestamp_ns,
        pc_received_monotonic_ns=pc_received_monotonic_ns,
        width=width,
        height=height,
        payload_length=payload_length,
    )


def _build_frame(
    metadata: _HeaderMetadata, camera_name_bytes: bytes, payload: bytes
) -> VideoFrame:
    try:
        camera_name = camera_name_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("camera name is not valid UTF-8") from error
    try:
        return VideoFrame(
            camera_name=camera_name,
            sequence_id=metadata.sequence_id,
            source_timestamp_ns=metadata.source_timestamp_ns,
            pc_received_monotonic_ns=metadata.pc_received_monotonic_ns,
            width=metadata.width,
            height=metadata.height,
            encoding="jpeg",
            payload=payload,
        )
    except ValueError as error:
        raise ProtocolError(f"invalid video frame: {error}") from error


def _recv_exact(
    reader: SocketReader, size: int, *, allow_clean_eof: bool = False
) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        try:
            chunk = reader.recv(size - len(chunks))
        except socket.timeout:
            raise
        except OSError as error:
            raise ProtocolError(f"video socket read failed: {error}") from error
        if not chunk:
            if allow_clean_eof and not chunks:
                return None
            raise ProtocolError(
                f"video stream ended after {len(chunks)} of {size} required bytes"
            )
        chunks.extend(chunk)
    return bytes(chunks)
