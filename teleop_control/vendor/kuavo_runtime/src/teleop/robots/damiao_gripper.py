"""DM-J4310-2EC MIT/mode-4 codecs and injected CAN-owner boundary.

Adapted from TJ_FX/Marvin-VR-Teleop/gripper/damiao.py. No sockets, SDK imports,
discovery, register writes or implicit initialization. The Leju CAN-owner bridge
is injected separately. Submission means queue acceptance, not motor ACK.
"""

from dataclasses import dataclass, asdict, replace
from collections import deque
import math
import struct
import threading


ENABLE = b'\xff' * 7 + b'\xfc'
DISABLE = b'\xff' * 7 + b'\xfd'


def finite(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + ' must be a finite number')
    return float(value)


def resolve_imax(raw_imax, identity=None):
    """TJ's exact UART-verified exception; V1.1 alone is not an identity."""
    raw_imax = finite(raw_imax, 'Imax')
    if raw_imax > 0:
        return raw_imax
    if raw_imax == 0 and identity == (0x56303033, 0x37313035, 0x00000035):
        return 10.261194
    raise ValueError('unverified Imax; phase-current scaling unavailable')


@dataclass(frozen=True)
class MotorProfile:
    """Explicit, measured values; synthetic profiles are for offline tests only."""

    bus: str
    motor_id: int
    master_id: int
    open_rad: float
    closed_rad: float
    margin_rad: float
    speed_rad_s: float
    accel_rad_s2: float
    current_fraction: float
    max_current_a: float
    imax_a: float
    pmax: float
    vmax: float
    tmax: float
    feedback_timeout_ms: int = 150
    max_temperature_c: float = 60.
    control_mode: int = 4
    kp: float = 0.
    kd: float = 0.
    max_torque_nm: float = .5
    target_reserve_rad: float = 0.
    position_recovery: bool = False
    open_target_reserve_rad: float = None
    closed_target_reserve_rad: float = None

    def __post_init__(self):
        if not isinstance(self.bus, str) or not self.bus:
            raise ValueError('explicit bus name required')
        # TJ decoder identifies the motor using the low nibble of D[0].
        if type(self.motor_id) is not int or not 1 <= self.motor_id <= 15:
            raise ValueError('motor_id must be 1..15 for the verified TJ decoder')
        if type(self.master_id) is not int or not 0 <= self.master_id < 0x7ff:
            raise ValueError('invalid master_id')
        for name in ('open_rad', 'closed_rad', 'margin_rad', 'speed_rad_s',
                     'accel_rad_s2', 'current_fraction', 'max_current_a',
                     'imax_a', 'pmax', 'vmax', 'tmax', 'max_temperature_c',
                     'kp', 'kd', 'max_torque_nm', 'target_reserve_rad'):
            finite(getattr(self, name), name)
        if type(self.control_mode) is not int or self.control_mode not in (1, 4):
            raise ValueError('control_mode must be read-back MIT 1 or hybrid 4')
        if not 0 <= self.kp <= 500 or not 0 <= self.kd <= 5:
            raise ValueError('MIT gains outside protocol range')
        if self.control_mode == 1 and (self.kp < 500/4095 or self.kd < 5/4095):
            raise ValueError('MIT position control requires nonzero wire Kp and Kd')
        if not 0 < self.max_torque_nm <= self.tmax:
            raise ValueError('positive torque envelope no greater than TMAX required')
        if not .01 <= self.speed_rad_s <= 100 or not 0 < self.accel_rad_s2 <= 200:
            raise ValueError('invalid trajectory limits')
        if not 0 < self.current_fraction <= .2 or not 0 < self.max_current_a <= 2.5:
            raise ValueError('explicit phase-current limits required')
        if min(self.pmax, self.vmax, self.tmax) <= 0 or self.imax_a < 0 or (self.control_mode == 4 and self.imax_a == 0):
            raise ValueError('positive PMAX/VMAX/TMAX required; mode4 also requires positive Imax')
        if type(self.feedback_timeout_ms) is not int or not 50 <= self.feedback_timeout_ms <= 300:
            raise ValueError('feedback timeout must be 50..300 ms')
        if not 20 <= self.max_temperature_c <= 80:
            raise ValueError('invalid temperature limit')
        lo, hi = sorted((self.open_rad, self.closed_rad))
        if self.margin_rad <= 0 or hi-lo <= 2*self.margin_rad:
            raise ValueError('travel must contain two positive endpoint margins')
        reserves = self.endpoint_reserves
        for value in reserves:
            finite(value, 'endpoint target reserve')
        if self.target_reserve_rad < 0 or min(reserves) < 0 or hi-lo <= 2*self.margin_rad+sum(reserves):
            raise ValueError('target reserve must be nonnegative and leave usable travel')
        if type(self.position_recovery) is not bool:
            raise ValueError('position_recovery must be boolean')
        if self.position_recovery and (self.control_mode != 4 or self.margin_rad < .012 or
                                       min(reserves) < .01):
            raise ValueError('position recovery requires mode4, margin >= .012 and reserve >= .01 rad')
        if max(abs(lo), abs(hi)) >= min(math.pi, self.pmax):
            raise ValueError('travel must fit PMAX and cold-start single-turn range')
        if self.control_mode == 4 and self.current_code < 1:
            raise ValueError('phase-current limit below protocol resolution')

    @property
    def bounds(self):
        lo, hi = sorted((self.open_rad, self.closed_rad))
        return lo+self.margin_rad, hi-self.margin_rad

    @property
    def endpoint_reserves(self):
        """Opening/closing overrides; legacy configurations retain symmetric goals."""
        return tuple(self.target_reserve_rad if value is None else value for value in
                     (self.open_target_reserve_rad, self.closed_target_reserve_rad))

    @property
    def target_bounds(self):
        # Native envelope stays unchanged. Normal goals lie inside it so a
        # small following error does not immediately hit the feedback guard.
        lo, hi = self.bounds
        opened, closed = self.endpoint_reserves
        lower, upper = (opened, closed) if self.open_rad < self.closed_rad else (closed, opened)
        return lo+lower, hi-upper

    @property
    def takeover_bounds(self):
        lo, hi = self.bounds
        extra = .008 if self.position_recovery else 0.
        return lo-extra, hi+extra

    @property
    def feedback_tolerance_rad(self):
        return .01 if self.position_recovery else .002

    @property
    def current_code(self):
        if self.control_mode == 1:
            return None  # MIT frames have no phase-current-limit field.
        return int(min(self.current_fraction, self.max_current_a/self.imax_a)*10000)

    @property
    def effective_current_limit_a(self):
        if self.control_mode == 1:
            return None
        return self.current_code/10000*self.imax_a

    @property
    def command_can_id(self):
        return self.motor_id + (0x300 if self.control_mode == 4 else 0)

    def target(self, fraction):
        fraction = finite(fraction, 'closure fraction')
        if not 0 <= fraction <= 1:
            raise ValueError('closure fraction must be within 0..1')
        lo, hi = self.target_bounds
        return lo+fraction*(hi-lo) if self.open_rad < self.closed_rad else hi-fraction*(hi-lo)


