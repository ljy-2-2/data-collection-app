"""Host-side serial protocol shared by Galaxis encoder readers."""

from __future__ import annotations

import math
from typing import Optional, Tuple, Union

import serial


ENCODER_BAUD = 115200
SERIAL_TIMEOUT_SEC = 0.2


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
