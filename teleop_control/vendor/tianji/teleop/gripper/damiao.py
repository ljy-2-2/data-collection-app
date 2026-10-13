"""DM-J4310-2EC common protocol: MIT/PVT control, radians, 50 us timeout ticks."""

from dataclasses import asdict, dataclass
from .config import bounded_position, travel_bounds
from .compatibility import current_limit
from .sampling import FeedbackBuffer
import math
import socket
import struct
import time


class TransportBusy(OSError):
    """Endpoint transmit slot is busy; retry latest target on a later cycle."""


ENABLE = b"\xff" * 7 + b"\xfc"
DISABLE = b"\xff" * 7 + b"\xfd"
CLEAR_ERROR = b"\xff" * 7 + b"\xfb"


def command(position, speed, current):
    if not all(math.isfinite(x) for x in (position, speed, current)):
        raise ValueError("nonfinite motor command")
    if not 0.01 <= speed <= 100 or not 0.0001 <= current <= 1:
        raise ValueError("invalid speed/current limit")
    return struct.pack("<fHH", position, int(speed * 100), int(current * 10000))


def mit_command(position, limits, kp=20., kd=1.):
    """Same MIT bit layout as ESP32 host mit_position; Kp 0..500, Kd 0..5."""
    pmax, vmax, tmax = limits
    if not all(math.isfinite(v) for v in (position, *limits, kp, kd)):
        raise ValueError("nonfinite MIT command")
    if min(limits) <= 0 or abs(position) > pmax or not 0 <= kp <= 500 or not 0 <= kd <= 5:
        raise ValueError("invalid MIT position/limits/gains")
    def encode(value, low, high, bits):
        return int((value-low)*((1 << bits)-1)/(high-low))
    p = encode(position, -pmax, pmax, 16)
    v = encode(0., -vmax, vmax, 12)
    k = encode(kp, 0., 500., 12)
    d = encode(kd, 0., 5., 12)
    t = encode(0., -tmax, tmax, 12)
    return bytes((p >> 8, p & 255, v >> 4, ((v & 15) << 4) | (k >> 8),
                  k & 255, d >> 4, ((d & 15) << 4) | (t >> 8), t & 255))


@dataclass
class Feedback:
    position: float
    velocity: float
    torque: float
    mos_c: int
    rotor_c: int
    state: int
    received: float
    sequence: int = 0

    def status(self):
        return asdict(self)


def decode(data, motor_id, limits, now):
    if len(data) != 8 or data[0] & 15 != motor_id:
        return None
    p, v, t = limits
    return Feedback(
        int.from_bytes(data[1:3], "big") * (2 * p / 65535) - p,
        ((data[3] << 4) | (data[4] >> 4)) * (2 * v / 4095) - v,
        (((data[4] & 15) << 8) | data[5]) * (2 * t / 4095) - t,
        data[6],
        data[7],
        data[0] >> 4,
        now,
    )


class SocketCAN:
    """A dedicated classic SocketCAN interface; never configures the network."""

    def __init__(self, channel):
        self.sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            self.sock.bind((channel,))
            self.sock.setblocking(False)
        except BaseException:
            self.sock.close()
            raise

    def send(self, can_id, data):
        if not 0 <= can_id <= 0x7FF or len(data) > 8:
            raise ValueError("standard CAN frame required")
        self.sock.send(
            struct.pack("=IB3x8s", can_id, len(data), data.ljust(8, b"\x00"))
        )

    def recv(self):
        try:
            packet = self.sock.recv(16)
        except BlockingIOError:
            return None
        can_id, length, data = struct.unpack("=IB3x8s", packet)
        if can_id & ~0x7FF or length > 8:  # Ignore extended/RTR/error frames.
            return (-1, b"")
        return can_id, data[:length]

    def close(self):
        self.sock.close()


def mode_offset(motor):
    """DM modes 1/2/3/4 shift the command CAN ID by 0x000/0x100/0x200/0x300."""
    return (int(motor.get("control_mode", 4)) - 1) * 0x100


