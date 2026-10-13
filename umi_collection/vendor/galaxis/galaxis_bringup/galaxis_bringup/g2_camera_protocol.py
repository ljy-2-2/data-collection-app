"""Small length-prefixed wire protocol for the local G2 camera bridge."""

from __future__ import annotations

import json
import socket
import struct
from typing import Any


MAX_HEADER_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("G2 camera stream closed")
        chunks.extend(chunk)
    return bytes(chunks)


def send_frame(connection: socket.socket, metadata: dict[str, Any], data: bytes) -> None:
    """Send one JSON metadata block followed by its encoded image bytes."""
    header = dict(metadata)
    header["data_length"] = len(data)
    encoded_header = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(encoded_header) > MAX_HEADER_BYTES:
        raise ValueError("G2 camera frame header is too large")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("G2 camera frame is too large")
    connection.sendall(struct.pack("!I", len(encoded_header)))
    connection.sendall(encoded_header)
    connection.sendall(data)


def receive_frame(connection: socket.socket) -> tuple[dict[str, Any], bytes]:
    """Receive one frame and reject malformed or unbounded payload lengths."""
    header_size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if not 0 < header_size <= MAX_HEADER_BYTES:
        raise ValueError(f"invalid G2 camera header size: {header_size}")
    header = json.loads(_recv_exact(connection, header_size).decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("G2 camera header must be a JSON object")
    data_size = header.get("data_length")
    if not isinstance(data_size, int) or not 0 < data_size <= MAX_IMAGE_BYTES:
        raise ValueError(f"invalid G2 camera frame size: {data_size!r}")
    return header, _recv_exact(connection, data_size)
