"""Host-side serial protocol shared by Galaxis encoder readers."""

from __future__ import annotations

import math
from typing import Optional, Tuple, Union

import serial


ENCODER_BAUD = 115200
SERIAL_TIMEOUT_SEC = 0.2


class EncoderLineReader:
    """Drain available bytes per syscall and retain incomplete serial lines.

    Serial.readline reads one byte at a time; its timeout can also return a
    truncated number that float() would mistakenly accept as a new angle.
    No values are synthesized and no complete lines are discarded.
    """

    def __init__(self, connection, max_line_bytes=4096):
        self.connection = connection
        self.pending = bytearray()
        self.max_line_bytes = max_line_bytes
        self.discarding = False

    def readline(self):
        while True:
            end = self.pending.find(b'\n')
            if end >= 0:
                line = bytes(self.pending[:end + 1])
                del self.pending[:end + 1]
                if self.discarding or len(line) > self.max_line_bytes:
                    self.discarding = False
                    continue
                return line
            if len(self.pending) > self.max_line_bytes:
                self.pending.clear()
                self.discarding = True
            chunk = self.connection.read(min(4096, max(1, self.connection.in_waiting)))
            if not chunk:
                return b''  # Keep the prefix until its newline arrives.
            self.pending.extend(chunk)


def read_encoder_line(connection):
    reader = getattr(connection, '_galaxis_line_reader', None)
    if reader is None:
        reader = EncoderLineReader(connection)
        connection._galaxis_line_reader = reader
    return reader.readline()


def parse_encoder_line(
    raw_line: Union[bytes, str], logger=None
) -> Tuple[Optional[str], Optional[float]]:
    """Parse the firmware's backward-compatible ``DEVICE_ID,radians`` line.

    Boot ROM text, firmware comments beginning with ``#``, malformed values,
    NaN and infinity are ignored. Unlabelled numeric lines remain accepted for
    compatibility with firmware predating device IDs.
    """
    if isinstance(raw_line, bytes):
        line = raw_line.decode("utf-8", errors="ignore").strip()
    else:
        line = str(raw_line).strip()

    if not line or line.startswith("#"):
        return None, None

    device_id: Optional[str] = None
    value_text = line
    if "," in line:
        device_text, value_text = line.split(",", 1)
        device_text = device_text.strip()
        device_id = device_text or None

    try:
        radians = float(value_text.strip())
    except ValueError:
        if logger is not None:
            logger.debug(f"Malformed encoder line: {line!r}")
        return None, None

    if not math.isfinite(radians):
        if logger is not None:
            logger.debug(f"Non-finite encoder value: {line!r}")
        return None, None

    return device_id, radians


def open_encoder_serial(
    port: str,
    baud: int = ENCODER_BAUD,
    timeout: float = SERIAL_TIMEOUT_SEC,
) -> serial.Serial:
    """Open one encoder port without pulsing the board's reset/boot lines."""
    connection = serial.Serial(port=None, baudrate=baud, timeout=timeout)
    connection.exclusive = True
    connection.dtr = False
    connection.rts = False
    connection.port = port
    try:
        connection.open()
        connection.reset_input_buffer()
    except Exception:
        connection.close()
        raise
    return connection
