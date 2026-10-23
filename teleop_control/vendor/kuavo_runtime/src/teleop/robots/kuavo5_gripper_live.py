"""Explicit client and calibration helpers for the existing Leju CAN owner.

Import/construction has no I/O. Register/status reads SEND CAN frames; explicit
preparation and trial functions may write timeout/enable/targets. Continuous
teleoperation uses kuavo5_gripper_owner, never a second serial/CAN owner.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import time

REQUEST = struct.Struct("<4sQQBBH8s")
COMMISSION = struct.Struct("<4sQQBBH12f")
COMMISSION_RECOVERY = struct.Struct("<4sQQBBH14f")
REPLY = struct.Struct("<4sBBBBQQHBB8sI")
ERRORS = {1: "invalid request", 2: "expired command", 3: "operation not permitted",
          4: "owner/serial busy (no bytes sent)", 5: "preflight/feedback incomplete",
          6: "position/speed/current limit", 7: "session revoked", 8: "serial transport fault"}
REGISTERS = {7: ("master_id", "I"), 8: ("motor_id", "I"), 9: ("watchdog_ticks", "I"),
             10: ("mode", "I"), 0x23: ("bitrate_code", "I"), 0x15: ("pmax", "f"),
             0x16: ("vmax", "f"), 0x17: ("tmax", "f"), 0x3b: ("imax_a", "f"),
             0x0d: ("hardware_version", "I"), 0x0e: ("software_version", "I"),
             0x24: ("sub_version", "I")}
DISABLE = b"\xff"*7+b"\xfd"
ENABLE = b"\xff"*7+b"\xfc"


class OwnerBusy(RuntimeError):
    """Explicit native BUSY acknowledgement: this request sent no CAN bytes."""


class OwnerExpired(RuntimeError):
    """Expired before host write; not an acknowledgement timeout/unknown result."""


def check_inventory(path, side):
    """An explicit operator-reviewed inventory; never promote the candidate file."""
    from .damiao_gripper import validate_routes
    from types import SimpleNamespace
    value = json.loads(Path(path).read_text())
    bus, mid = ("canbus0", 8) if side == "left" else ("canbus1", 1)
    record = value.get("buses", {}).get(bus, {})
    if (record.get("complete") is not True or record.get("parameter_0x7ff_reviewed") is not True
            or record.get("adapter_rx_echo_excluded") is not True
            or not isinstance(record.get("evidence"), str) or not record["evidence"].strip()
            or not isinstance(record.get("occupied_ids"), list) or not record["occupied_ids"]):
        raise ValueError("必须提供该总线完整编号清单、0x7FF 参数协议及接收回显检查的现场证据；候选配置不够")
    validate_routes({side: SimpleNamespace(bus=bus, motor_id=mid, master_id=0)},
                    {bus: record["occupied_ids"]})
    return value


def finite_range(value, low, high, name):
    if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite and within [{low}, {high}]")
    return value


@dataclass(frozen=True)
class Trial:
    delta_rad: float
    speed_rad_s: float
    current_a: float

    def __post_init__(self):
        finite_range(abs(self.delta_rad), .005, .1, "abs(delta_rad)")
        finite_range(self.speed_rad_s, .01, .2, "speed_rad_s")
        finite_range(self.current_a, .01, .5, "phase current_a")
        # Quantization is downward; use the wire speed to bound duration.
        if abs(self.delta_rad)/(int(self.speed_rad_s*100)/100)+1.5 > 8:
            raise ValueError("trial exceeds 8 seconds; reduce displacement")

    def payload(self, position, imax, pmax):
        finite_range(imax, .01, 1000, "read-back Imax")
        if not math.isfinite(position) or not math.isfinite(pmax) or pmax <= 0 or abs(position) >= pmax:
            raise ValueError("position outside read-back PMAX")
        current_code = int(min(self.current_a/imax, .1)*10000)
        if current_code < 1:
            raise ValueError("current below protocol resolution")
        return struct.pack("<fHH", position, int(self.speed_rad_s*100), current_code)


class OwnerClient:
    """Explicit connection to one given local socket; never opens tty/CAN devices."""

    def __init__(self, directory, side, log):
        if side not in ("left", "right"):
            raise ValueError("explicit left or right side required")
        self.mid = 8 if side == "left" else 1
        self.bus = "canbus0" if side == "left" else "canbus1"
        self.path = Path(directory)/f"{self.bus}.sock"
        self.sock = None
        self.seq = 0
        self.events = []
        self.feedback = None
        self.feedback_sequence = 0
        self.log = log
        self.host_writes = 0
        self.on_feedback = None

    def connect(self):
        parent = self.path.parent.lstat()
        node = self.path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o777 != 0o700
                or not stat.S_ISSOCK(node.st_mode) or node.st_mode & 0o777 != 0o600
                or parent.st_uid != node.st_uid or node.st_uid not in (0, os.geteuid())):
            raise ValueError("owner socket must be in its owner's private 0700 directory, mode 0600")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock.settimeout(.5)
        self.sock.connect(str(self.path))
        self.pid, uid, _ = struct.unpack("3i", self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != node.st_uid:
            raise ValueError("socket peer identity does not match file owner")
        hello = self.receive(.5)
        if hello["type"] != 3:
            raise ValueError("owner bridge handshake missing")
        self.control = hello["control"]
        self.log("connected", bus=self.bus, motor_id=self.mid, owner_pid=self.pid,
                 control_enabled=self.control)

    def receive(self, timeout):
        self.sock.settimeout(max(.001, timeout))
        raw = self.sock.recv(REPLY.size+1)
        if len(raw) != REPLY.size:
            raise RuntimeError("owner disconnected or invalid response length")
        magic, kind, result, mid, control, seq, stamp, cid, length, reserved, data, pid = REPLY.unpack(raw)
        now = time.monotonic_ns()
        if (magic != b"DMR1" or kind not in (1, 2, 3) or mid != self.mid or pid != self.pid
                or length > 8 or reserved or not 0 < stamp <= now or now-stamp > 500_000_000):
            raise RuntimeError("invalid/stale owner response")
        event = dict(type=kind, result=result, sequence=seq, received_ns=stamp,
                     can_id=cid, data=data[:length], control=bool(control))
        if kind == 2:
            self.log("rx", can_id=cid, data_hex=data[:length].hex(), owner_received_ns=stamp)
            if cid != 0 or length != 8:
                raise RuntimeError("unexpected route from owner")
            if data[:2] == bytes((self.mid, 0)) and data[2] in (0x33, 0x55, 0xaa, 0xcc):
                self.events.append(event)
                if len(self.events) > 64:
                    raise RuntimeError("register event backlog")
            elif data[0] & 15 == self.mid:
                if self.feedback and stamp < self.feedback["received_ns"]:
                    raise RuntimeError("feedback timestamp regressed")
                self.feedback_sequence += 1
                self.feedback = event
                if self.on_feedback is not None:
                    self.on_feedback(event)
        return event

    def send(self, cid, data, *, deadline_ns=None):
        if not isinstance(data, bytes) or len(data) > 8:
            raise ValueError("classic CAN payload required")
        # Only explicit BUSY is retried here; never replay an expired target.
        # Read queries are not motion leases. Give them a bounded 50 ms IPC
        # window; control always retains its caller's absolute input deadline.
        read_query = (cid == 0x7ff and len(data) == 8 and
                      data[:2] == struct.pack('<H', self.mid) and data[4:] == bytes(4) and
                      ((data[2] == 0x33 and data[3] in set(REGISTERS) | {0x50, 0x51}) or
                       (data[2] == 0xcc and data[3] == 0)))
        end = time.monotonic()+.08
        if deadline_ns is not None:
            end = min(end, deadline_ns/1e9)
        busy_replies = 0
        while True:
            self.seq += 1
            now = time.monotonic_ns()
            window = 50_000_000 if read_query or deadline_ns is not None else 20_000_000
            deadline = min(now+window, deadline_ns) if deadline_ns is not None else now+window
            if deadline <= now:
                raise OwnerExpired("owner request lease expired; no frame submitted")
            packet = REQUEST.pack(b"DMQ1", self.seq, deadline, 1, len(data), cid, data.ljust(8, b"\0"))
            self.sock.settimeout(.03)
            self.sock.sendall(packet)
            ack_end = time.monotonic()+.08
            while True:
                remaining = ack_end-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("owner write acknowledgement timed out; not retransmitting")
                reply = self.receive(remaining)
                if reply["type"] != 1:
                    continue
                if reply["sequence"] != self.seq:
                    raise RuntimeError("owner acknowledgement sequence mismatch")
                break
            if reply["result"] == 4 and time.monotonic() < end:
                # The owner already serializes requests through its receive
                # loop (about 2 ms per ACK on this robot). Another fixed 2 ms
                # sleep skips polling opportunities and can exhaust the shared
                # seed/enable lease. Only explicit unsent BUSY is retried, with
                # the SAME caller deadline; never retry EXPIRED/unknown ACKs.
                busy_replies += 1
                continue
            if reply["result"]:
                self.log("host_write_rejected", can_id=cid, data_hex=data.hex(),
                         sequence=self.seq, result=reply['result'], request_ns=now,
                         busy_replies=busy_replies,
                         deadline_ns=deadline, owner_ack_ns=reply.get('received_ns'),
                         received_ns=time.monotonic_ns())
            if reply["result"] == 4:
                raise OwnerBusy(ERRORS[4])
            if reply["result"] == 2:
                raise OwnerExpired(ERRORS[2])
            if reply["result"]:
                raise RuntimeError(ERRORS.get(reply["result"], "unknown bridge error"))
            self.host_writes += 1
            self.log("host_write", can_id=cid, data_hex=data.hex(), sequence=self.seq,
                     busy_replies=busy_replies,
                     request_ns=now, deadline_ns=deadline, owner_ack_ns=reply.get('received_ns'),
                     motor_acknowledged=False)
            return now

    def commission(self, profile):
        """Install one explicit mechanical envelope; sends IPC only, no CAN control."""
        self.seq += 1
        now = time.monotonic_ns()
        fields = (*profile.bounds, profile.speed_rad_s, profile.effective_current_limit_a or 0.,
                  profile.imax_a, profile.pmax, profile.vmax, profile.tmax,
                  profile.kp, profile.kd, profile.max_torque_nm, profile.max_temperature_c)
        codec, magic = COMMISSION, b"DMC1"
        if profile.position_recovery:
            codec, magic = COMMISSION_RECOVERY, b"DMC2"
            fields += tuple(sorted((profile.open_rad, profile.closed_rad)))
        packet = codec.pack(magic, self.seq, now+50_000_000,
                            profile.control_mode, self.mid, 0, *fields)
        self.sock.sendall(packet)
        end = time.monotonic()+.1
        while time.monotonic() < end:
            reply = self.receive(end-time.monotonic())
            if reply["type"] == 1:
                if reply["sequence"] != self.seq:
                    raise RuntimeError("commission acknowledgement sequence mismatch")
                if reply["result"]:
                    raise RuntimeError("owner commissioning rejected: "+ERRORS.get(reply["result"], "unknown"))
                self.log("commissioned_envelope", mode=profile.control_mode,
                         lower_rad=profile.bounds[0], upper_rad=profile.bounds[1],
                         position_recovery=profile.position_recovery,
                         feedback_tolerance_rad=profile.feedback_tolerance_rad)
                return
        raise TimeoutError("owner commissioning acknowledgement missing")

    def refresh_status(self, *, expected_state=0):
        since = self.send(0x7ff, struct.pack("<HBBI", self.mid, 0xcc, 0, 0))
        return self.wait_state(expected_state, since)

    def register(self, rid, fmt="I"):
        header = struct.pack("<HBB", self.mid, 0x33, rid)
        self.events.clear()
        # Official DM SDK transmits eight bytes; the reply retains the four-byte
        # header and replaces the zero padding with the register value.
        sent = self.send(0x7ff, header + bytes(4))
        end = time.monotonic()+.3
        while True:
            for event in self.events:
                if event["received_ns"] >= sent and event["data"][:4] == header:
                    self.last_register_received_ns = event["received_ns"]
                    return struct.unpack("<"+fmt, event["data"][4:])[0]
            if time.monotonic() >= end:
                raise RuntimeError(f"register {rid:#x}: no matching motor reply")
            self.receive(end-time.monotonic())

    def wait_state(self, state, since, timeout=.25):
        end = time.monotonic()+timeout
        while True:
            f = self.feedback
            if f and f["received_ns"] >= since and time.monotonic_ns()-f["received_ns"] < 100_000_000:
                actual = f["data"][0] >> 4
                if actual == state:
                    return f
                if actual > 1:
                    raise RuntimeError(f"motor fault state={actual}; no automatic fault clear")
            if time.monotonic() >= end:
                raise RuntimeError(f"motor state {state} not acknowledged")
            self.receive(end-time.monotonic())

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


def inspect_motor(client):
    values = {name: client.register(rid, fmt) for rid, (name, fmt) in REGISTERS.items()}
    if (values["motor_id"] != client.mid or values["master_id"] != 0 or values["bitrate_code"] != 4):
        raise RuntimeError(f"actual route/bitrate differs from agreed configuration: {values}")
    if values["mode"] not in (1, 2, 3, 4):
        raise RuntimeError("unsupported current control mode")
    for name in ("pmax", "vmax", "tmax", "imax_a"):
        finite_range(values[name], 0. if name=="imax_a" and values["mode"]==1 else .00001, 1e6, name)
    client.log("register_readback", **values)
    return values


def require_hybrid_mode(values):
    if values.get("mode") != 4:
        raise RuntimeError(f"motor reports mode {values.get('mode')}; this entry requires read-back mode4 "
                           "(hybrid control). No mode change or motor control was sent")


def prepare_motor(client, values):
    require_hybrid_mode(values)
    if not client.control:
        raise RuntimeError("owner is inspect-only; operator must configure control-enabled owner")
    # Explicit prepare is a hardware action in an ALREADY configured mode4:
    # disable the selected motor and set only its volatile timeout.
    since = client.send(0x300+client.mid, DISABLE)
    client.wait_state(0, since)
    if values["watchdog_ticks"] != 4000:
        client.send(0x7ff, struct.pack("<HBBI", client.mid, 0x55, 9, 4000))
        if client.register(9) != 4000:
            raise RuntimeError("volatile timeout write/readback mismatch")
        values["watchdog_ticks"] = 4000
    # Refresh disabled-state proof after elapsed preparation work.
    since = client.send(0x300+client.mid, DISABLE)
    client.wait_state(0, since)
    client.log("prepared", mode=4, watchdog_ticks=4000, flash_written=False,
               automatic_restore=False)
    return values


def run_trial(client, values, trial):
    require_hybrid_mode(values)
    from .damiao_gripper import decode_feedback
    from types import SimpleNamespace
    profile = SimpleNamespace(motor_id=client.mid, pmax=values["pmax"], vmax=values["vmax"], tmax=values["tmax"])
    # Fresh DISABLED acknowledgement, followed by exact (not 16-bit) position.
    since = client.send(0x300+client.mid, DISABLE)
    client.wait_state(0, since)
    origin = client.register(0x50, "f")
    target = origin+trial.delta_rad
    seed = trial.payload(origin, values["imax_a"], values["pmax"])
    trial.payload(target, values["imax_a"], values["pmax"])
    quantization = 2*values["pmax"]/65535
    if quantization > abs(trial.delta_rad)/4:
        raise RuntimeError("feedback resolution too coarse for requested displacement")
    client.log("trial_start", origin_rad=origin, target_rad=target, delta_rad=trial.delta_rad,
               speed_rad_s=int(trial.speed_rad_s*100)/100,
               effective_current_a=struct.unpack("<fHH", seed)[2]/10000*values["imax_a"])
    attempted_enable = False
    completed = False
    try:
        client.send(0x300+client.mid, seed)
        attempted_enable = True  # Even a lost enable ACK requires termination handling.
        since = client.send(0x300+client.mid, ENABLE)
        client.wait_state(1, since, .08)
        start = time.monotonic()
        speed = int(trial.speed_rad_s*100)/100
        travel_time = abs(trial.delta_rad)/speed
        position = origin
        sequence = 0
        while time.monotonic()-start < travel_time+1.0:
            tick = time.monotonic()
            elapsed = tick-start
            commanded = origin+math.copysign(min(abs(trial.delta_rad), speed*elapsed), trial.delta_rad)
            sent = client.send(0x300+client.mid, trial.payload(commanded, values["imax_a"], values["pmax"]))
            event = client.wait_state(1, sent, .08)
            sequence += 1
            f = decode_feedback(profile, event["data"], event["received_ns"], sequence)
            position = f.position_rad
            if max(f.mos_c, f.rotor_c) > 55 or abs(position-origin) > .12:
                raise RuntimeError("feedback outside trial temperature/displacement bounds")
            client.log("motor_feedback", position_rad=position, velocity_rad_s=f.velocity_rad_s,
                       state=f.state, mos_c=f.mos_c, rotor_c=f.rotor_c,
                       owner_received_ns=f.received_ns)
            time.sleep(max(0, .01-(time.monotonic()-tick)))
        tolerance = max(.003, 3*quantization)
        if abs(position-target) > tolerance or (position-origin)*trial.delta_rad <= 0:
            raise RuntimeError("target not reached; inspect load/current/feedback instead of automatically increasing current")
        completed = True
    except BaseException as error:
        client.log("trial_failure", error=str(error))
        raise
    finally:
        if attempted_enable:
            try:
                since = client.send(0x300+client.mid, DISABLE)
                client.wait_state(0, since)
                client.log("disabled_acknowledged")
            except BaseException as error:
                client.log("disable_unconfirmed", error=str(error), hardware_emergency_stop_if_abnormal=True)
                raise
    return dict(result="trial_passed" if completed else "trial_failed", origin_rad=origin,
                target_rad=target, final_position_rad=position, disabled_acknowledged=True)


def capture_calibration_sample(client, values=None):
    """Disabled hand marking: read both position registers and fresh status.

    No transparent torque, enable, disable, mode change or watchdog write.
    """
    from .damiao_gripper import decode_feedback
    from types import SimpleNamespace
    values = inspect_motor(client) if values is None else values
    started = time.monotonic_ns()
    client.refresh_status(expected_state=0)
    position = client.register(0x50, "f")
    xout = client.register(0x51, "f")
    xout_received_ns = client.last_register_received_ns
    event = client.refresh_status(expected_state=0)
    for name, value in (("position", position), ("xout", xout)):
        finite_range(value, -values["pmax"], values["pmax"], name)
    f = decode_feedback(SimpleNamespace(motor_id=client.mid, **{
        k: values[k] for k in ("pmax", "vmax", "tmax")}), event["data"], event["received_ns"], client.feedback_sequence)
    if time.monotonic_ns()-started > 500_000_000 or abs(f.velocity_rad_s) > max(.1, 2*values["vmax"]/4095):
        raise RuntimeError("calibration sample stale or motor moving; settle at endpoint")
    if max(f.mos_c, f.rotor_c) >= 60:
        raise RuntimeError("motor too hot for calibration")
    if abs(position-f.position_rad) > max(.002, 3*2*values["pmax"]/65535):
        raise RuntimeError("motor moved while calibration positions were read")
    return dict(position_rad=position, xout_rad=xout, xout_received_ns=xout_received_ns,
                received_ns=event["received_ns"],
                feedback=f, readback=dict(values), raw_hex=event["data"].hex())


def prepare_watchdog(client, values):
    """Operator-requested volatile timeout setup only; never changes motor mode."""
    if not client.control or values.get("mode") not in (1, 4):
        raise RuntimeError("timeout preparation requires control owner and read-back mode1 or mode4")
    client.refresh_status(expected_state=0)
    if values["watchdog_ticks"] != 4000:
        client.send(0x7ff, struct.pack("<HBBI", client.mid, 0x55, 9, 4000))
        if client.register(9) != 4000:
            raise RuntimeError("volatile watchdog write/readback mismatch")
        values["watchdog_ticks"] = 4000
    client.log("watchdog_prepared", watchdog_ticks=4000, mode=values["mode"], flash_written=False)
    return values

# Explicit operator CLI naming; identical timeout-only operation.
prepare_teleop_motor = prepare_watchdog
