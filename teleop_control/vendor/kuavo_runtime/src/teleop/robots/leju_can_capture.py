"""Offline decoder for the LEJU CANable serial format audited on 2026-10-12.

Pure functions only: no serial port, SDK, ROS, socket or device discovery.
Input must come from an independently audited capture path. A parsed frame is
not evidence of physical reception, device identity, or permission to control.
Unlike the vendor Recv_frame conversion, retain FD/BRS/extended/RTR flags and
reject truncated payloads instead of silently completing them with zero bytes.
Optional serial timestamp suffixes are deliberately unsupported until verified.
"""

from dataclasses import dataclass


FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
HEX = frozenset(b"0123456789abcdefABCDEF")


@dataclass(frozen=True)
class CanFrame:
    can_id: int
    dlc: int
    data: bytes
    is_extended: bool
    is_fd: bool
    bit_rate_switch: bool
    is_remote: bool


def parse_slcan(line):
    """Parse one CR-terminated vendor-format record; commands/ACKs are errors."""
    if not isinstance(line, bytes) or not line.endswith(b"\r"):
        raise ValueError("one complete bytes record ending in CR required")
    raw = line[:-1]
    if not raw or raw[0] not in b"tTdDbBrR":
        raise ValueError("not a supported CAN data/remote record")
    kind = chr(raw[0])
    extended = kind.isupper()
    fd = kind.lower() in ("d", "b")
    remote = kind.lower() == "r"
    width = 8 if extended else 3
    if len(raw) < width + 2 or any(c not in HEX for c in raw[1:]):
        raise ValueError("incomplete header or non-hex record")
    can_id = int(raw[1:1 + width], 16)
    if can_id > (0x1FFFFFFF if extended else 0x7FF):
        raise ValueError("CAN ID exceeds frame format")
    dlc = int(raw[1 + width:2 + width], 16)
    if not fd and dlc > 8:
        raise ValueError("classic CAN DLC exceeds 8")
    length = 0 if remote else FD_LENGTHS[dlc]
    if len(raw) != width + 2 + length * 2:
        raise ValueError("payload length does not match DLC; suffixes unsupported")
    return CanFrame(can_id, dlc, bytes.fromhex(raw[2 + width:].decode("ascii")),
                    extended, fd, kind.lower() == "b", remote)


class SlcanStream:
    """Bounded serial reassembly for OFFLINE bytes; resync at CR after errors."""

    def __init__(self):
        self._pending = bytearray()
        self._discard = False
        self.invalid_records = 0
        self.records = 0

    def feed(self, chunk):
        if not isinstance(chunk, bytes):
            raise ValueError("bytes required")
        frames = []
        for value in chunk:
            if value == 13:
                if not self._discard and self._pending:
                    try:
                        frames.append(parse_slcan(bytes(self._pending) + b"\r"))
                        self.records += 1
                    except ValueError:
                        self.invalid_records += 1
                self._pending.clear()
                self._discard = False
            elif not self._discard:
                self._pending.append(value)
                # Largest supported record: prefix + ID8 + DLC1 + 64 bytes hex.
                if len(self._pending) > 138:
                    self.invalid_records += 1
                    self._pending.clear()
                    self._discard = True
        return frames

    def finish(self):
        """An unterminated tail is incomplete, never a completed CAN sample."""
        if self._pending:
            self.invalid_records += 1
        self._pending.clear()
        self._discard = False


@dataclass(frozen=True)
class MotorRoute:
    bus: str
    master_id: int
    motor_id: int

    def __post_init__(self):
        if not isinstance(self.bus, str) or not self.bus:
            raise ValueError("explicit bus required")
        if type(self.master_id) is not int or not 0 <= self.master_id <= 0x7FF:
            raise ValueError("standard feedback CAN ID required")
        if type(self.motor_id) is not int or not 1 <= self.motor_id <= 15:
            raise ValueError("verified low-nibble decoder requires motor ID 1..15")


def decode_damiao_raw(frame, *, bus, direction, route):
    """Decode codes without calibration or guessed units; require RX provenance.

Routes must be configured from verified wiring/identity, never inferred from
an arbitrary matching payload. 'rx' is supplied evidence, not proof on its own.
FD and extended frames are retained by the parser but not accepted by this
classic-frame motor decoder. Register-like packets are conservatively rejected.
"""
    if direction != "rx":
        raise ValueError("TX echoes/unknown direction are not motor feedback")
    if bus != route.bus or frame.can_id != route.master_id:
        raise ValueError("wrong bus or feedback CAN ID")
    if frame.is_fd or frame.is_extended or frame.is_remote or len(frame.data) != 8:
        raise ValueError("only verified classic standard 8-byte feedback supported")
    data = frame.data
    if data[:2] == bytes((route.motor_id, 0)) and data[2] in (0x33, 0x55, 0xAA):
        raise ValueError("register-like reply is not fresh motor status")
    if data[0] & 15 != route.motor_id:
        raise ValueError("feedback motor ID mismatch")
    return dict(motor_id=data[0] & 15, state=data[0] >> 4,
                position_code=int.from_bytes(data[1:3], "big"),
                velocity_code=(data[3] << 4) | (data[4] >> 4),
                torque_code=((data[4] & 15) << 8) | data[5],
                mos_temperature_c=data[6], rotor_temperature_c=data[7])
