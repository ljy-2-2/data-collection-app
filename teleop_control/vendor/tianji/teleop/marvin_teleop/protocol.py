"""Galaxis PackageHandle.cs framing, TCP stream assembly and latest-frame watchdog."""

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, replace, field
from .geometry import pose

GRIP_ON = 0.75
GRIP_OFF = 0.2


class GripFilter:
    """Per-hand 30 ms ordinary-release debounce, measured on source timestamps."""

    def __init__(self):
        self.values = {s: 0.0 for s in ("left", "right")}
        self.low_since = {s: None for s in self.values}

    def update(self, grips, stamp):
        for side, value in grips.items():
            if value > GRIP_OFF:
                self.values[side], self.low_since[side] = value, None
            elif self.values[side] <= GRIP_OFF:
                self.values[side] = value
            else:
                if self.low_since[side] is None:
                    self.low_since[side] = stamp
                if stamp - self.low_since[side] >= 0.03 - 1e-9:
                    self.values[side] = value
        return self.values.copy()


def pack(command, payload, timestamp=0, head=0x3F):
    return (
        struct.pack("<BBI", head, command, len(payload))
        + payload
        + struct.pack("<qB", timestamp, 0xA5)
    )


class Decoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        packets = []
        while len(self.buffer) >= 6:
            head, cmd, length = struct.unpack_from("<BBI", self.buffer)
            if head != 0x3F or length > 262144:
                raise ValueError("bad packet header/length")
            if len(self.buffer) < length + 15:
                break
            if self.buffer[length + 14] != 0xA5:
                raise ValueError("bad packet terminator")
            payload = bytes(self.buffer[6 : 6 + length])
            packets.append((cmd, payload))
            del self.buffer[: length + 15]
        return packets


@dataclass(frozen=True)
class Frame:
    received: float
    session: int
    sequence: int
    source_time: float
    focused: bool
    tracked: bool
    hands: dict
    head: object
    grips: dict
    stop: bool
    safety_epoch: int = 0
    release_epochs: dict = field(default_factory=lambda: {"left": 0, "right": 0})
    control_grips: dict | None = None
    triggers: dict = field(default_factory=dict)
    buttons: dict = field(default_factory=dict)


def parse_frame(data, session, received):
    """Read the ORIGINAL TrackingData.cs schema, not a custom/new APK protocol.

    The old app/SDK has no per-controller tracking flags. `tracked` below means
    numeric pose validation only; it must not be reported as sensor tracking proof.
    """
    import math

    stamp = data["timeStampNs"]
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("invalid source timestamp")
    controls = data["Controller"]
    hands = {side: pose(controls[side]["pose"]) for side in ("left", "right")}
    grips = {side: float(controls[side]["grip"]) for side in hands}
    if any(not math.isfinite(g) or not 0 <= g <= 1 for g in grips.values()):
        raise ValueError("invalid grip")
    # Missing trigger is unavailable, never an implicit command to open.
    triggers = {
        s: float(controls[s]["trigger"]) for s in hands if "trigger" in controls[s]
    }
    if any(not math.isfinite(t) or not 0 <= t <= 1 for t in triggers.values()):
        raise ValueError("invalid trigger")
    head = pose(data["Head"]["pose"])
    # Direct TCP supplies focus. The reference SDK does not expose this field.
    focus = data.get("appState", {}).get("focus", True)
    if type(focus) is not bool:
        raise ValueError("invalid app focus")
    return Frame(
        received,
        session,
        stamp,
        stamp / 1e9,
        focus,
        True,
        hands,
        head,
        grips,
        False,  # A/X start and B/Y save data; face buttons never stop motion.
        triggers=triggers,
        buttons={"A": controls["right"].get("primaryButton"),
                 "B": controls["right"].get("secondaryButton"),
                 "X": controls["left"].get("primaryButton"),
                 "Y": controls["left"].get("secondaryButton")},
    )


class VRServer:
    def __init__(self, host, port, timeout):
        self.lock = threading.Lock()
        self.frame = None
        self.stop_latched = False
        self.error = "waiting for VR"
        self.connected = False
        self.peer = None
        self.session = 0
        self.timeout = timeout
        self.shutdown = threading.Event()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(1)
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return self.frame, self.stop_latched, self.error, self.connected

    def run(self):
        while not self.shutdown.is_set():
            try:
                client, peer = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            decoder = Decoder()
            last_seq = 0
            last_source = -1.0
            baseline = None
            safety_epoch = 0
            release_epochs = {"left": 0, "right": 0}
            grip_filter = GripFilter()
            with self.lock:
                self.session += 1
                self.connected, self.peer = True, peer[0]
            client.settimeout(0.1)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            last_bytes = time.monotonic()
            try:
                with client:
                    while not self.shutdown.is_set():
                        try:
                            chunk = client.recv(65536)
                        except socket.timeout:
                            if time.monotonic() - last_bytes > 3:
                                raise ValueError("VR socket idle")
                            continue
                        if not chunk:
                            break
                        last_bytes = time.monotonic()
                        for cmd, payload in decoder.feed(chunk):
                            if cmd != 0x6D:
                                continue
                            envelope = json.loads(payload)
                            function = envelope.get("functionName")
                            if function != "Tracking":
                                continue
                            data = envelope["value"]
                            if isinstance(data, str):
                                data = json.loads(data)
                            now = time.monotonic()
                            frame = parse_frame(data, self.session, now)
                            if (
                                frame.sequence <= last_seq
                                or frame.source_time <= last_source
                            ):
                                raise ValueError("replayed or nonadvancing VR frame")
                            if not frame.tracked or not frame.focused:
                                safety_epoch += 1
                            filtered = grip_filter.update(
                                frame.grips, frame.source_time
                            )
                            for side, grip in filtered.items():
                                if grip <= GRIP_OFF:
                                    release_epochs[side] += 1
                            frame = replace(
                                frame,
                                safety_epoch=safety_epoch,
                                release_epochs=release_epochs.copy(),
                                control_grips=filtered,
                            )
                            offset = now - frame.source_time
                            baseline = (
                                offset if baseline is None else min(baseline, offset)
                            )
                            if offset - baseline > self.timeout:
                                raise ValueError("VR backlog exceeds latency limit")
                            last_seq, last_source = frame.sequence, frame.source_time
                            with self.lock:
                                self.frame, self.error = frame, ""
                                if frame.stop:
                                    self.stop_latched = True
            except (ValueError, KeyError, TypeError, OSError, OverflowError) as error:
                with self.lock:
                    self.error = str(error)
            finally:
                with self.lock:
                    self.connected, self.frame = False, None
                    if not self.error:
                        self.error = "VR disconnected"

    def close(self):
        self.shutdown.set()
        self.sock.close()
        self.thread.join(timeout=1)
