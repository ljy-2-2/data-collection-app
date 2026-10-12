"""TCP server transport for the Galaxis PICO 4 Ultra Unity client.

The headset connects to this server on TCP 63901 over Wi-Fi or through
``adb reverse``.  This module deliberately contains every Galaxis wire-format
detail so the ROS publisher remains independent of the Unity project.
"""

from __future__ import annotations

import json
import math
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class PicoFrame:
    """Protocol-neutral PICO sample handed from transport to ROS publishers."""

    device_time_ns: int
    pc_monotonic_ns: int
    seq: int
    pico_id: str
    delta_time_s: float
    raw: Dict[str, Any] = field(default_factory=dict)


_PACKET_HEAD = 0x3F
_PACKET_END = 0xA5
_SERVER_PACKET_HEAD = 0xCF
_CMD_CONNECT = 0x19
_CMD_FUNCTION = 0x6D
_CMD_SERVER_FUNCTION = 0x5F
_HEADER_SIZE = 6
_TRAILER_SIZE = 9  # int64 timestamp + end byte
_MIN_PACKET_SIZE = _HEADER_SIZE + _TRAILER_SIZE
_MAX_PAYLOAD_SIZE = 4 * 1024 * 1024


def _encode_server_packet(
    cmd: int,
    payload: bytes,
    *,
    timestamp_ms: Optional[int] = None,
) -> bytes:
    """Encode one PC-to-headset packet accepted by ``PackageHandle.Unpack``."""
    if len(payload) > _MAX_PAYLOAD_SIZE:
        raise ValueError("Galaxis server payload exceeds the configured limit")
    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    return b"".join(
        (
            bytes((_SERVER_PACKET_HEAD, int(cmd))),
            len(payload).to_bytes(4, "little", signed=False),
            payload,
            int(timestamp_ms).to_bytes(8, "little", signed=True),
            bytes((_PACKET_END,)),
        )
    )


class GalaxisPacketDecoder:
    """Incrementally decode packets emitted by ``PackageHandle.Pack``.

    The current Galaxis sender writes a four-byte little-endian payload length,
    followed by the payload, an eight-byte little-endian Unix timestamp in
    milliseconds, and the packet terminator.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> List[Tuple[int, bytes, int]]:
        self._buffer.extend(data)
        packets: List[Tuple[int, bytes, int]] = []

        while True:
            start = self._buffer.find(bytes((_PACKET_HEAD,)))
            if start < 0:
                self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < _MIN_PACKET_SIZE:
                break

            declared = int.from_bytes(self._buffer[2:6], "little", signed=False)
            if declared <= 0 or declared > _MAX_PAYLOAD_SIZE:
                # The current sender never emits empty application packets.
                del self._buffer[0]
                continue

            total = _MIN_PACKET_SIZE + declared
            if len(self._buffer) < total:
                break
            if self._buffer[total - 1] != _PACKET_END:
                del self._buffer[0]
                continue
            packets.append(self._take_packet(total, declared))

        return packets

    def _take_packet(self, total: int, payload_size: int) -> Tuple[int, bytes, int]:
        cmd = self._buffer[1]
        payload_end = _HEADER_SIZE + payload_size
        payload = bytes(self._buffer[_HEADER_SIZE:payload_end])
        timestamp_ms = int.from_bytes(
            self._buffer[payload_end : payload_end + 8], "little", signed=True
        )
        del self._buffer[:total]
        return cmd, payload, timestamp_ms

def _parse_pose(value: Any) -> Optional[Tuple[Dict[str, float], Dict[str, float]]]:
    if not isinstance(value, str):
        return None
    try:
        numbers = [float(item) for item in value.split(",")]
    except (TypeError, ValueError):
        return None
    if len(numbers) != 7 or not all(math.isfinite(item) for item in numbers):
        return None
    return (
        {"x": numbers[0], "y": numbers[1], "z": numbers[2]},
        {"x": numbers[3], "y": numbers[4], "z": numbers[5], "w": numbers[6]},
    )


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _put_pose(raw: Dict[str, Any], prefix: str, pose: Any) -> bool:
    parsed = _parse_pose(pose)
    if parsed is None:
        return False
    raw[f"{prefix}Position"], raw[f"{prefix}Rotation"] = parsed
    return True


def normalize_tracking_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert one Galaxis ``Tracking`` value to the bridge's raw schema."""
    raw: Dict[str, Any] = {
        "deviceTimeNs": int(payload.get("timeStampNs", 0) or 0),
    }

    head = payload.get("Head")
    if isinstance(head, dict):
        _put_pose(raw, "hmd", head.get("pose"))

    controllers = payload.get("Controller")
    if not isinstance(controllers, dict):
        return raw

    for side in ("left", "right"):
        controller = controllers.get(side)
        if not isinstance(controller, dict):
            continue
        pose_ok = _put_pose(raw, f"{side}Controller", controller.get("pose"))
        raw[f"{side}Tracked"] = pose_ok
        raw[f"{side}Valid"] = pose_ok
        raw[f"{side}Joystick"] = {
            "x": _finite_float(controller.get("axisX")),
            "y": _finite_float(controller.get("axisY")),
        }
        raw[f"{side}ThumbstickClick"] = bool(controller.get("axisClick", False))

        trigger = _finite_float(controller.get("trigger"))
        grip = _finite_float(controller.get("grip"))
        raw[f"{side}TriggerValue"] = trigger
        raw[f"{side}GripValue"] = grip
        raw[f"{side}TriggerPressed"] = trigger >= 0.5
        raw[f"{side}GripPressed"] = grip >= 0.5
        raw[f"{side}IndexTriggerTouched"] = trigger > 0.0
        raw[f"{side}ThumbstickTouched"] = False

        primary = bool(controller.get("primaryButton", False))
        secondary = bool(controller.get("secondaryButton", False))
        menu = bool(controller.get("menuButton", False))
        if side == "left":
            raw["buttonXPressed"] = primary
            raw["buttonYPressed"] = secondary
            raw["buttonXTouched"] = primary
            raw["buttonYTouched"] = secondary
            raw["startPressed"] = menu
        else:
            raw["buttonAPressed"] = primary
            raw["buttonBPressed"] = secondary
            raw["buttonATouched"] = primary
            raw["buttonBTouched"] = secondary
            raw["backPressed"] = menu

    return raw