class Driver:
    def __init__(self, bus, motors):
        self.bus, self.motors = bus, motors
        self.feedback, self.limits = {}, {}
        self.original_timeouts = {}
        self.original_modes = {}
        self.current_limits = {}
        self.current_limit_reports = {}
        self.last_position_commands = {}
        self.received_feedback = FeedbackBuffer(motors)

    def drain_feedback(self):
        return self.received_feedback.drain()

    def set_send_deadline(self, side, deadline):
        self.bus.send_deadline = deadline

    @property
    def feedback_timing(self):
        return {side: self.received_feedback.timing(side) for side in self.motors}

    def register(self, side, rid, fmt="I", *, attempts=1):
        m = self.motors[side]
        header = struct.pack("<HBB", m["motor_id"], 0x33, rid)
        unmatched = []
        for _ in range(attempts):
            if getattr(self.bus, "read_separator_required", False) is True:
                self.bus.prepare_register_read(m["motor_id"], rid)
            self.bus.send(0x7FF, header)
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                packet = self.bus.recv()
                if packet is None:
                    time.sleep(0.001)
                    continue
                cid, data = packet
                if cid == m["master_id"] and len(data) == 8 and data[:4] == header:
                    return struct.unpack("<" + fmt, data[4:])[0]
                if len(unmatched) < 8:
                    unmatched.append(f"CAN ID={cid:#x}, data={data.hex(' ')}")
                if len(data) == 8 and data[:4] == header and cid >= 0:
                    raise RuntimeError(
                        f"{side}: register 0x{rid:02x} replied on Master ID={cid} ({cid:#x}), "
                        f"but config master_id={m['master_id']}; confirm/update feedback ID"
                    )
        raise RuntimeError(
            f"{side}: register 0x{rid:02x} no matching reply after {attempts} read request(s); "
            f"expected motor_id={m['motor_id']}, master_id={m['master_id']}; "
            + (
                "unmatched: " + "; ".join(unmatched)
                if unmatched
                else "no decoded CAN reply received"
            )
        )

    def read_current_limit(self, side, *, attempts=3):
        """Read-only capability discovery shared by startup and diagnostics."""
        imax = self.register(side, 0x3B, "f", attempts=attempts)
        identity = None
        if imax == 0:
            identity = tuple(self.register(side, rid, attempts=attempts)
                             for rid in (0x0D, 0x0E, 0x24))
        try:
            return current_limit(self.motors[side], imax, identity)
        except ValueError as error:
            raise ValueError(f"{side}: {error}") from error

    def preflight(self):
        self.current_limits.clear()
        self.current_limit_reports.clear()
        # 插上即用：清除陈旧故障，控制模式/硬件看门狗与配置不符时易失写入（close 恢复）。
        # 不写零点/Flash；PMAX/VMAX/TMAX 与位置保持只读。
        modes, watchdogs = {}, {}
        for side, m in self.motors.items():
            mode = self.register(side, 0x0A)
            if mode not in (1, 2, 3, 4):
                raise ValueError(f"{side}: invalid CTRL_MODE {mode}")
            desired = int(m.get("control_mode", 4))
            if desired not in (1, 4):
                raise ValueError(f"{side}: control_mode must be 1 (MIT) or 4 (force-position)")
            desired_ticks = int(m.get("watchdog_ticks", 0))
            if desired_ticks and not 1000 <= desired_ticks <= 6000:
                raise ValueError(f"{side}: watchdog_ticks must be 0 or 1000..6000")
            ticks = self.register(side, 0x09)
            if not desired_ticks and not 1000 <= ticks <= 6000:
                raise ValueError(
                    f"{side}: CAN TIMEOUT must be 1000..6000 ticks (50..300 ms) "
                    "or set watchdog_ticks so the program writes it"
                )
            self.limits[side] = tuple(
                self.register(side, r, "f") for r in (0x15, 0x16, 0x17)
            )
            if not all(math.isfinite(x) and x > 0 for x in self.limits[side]):
                raise ValueError(f"{side}: invalid PMAX/VMAX/TMAX")
            if max(abs(m["open_rad"]), abs(m["closed_rad"])) >= self.limits[side][0]:
                raise ValueError(f"{side}: calibrated travel exceeds PMAX")
            if "max_current_a" in m:
                report = self.read_current_limit(side)
                self.current_limit_reports[side] = report
                self.current_limits[side] = report["current_fraction"]
            modes[side], watchdogs[side] = mode, ticks
        for side in self.motors:
            # 当前模式可能不是配置模式：两个模式帧 ID 都发一次清错/失能。
            self._send_special(side, modes[side], CLEAR_ERROR)
            self._send_special(side, modes[side], DISABLE)
            self.disable(side)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            self.poll()
            if len(self.feedback) == len(self.motors) and all(
                f.state == 0 for f in self.feedback.values()
            ):
                break
            time.sleep(0.001)
        else:
            raise RuntimeError(
                "gripper: no disabled-state acknowledgement（若为故障，检查是否被保护锁定）"
            )
        for side, m in self.motors.items():
            desired = int(m.get("control_mode", 4))
            if modes[side] != desired:
                self.original_modes[side] = modes[side]
                self._write_register(side, 0x0A, desired)
                if self.register(side, 0x0A) != desired:
                    raise RuntimeError(f"{side}: control mode switch not confirmed")
            desired_ticks = int(m.get("watchdog_ticks", 0))
            if desired_ticks and watchdogs[side] != desired_ticks:
                self.original_timeouts[side] = watchdogs[side]
                self._write_register(side, 0x09, desired_ticks)
                if self.register(side, 0x09) != desired_ticks:
                    raise RuntimeError(f"{side}: watchdog write not confirmed")
        for side in self.motors:
            p = self.register(side, 0x50, "f")
            self.check_position(side, p)
            self.feedback[side].position = p

    def check_position(self, side, p):
        m = self.motors[side]
        lo, hi = sorted((m["open_rad"], m["closed_rad"]))
        if not math.isfinite(p) or not lo - 0.001 <= p <= hi + 0.001:
            raise RuntimeError(
                f"{side}: motor position {p} outside calibrated travel [{lo}, {hi}]"
            )

    def poll(self):
        for _ in range(256):
            packet = self.bus.recv()
            if packet is None:
                return
            cid, data = packet
            for side, m in self.motors.items():
                if cid == m["master_id"]:
                    if len(data) == 8 and data[:2] == struct.pack("<H", m["motor_id"]) and data[2] in (0x33, 0x55):
                        continue  # Register echoes are not motor status frames.
                    f = decode(data, m["motor_id"], self.limits[side], time.monotonic())
                    if f is not None:
                        self.feedback[side] = f
                        self.received_feedback.record(side, f)
        raise RuntimeError("gripper: CAN receive backlog")

    def send_mit(self, side, p, kp, kd):
        """MIT position frame on the configured mode's CAN ID (mode 1 keeps the plain ID)."""
        m = self.motors[side]
        p = bounded_position(m, p)
        payload = bytearray(mit_command(p, self.limits[side], kp, kd))
        # Quantize inward, including for negative/reversed endpoint coordinates.
        lo, hi = travel_bounds(m)
        pmax = self.limits[side][0]
        low_code = math.ceil((lo + pmax) * 65535 / (2 * pmax))
        high_code = math.floor((hi + pmax) * 65535 / (2 * pmax))
        if low_code > high_code:
            raise ValueError("gripper: travel smaller than protocol resolution")
        code = max(low_code, min(high_code, int.from_bytes(payload[:2], "big")))
        payload[:2] = code.to_bytes(2, "big")
        can_id=mode_offset(m)+m["motor_id"]
        self.bus.send(can_id, bytes(payload))
        self.last_position_commands[side]=dict(accepted_at=time.monotonic(),
            transport_channel=getattr(self.bus,'channel',None),can_id=can_id,control_mode=1,
            payload_hex=bytes(payload).hex(' '),position_rad=code*(2*pmax/65535)-pmax,
            acceptance='transport send returned successfully; not motor position acknowledgement')

    def send(self, side, p):
        """Force-position (mode 4) frame with driver-enforced speed/current limits."""
        m = self.motors[side]
        p = bounded_position(m, p)
        # Stay inside the inset range even after float32 wire rounding.
        lo, hi = travel_bounds(m)
        p = max(lo + 1e-6, min(hi - 1e-6, p))
        if "max_current_a" in m and side not in self.current_limits:
            raise RuntimeError(f"{side}: current limit preflight required")
        current = self.current_limits.get(side, m["current_fraction"])
        payload=command(p,m["speed_rad_s"],current);can_id=0x300+m["motor_id"]
        self.bus.send(can_id,payload)
        wire_position,wire_speed,wire_current=struct.unpack('<fHH',payload)
        self.last_position_commands[side]=dict(accepted_at=time.monotonic(),
            transport_channel=getattr(self.bus,'channel',None),can_id=can_id,control_mode=4,
            payload_hex=payload.hex(' '),position_rad=wire_position,
            speed_rad_s=wire_speed/100,current_fraction=wire_current/10000,
            acceptance='transport send returned successfully; not motor position acknowledgement')

    def send_position(self, side, p):
        """Dispatch the position command for the configured control mode."""
        m = self.motors[side]
        if int(m.get("control_mode", 4)) == 1:
            self.send_mit(side, p, m.get("kp", 20.0), m.get("kd", 1.0))
        else:
            self.send(side, p)

    def enable(self, side, p):
        self.send_position(side, p)  # Seed measured pose while disabled, before enabling.
        m = self.motors[side]
        self.bus.send(mode_offset(m) + m["motor_id"], ENABLE)

    def disable(self, side):
        m = self.motors[side]
        self.bus.send(mode_offset(m) + m["motor_id"], DISABLE)

    def _write_register(self, side, rid, value):
        m = self.motors[side]
        payload = struct.pack("<HBBI", m["motor_id"], 0x55, rid, value)
        self.bus.send(0x7FF, payload)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            packet = self.bus.recv()
            if packet is None:
                time.sleep(0.001)
                continue
            cid, data = packet
            if cid == m["master_id"] and len(data) == 8 and data[:4] == payload[:4]:
                if data != payload:
                    raise RuntimeError("register write acknowledgement mismatch")
                return
        raise RuntimeError(f"{side}: register 0x{rid:02x} write acknowledgement timeout")

    def _send_special(self, side, mode, payload):
        m = self.motors[side]
        self.bus.send((int(mode) - 1) * 0x100 + m["motor_id"], payload)

    def close(self):
        errors = []
        for side in self.motors:
            try:
                self.disable(side)
            except OSError as error:
                errors.append(str(error))
        for side, ticks in self.original_timeouts.items():
            try:
                self._write_register(side, 0x09, ticks)
            except (OSError, RuntimeError) as error:
                errors.append(f"{side}: watchdog restore failed: {error}")
        for side, mode in self.original_modes.items():
            try:
                self._write_register(side, 0x0A, mode)
            except (OSError, RuntimeError) as error:
                errors.append(f"{side}: control mode restore failed: {error}")
        self.bus.close()
        if errors:
            raise RuntimeError(
                "gripper disable/restore failed; firmware TIMEOUT required: "
                + "; ".join(errors)
            )


class Simulation:
    def __init__(self, motors):
        self.motors = motors
        self.feedback = {
            s: Feedback(m["open_rad"], 0, 0, 25, 25, 0, time.monotonic())
            for s, m in motors.items()
        }
        self.received_feedback = FeedbackBuffer(motors)

    def drain_feedback(self):
        return self.received_feedback.drain()

    def set_send_deadline(self, side, deadline):
        pass

    @property
    def feedback_timing(self):
        return {side: self.received_feedback.timing(side) for side in self.motors}

    def preflight(self):
        pass

    def poll(self):
        for side, f in self.feedback.items():
            f.received = time.monotonic()
            self.received_feedback.record(side, f)

    def send(self, side, p):
        self.feedback[side].position = bounded_position(self.motors[side], p)

    def send_position(self, side, p):
        self.send(side, p)

    def enable(self, side, p):
        self.feedback[side].state = 1

    def disable(self, side):
        self.feedback[side].state = 0

    def close(self):
        for side in self.motors:
            self.disable(side)