def validate_routes(profiles, occupied_ids):
    """Conservative per-bus audit, including low-byte query aliases.

    occupied_ids must cover ALL existing command/reply/parameter IDs. Empty or
    synthetic inventories never constitute a commissioning certificate.
    """
    if not profiles or not set(profiles) <= {'left', 'right'}:
        raise ValueError('profiles require left and/or right')
    reserved = {bus: set(ids) for bus, ids in occupied_ids.items()}
    for side, p in profiles.items():
        if p.bus not in reserved:
            raise ValueError(side + ': bus inventory missing')
        ids = reserved[p.bus]
        if any(type(i) is not int or not 0 <= i <= 0x7ff for i in ids):
            raise ValueError('invalid inventory CAN ID')
        commands = {p.motor_id+n for n in (0, 0x100, 0x200, 0x300)}
        if p.master_id in commands or p.master_id & 0xff == p.motor_id:
            raise ValueError(side + ': feedback ID aliases motor query ID')
        if commands & ids or p.master_id in ids or any(
                i & 0xff in (p.motor_id, p.master_id & 0xff) for i in ids):
            raise ValueError(side + ': CAN ID/low-byte alias conflict')
        # 0x7ff parameter requests require a separately audited shared protocol.
        reserved[p.bus].update(commands | {p.master_id})


def encode_position(profile, position, *, takeover=False):
    """Use the measured mode: MIT or mode4 float32 rad/speed/current fraction."""
    position = finite(position, 'position')
    lo, hi = profile.takeover_bounds if takeover else profile.bounds
    if not lo <= position <= hi:
        raise ValueError('position outside commissioned soft travel')
    if profile.control_mode == 1:
        return encode_mit(profile, position)
    rounded = struct.unpack('<f', struct.pack('<f', position))[0]
    if not lo <= rounded <= hi:
        bits = struct.unpack('<I', struct.pack('<f', rounded))[0]
        up = rounded < lo
        bits += 1 if up == (rounded >= 0) else -1
        rounded = struct.unpack('<f', struct.pack('<I', bits))[0]
    if not lo <= rounded <= hi:
        raise ValueError('travel narrower than float32 resolution')
    return struct.pack('<fHH', rounded, int(profile.speed_rad_s*100), profile.current_code)