class GalaxisTcpServerTransport:
    """Accept Galaxis headset connections and emit normalized frames."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        on_frame: Callable[[PicoFrame], None],
        logger: Any = None,
        fps_window: int = 30,
        liveness_interval_sec: float = 5.0,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._on_frame = on_frame
        self._log = logger
        self._fps_window = int(fps_window)
        self._liveness_interval_sec = max(float(liveness_interval_sec), 0.5)
        self._lock = threading.Lock()
        self._frame_dispatch_lock = threading.Lock()
        self._listener: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._connection_generation = 0
        self._running = False
        self._connected = False
        self._listen_error: Optional[str] = None
        self._frame_times: List[float] = []
        self._last_frame_mono: Optional[float] = None
        self._pico_id = "galaxis-pico4-ultra"
        self._seq = 0
        self._last_device_time_ns: Optional[int] = None
        self._stale_frame_drops = 0

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def metrics(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            times = tuple(self._frame_times)
            last = self._last_frame_mono
            connected = self._connected
            listening = self._listener is not None
            listen_error = self._listen_error
        fps = 0.0
        if len(times) >= 2 and times[-1] > times[0]:
            fps = (len(times) - 1) / (times[-1] - times[0])
        return {
            "connected": connected,
            "listening": listening,
            "listen_error": listen_error or "",
            "last_frame_age_s": float("nan") if last is None else now - last,
            "fps": fps,
            "offset_ns": 0,
            "rtt_ns": None,
        }

    def start_listener(self) -> None:
        """Start the Galaxis PICO listener once."""
        with self._lock:
            if self._listener is not None:
                return
        listener: Optional[socket.socket] = None
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._host, self._port))
            listener.listen(1)
            listener.settimeout(0.5)
            with self._lock:
                self._listener = listener
                self._running = True
                self._listen_error = None
            threading.Thread(target=self._accept_loop, daemon=True).start()
            self._info(f"Galaxis PICO listener ready on {self._host}:{self._port}")
        except Exception as exc:
            if listener is not None:
                listener.close()
            with self._lock:
                self._listen_error = str(exc)
            self._warn(f"Cannot listen on {self._host}:{self._port}: {exc}")

    def stop(self) -> None:
        with self._frame_dispatch_lock:
            with self._lock:
                self._running = False
                self._connection_generation += 1
                listener, client = self._listener, self._client
                self._listener = None
                self._client = None
                self._connected = False
                self._listen_error = None
        for sock in (client, listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _accept_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running or self._listener is None:
                    return
                listener = self._listener
            try:
                client, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            client.settimeout(0.5)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._frame_dispatch_lock:
                with self._lock:
                    previous = self._client
                    self._connection_generation += 1
                    generation = self._connection_generation
                    self._client = client
                    self._connected = True
                    self._frame_times.clear()
                    self._last_frame_mono = None
                    self._pico_id = "galaxis-pico4-ultra"
                    self._seq = 0
                    self._last_device_time_ns = None
                    self._stale_frame_drops = 0
            if previous is not None:
                self._close_socket(previous)
            self._info(f"Galaxis PICO connected from {address[0]}:{address[1]}")
            threading.Thread(
                target=self._recv_loop, args=(client, generation), daemon=True
            ).start()

    def _recv_loop(self, client: socket.socket, generation: int) -> None:
        decoder = GalaxisPacketDecoder()
        reason = "headset disconnected"
        # Let the Unity sender deliver its first tracking frame before writing
        # to the socket. The deployed APK can block its send path when a server
        # packet arrives immediately after accept(), leaving the bridge in
        # "connected but not streaming".
        next_liveness_at = time.monotonic() + self._liveness_interval_sec
        try:
            while True:
                with self._lock:
                    if not self._is_current_session_locked(client, generation):
                        return
                now = time.monotonic()
                if now >= next_liveness_at:
                    self._send_liveness_probe(client)
                    next_liveness_at = now + self._liveness_interval_sec
                try:
                    data = client.recv(1024 * 1024)
                except socket.timeout:
                    continue
                if not data:
                    break
                with self._lock:
                    if not self._is_current_session_locked(client, generation):
                        return
                for cmd, payload, _timestamp_ms in decoder.feed(data):
                    with self._lock:
                        if not self._is_current_session_locked(client, generation):
                            return
                    try:
                        self._handle_packet(cmd, payload, generation=generation)
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        ValueError,
                    ) as exc:
                        self._warn(f"Ignored malformed Galaxis packet: {exc}")
        except Exception as exc:
            reason = f"headset receive error: {exc}"
        finally:
            with self._lock:
                was_current = self._is_current_session_locked(client, generation)
                if was_current:
                    self._client = None
                    self._connected = False
            self._close_socket(client)
            if was_current:
                self._warn(reason)

    def _is_current_session_locked(
        self, client: socket.socket, generation: int
    ) -> bool:
        return (
            self._running
            and self._client is client
            and self._connection_generation == generation
        )

    def _send_liveness_probe(self, client: socket.socket) -> None:
        """Keep the Unity receive path active and exercise its timeTest reply."""
        payload = json.dumps(
            {
                "functionName": "timeTest",
                "value": str(time.time_ns() // 1_000_000),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        client.sendall(_encode_server_packet(_CMD_SERVER_FUNCTION, payload))

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _handle_packet(
        self, cmd: int, payload: bytes, *, generation: Optional[int] = None
    ) -> None:
        text = payload.decode("utf-8", errors="strict")
        if cmd == _CMD_CONNECT:
            device_id = text.split("|", 1)[0].strip()
            if device_id:
                with self._frame_dispatch_lock:
                    with self._lock:
                        if not self._generation_is_current_locked(generation):
                            return
                        self._pico_id = device_id
            return
        if cmd != _CMD_FUNCTION:
            return
        if text == "timeTest":
            return
        envelope = json.loads(text)
        if envelope.get("functionName") != "Tracking":
            return
        value = envelope.get("value")
        tracking = json.loads(value) if isinstance(value, str) else value
        if not isinstance(tracking, dict):
            return

        raw = normalize_tracking_payload(tracking)
        device_time_ns = int(raw.get("deviceTimeNs", 0) or 0)
        pc_mono_ns = time.monotonic_ns()
        now = pc_mono_ns / 1e9
        with self._frame_dispatch_lock:
            with self._lock:
                if not self._generation_is_current_locked(generation):
                    return
                previous_device_time_ns = self._last_device_time_ns
                if (
                    device_time_ns > 0
                    and previous_device_time_ns is not None
                    and device_time_ns <= previous_device_time_ns
                ):
                    self._stale_frame_drops += 1
                    stale_drop_count = self._stale_frame_drops
                    drop_frame = True
                else:
                    stale_drop_count = 0
                    drop_frame = False
                    if device_time_ns > 0:
                        self._last_device_time_ns = device_time_ns
                    self._seq += 1
                    seq = self._seq
                    pico_id = self._pico_id
                    self._last_frame_mono = now
                    self._frame_times.append(now)
                    if len(self._frame_times) > self._fps_window:
                        self._frame_times.pop(0)
            if drop_frame:
                if stale_drop_count == 1 or stale_drop_count % 100 == 0:
                    self._warn(
                        "Dropped stale Galaxis tracking frame: "
                        f"device_time_ns={device_time_ns} <= "
                        f"{previous_device_time_ns} "
                        f"(drops={stale_drop_count})"
                    )
                return
            delta_time_s = (
                float("nan")
                if previous_device_time_ns is None or device_time_ns <= 0
                else (device_time_ns - previous_device_time_ns) / 1e9
            )
            frame = PicoFrame(
                device_time_ns=device_time_ns,
                pc_monotonic_ns=pc_mono_ns,
                seq=seq,
                pico_id=pico_id,
                delta_time_s=delta_time_s,
                raw=raw,
            )
            self._on_frame(frame)

    def _generation_is_current_locked(self, generation: Optional[int]) -> bool:
        return generation is None or generation == self._connection_generation

    def _info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(message)

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warn(message)
