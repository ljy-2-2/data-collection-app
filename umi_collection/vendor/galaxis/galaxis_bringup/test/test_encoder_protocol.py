from __future__ import annotations

import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from galaxis_bringup import encoder_protocol


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b"L003,1.57079632\r\n", ("L003", 1.57079632)),
        (b"R017,6.28300000\n", ("R017", 6.283)),
        (b"0.25000000\n", (None, 0.25)),
        (b"# MT6835 streaming: rad, device_id=L003\n", (None, None)),
        (b"rst:0x1 (POWERON),boot:0xc (SPI_FAST_FLASH_BOOT)\n", (None, None)),
        (b"ESP-ROM:esp32c6-20220919\n", (None, None)),
        (b"L003,nan\n", (None, None)),
        (b"L003,inf\n", (None, None)),
        (b"broken\n", (None, None)),
    ],
)
def test_parse_encoder_line(line, expected):
    assert encoder_protocol.parse_encoder_line(line) == expected


def test_open_encoder_serial_disables_control_lines_before_open(monkeypatch):
    events = []

    class FakeSerial:
        def __init__(self, *, port, baudrate, timeout):
            assert port is None
            self.port = port
            self.baudrate = baudrate
            self.timeout = timeout
            self._dtr = None
            self._rts = None

        @property
        def dtr(self):
            return self._dtr

        @dtr.setter
        def dtr(self, value):
            self._dtr = value
            events.append(("dtr", value))

        @property
        def rts(self):
            return self._rts

        @rts.setter
        def rts(self, value):
            self._rts = value
            events.append(("rts", value))

        def open(self):
            events.append(("open", self.port, self._dtr, self._rts))

        def reset_input_buffer(self):
            events.append(("reset_input_buffer",))

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(encoder_protocol.serial, "Serial", FakeSerial)
    connection = encoder_protocol.open_encoder_serial("/dev/galaxis_left_esp32c6")

    assert connection.port == "/dev/galaxis_left_esp32c6"
    assert events == [
        ("dtr", False),
        ("rts", False),
        ("open", "/dev/galaxis_left_esp32c6", False, False),
        ("reset_input_buffer",),
    ]


def test_encoder_port_exclusive_across_preview_and_capture():
    import os
    import pty
    import serial
    master, slave = pty.openpty()
    try:
        name = os.ttyname(slave)
        with encoder_protocol.open_encoder_serial(name):
            with pytest.raises(serial.SerialException, match='exclusiv'):
                encoder_protocol.open_encoder_serial(name)
        # Closing the old owner releases the port for the next page/collector.
        with encoder_protocol.open_encoder_serial(name) as connection:
            assert connection.is_open
    finally:
        os.close(master)
        os.close(slave)