def encode_mit(profile, position, *, kp=None, kd=None):
    """Tianji MIT wire layout, zero requested velocity/feedforward torque.

    The odd-width symmetric encodings cannot represent an exact zero velocity
    or torque; they use the manufacturer's truncated midpoint. This function
    does not implement a hardware current limit.
    """
    position = finite(position, 'MIT position')
    kp = finite(profile.kp if kp is None else kp, 'MIT Kp')
    kd = finite(profile.kd if kd is None else kd, 'MIT Kd')
    if not -profile.pmax <= position <= profile.pmax or not 0 <= kp <= 500 or not 0 <= kd <= 5:
        raise ValueError('MIT position/gains outside protocol range')
    p = int((position+profile.pmax)*65535/(2*profile.pmax))
    # For motion targets, round inward so a boundary cannot quantize outside
    # the calibrated soft travel. Transparent calibration may lie outside it.
    if kp > 0:
        lo, hi = profile.bounds
        lower = math.ceil((lo+profile.pmax)*65535/(2*profile.pmax))
        upper = math.floor((hi+profile.pmax)*65535/(2*profile.pmax))
        if not lo <= position <= hi or lower > upper:
            raise ValueError('MIT target outside representable soft travel')
        p = max(lower, min(upper, p))
    v, t = 2047, 2047
    k, d = int(kp*4095/500), int(kd*4095/5)
    return bytes((p>>8, p&255, v>>4, ((v&15)<<4)|(k>>8), k&255,
                  d>>4, ((d&15)<<4)|(t>>8), t&255))


def transparent_payload(profile, sequence=0):
    """Explicit MIT calibration/status payload; never sends or enables.

    Alternating the irrelevant position LSB mirrors Tianji's duplicate-frame
    workaround; both gains stay zero. Residual torque quantization is <=1 LSB.
    """
    if type(sequence) is not int or sequence < 0:
        raise ValueError('nonnegative sequence required')
    return encode_mit(profile, 2*profile.pmax/65535 if sequence & 1 else 0., kp=0., kd=0.)


def mit_torque_estimate(profile, payload, feedback):
    """PD+feedforward estimate from actual encoded values, not a current limit."""
    p = int.from_bytes(payload[:2], 'big')*(2*profile.pmax/65535)-profile.pmax
    v = ((payload[2]<<4)|(payload[3]>>4))*(2*profile.vmax/4095)-profile.vmax
    kp = (((payload[3]&15)<<8)|payload[4])*500/4095
    kd = ((payload[5]<<4)|(payload[6]>>4))*5/4095
    torque = (((payload[6]&15)<<8)|payload[7])*(2*profile.tmax/4095)-profile.tmax
    return kp*(p-feedback.position_rad)+kd*(v-feedback.velocity_rad_s)+torque


@dataclass(frozen=True)
class MotorFeedback:
    position_rad: float
    velocity_rad_s: float
    torque_nm: float
    mos_c: int
    rotor_c: int
    state: int
    received_ns: int
    sequence: int


def decode_feedback(profile, data, received_ns, sequence):
    if not isinstance(data, bytes) or len(data) != 8 or data[0] & 15 != profile.motor_id:
        raise ValueError('unexpected DM feedback payload')
    if type(received_ns) is not int or received_ns < 0 or type(sequence) is not int or sequence < 1:
        raise ValueError('actual receive timestamp and sequence required')
    return MotorFeedback(
        int.from_bytes(data[1:3], 'big')*(2*profile.pmax/65535)-profile.pmax,
        ((data[3]<<4) | (data[4]>>4))*(2*profile.vmax/4095)-profile.vmax,
        (((data[4]&15)<<8) | data[5])*(2*profile.tmax/4095)-profile.tmax,
        data[6], data[7], data[0]>>4, received_ns, sequence)


