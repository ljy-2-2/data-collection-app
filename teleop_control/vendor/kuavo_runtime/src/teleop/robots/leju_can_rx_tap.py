"""Local IPC receiver for copies made inside the existing serial receive owner.

No serial/SDK/ROS imports or discovery. Importing this module performs no I/O.
An owner-serial record can include adapter echoes: it is NOT verified motor RX.
The format deliberately cannot grant motor-online status or control permission.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import socket
import stat
import struct
import time

from teleop.robots.leju_can_capture import parse_slcan

HEADER = struct.Struct("<4sBBHIQQQQH")
MAX_PACKET = HEADER.size + 139


@dataclass(frozen=True)
class Observation:
    bus: str
    pid: int
    session: int
    sequence: int
    received_ns: int
    dropped: int
    raw: bytes


def parse_packet(packet):
    if not isinstance(packet, bytes) or not HEADER.size < len(packet) <= MAX_PACKET:
        raise ValueError("invalid packet length")
    magic, version, bus, reserved, pid, session, seq, stamp, drops, n = HEADER.unpack_from(packet)
    if magic != b"LCRX" or version != 1 or bus not in (0, 1) or reserved:
        raise ValueError("unknown tap format or bus")
    if not pid or not session or not seq or stamp < session or drops >= seq:
        raise ValueError("invalid source counters/timestamp")
    raw = packet[HEADER.size:]
    if n != len(raw) or not raw.endswith(b"\r") or b"\r" in raw[:-1]:
        raise ValueError("invalid complete record")
    return Observation(f"canbus{bus}", pid, session, seq, stamp, drops, raw)


class ObservationTracker:
    """Bounded observer state; each bus pins first PID/session until restart.

    Uses the receiver host's monotonic clock: must run on the SAME HOST as tap.
    Receive timing is not motor sample timing. Repeated payloads are legitimate;
    repeated sequence numbers are not. No converted physical motor quantities.
    """

    def __init__(self, max_age_ns=1_000_000_000):
        if type(max_age_ns) is not int or max_age_ns <= 0:
            raise ValueError("positive maximum age required")
        self.max_age_ns = max_age_ns
        self.last = {}

    def accept(self, observation, now_ns):
        if not 0 <= now_ns - observation.received_ns <= self.max_age_ns:
            raise ValueError("old or future owner timestamp")
        previous = self.last.get(observation.bus)
        if previous:
            if (previous.pid, previous.session) != (observation.pid, observation.session):
                raise ValueError("owner changed: restart observer after review")
            if (observation.sequence <= previous.sequence
                    or observation.received_ns < previous.received_ns
                    or observation.dropped < previous.dropped):
                raise ValueError("duplicate or regressing record")
        self.last[observation.bus] = observation


class LocalReceiver:
    """Explicit AF_UNIX receive only; refuses existing paths and unsafe parents.

    Requires a private existing directory owned by this UID with mode 0700.
    Accepts kernel credentials from the same UID and checks the claimed PID.
    This protects against accidental cross-user traffic, not a malicious owner.
    """

    def __init__(self, path):
        path = Path(path)
        if not path.is_absolute() or len(os.fsencode(path)) >= 108:
            raise ValueError("short absolute Unix socket path required")
        parent = path.parent
        if parent.resolve() != parent:
            raise ValueError("socket parent must not contain symlinks")
        info = parent.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("socket parent must be owned by current UID with mode 0700")
        self.path = path
        self.identity = None
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            self.socket.bind(str(path)) # Existing file/socket is never unlinked.
            info = path.lstat()
            self.identity = (info.st_dev, info.st_ino)
            path.chmod(0o600)
        except BaseException:
            self.close()
            raise

    def receive(self, timeout):
        if timeout <= 0:
            raise ValueError("positive bounded timeout required")
        self.socket.settimeout(timeout)
        packet, ancillary, flags, _ = self.socket.recvmsg(MAX_PACKET, socket.CMSG_SPACE(12))
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError("truncated datagram or credentials")
        credentials = [struct.unpack("3i", data) for level, kind, data in ancillary
                       if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(data) == 12]
        observation = parse_packet(packet)
        if len(credentials) != 1 or credentials[0][:2] != (observation.pid, os.getuid()):
            raise ValueError("source credentials do not match")
        return observation

    def close(self):
        self.socket.close()
        if self.identity is not None:
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self.identity = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def collect(receiver, duration_s, *, synthetic=False):
    """Bounded summary, no recording backlog and no inferred left/right wiring."""
    if not 0 < duration_s <= 60:
        raise ValueError("duration must be in (0, 60] seconds")
    result = dict(source="synthetic_ipc" if synthetic else "owner_serial_copy",
                  physical_motor_communication_verified=False, can_frames_sent=0,
                  received_records=0, rejected_records=0, malformed_can_records=0,
                  timestamp_kind="host_serial_batch_read_completion", buses={})
    tracker = ObservationTracker()
    deadline = time.monotonic() + duration_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            obs = receiver.receive(remaining)
            tracker.accept(obs, time.monotonic_ns())
        except socket.timeout:
            break
        except ValueError:
            result["rejected_records"] += 1
            continue
        result["received_records"] += 1
        try:
            frame = parse_slcan(obs.raw)
        except ValueError:
            result["malformed_can_records"] += 1
            continue
        summary = result["buses"].setdefault(obs.bus, dict(records=0))
        summary.update(records=summary["records"] + 1, can_id=frame.can_id,
                       is_fd=frame.is_fd, is_extended=frame.is_extended,
                       bit_rate_switch=frame.bit_rate_switch, is_remote=frame.is_remote,
                       dlc=frame.dlc, data_hex=frame.data.hex(), owner_pid=obs.pid,
                       session=obs.session, sequence=obs.sequence,
                       owner_received_ns=obs.received_ns, export_dropped=obs.dropped,
                       motor_identity_verified=False)
    result["status"] = "records_observed" if result["buses"] else "no_valid_can_records"
    return result