class DamiaoGripperDriver:
    """Explicit injection seam for a SINGLE existing CAN owner; no device I/O.

    submit(bus, frames, deadline_ns) must be nonblocking and atomically accept
    the ordered batch or reject ALL of it. Owner must recheck expiry before each
    physical send, reject superseded targets, prioritize revocation, and never
    replay queued targets after disable. No production submitter is provided.
    Control mode, firmware watchdog, units, bus format and identity are verified
    by the bridge before it allows submission. MIT's torque envelope is a
    software PD estimate; only mode4 carries a phase-current cap on the wire.
    """

    def __init__(self, profiles, *, occupied_ids, submit, acknowledged_target=None):
        validate_routes(profiles, occupied_ids)
        if not callable(submit):
            raise ValueError('explicit CAN-owner submitter required')
        self.profiles = dict(profiles)
        self._submit = submit
        self._acknowledged_target = acknowledged_target
        self._feedback = {}
        self._lock = threading.Lock()
        self.accepted_batches = 0
        self._history = {side: deque(maxlen=512) for side in profiles}
        self._stamps = {side: deque(maxlen=101) for side in profiles}
        self.dropped_feedback = dict.fromkeys(profiles, 0)

    def receive(self, bus, can_id, data, *, received_ns, is_fd=False,
                is_extended=False, is_remote=False, is_error=False, tx_echo=False,
                position_offset_rad=0., recording_target=None):
        """Called only for an actual received classic CAN data frame, never TX echo."""
        if any((is_fd, is_extended, is_remote, is_error, tx_echo)):
            return False  # This offline boundary currently models classic STD data frames.
        if type(can_id) is not int or type(received_ns) is not int or received_ns < 0:
            raise ValueError('actual CAN ID and receive timestamp required')
        for side, profile in self.profiles.items():
            if (bus, can_id) != (profile.bus, profile.master_id):
                continue
            # Parameter echoes are not fresh position feedback. This driver
            # never issues parameter requests; do not accept replies from one.
            if isinstance(data, bytes) and len(data) == 8 and data[:2] == bytes((profile.motor_id, 0)) and data[2] in (0x33, 0x55, 0xaa):
                return False
            with self._lock:
                old = self._feedback.get(side)
                if old and received_ns <= old.received_ns:
                    return False
                value = decode_feedback(profile, data, received_ns, 1 if old is None else old.sequence+1)
                raw_position = value.position_rad
                offset = finite(position_offset_rad, 'session position offset')
                if offset:
                    value = replace(value, position_rad=raw_position-offset)
                self._feedback[side] = value
                self._stamps[side].append(received_ns)
                rows = self._history[side]
                if len(rows) == rows.maxlen:
                    self.dropped_feedback[side] += 1
                rows.append(dict(asdict(value), bus=bus, can_id=can_id, raw_hex=data.hex(),
                                 raw_position_rad=raw_position, position_offset_rad=offset,
                                 recording_target=dict(recording_target) if recording_target else None))
            return True
        return False

    def feedback(self, side):
        with self._lock:
            return self._feedback.get(side)

    def observed_hz(self, side):
        with self._lock:
            stamps = self._stamps[side]
            return ((len(stamps)-1)*1e9/(stamps[-1]-stamps[0])
                    if len(stamps) > 1 and stamps[-1] > stamps[0] else None)

    def drain_feedback(self):
        with self._lock:
            result = {side: list(rows) for side, rows in self._history.items()}
            for rows in self._history.values():
                rows.clear()
            return result

    def _send(self, side, payloads, deadline_ns):
        p = self.profiles[side]
        if type(deadline_ns) is not int or deadline_ns <= 0:
            raise ValueError('explicit positive command deadline required')
        if p.control_mode == 1:
            f = self.feedback(side)
            for data in payloads:
                if data in (ENABLE, DISABLE):
                    continue
                if f is None or abs(mit_torque_estimate(p, data, f)) > p.max_torque_nm:
                    raise ValueError('MIT torque estimate exceeds configured envelope or feedback missing')
        accepted = self._submit(p.bus, tuple((p.command_can_id, data) for data in payloads), deadline_ns)
        if type(accepted) is not bool:
            raise ValueError('CAN owner must explicitly accept or reject batch')
        self.accepted_batches += int(accepted)
        return accepted

    def acknowledged_target(self, side):
        """Last owner-confirmed wire target (rad, local ACK ns), NOT motor pose."""
        return self._acknowledged_target(side) if self._acknowledged_target else None

    def enable(self, side, position, *, deadline_ns):
        return self._send(side, (encode_position(self.profiles[side], position, takeover=True), ENABLE), deadline_ns)

    def position(self, side, position, *, deadline_ns):
        return self._send(side, (encode_position(self.profiles[side], position),), deadline_ns)

    def disable(self, side, *, deadline_ns):
        return self._send(side, (DISABLE,), deadline_ns)
