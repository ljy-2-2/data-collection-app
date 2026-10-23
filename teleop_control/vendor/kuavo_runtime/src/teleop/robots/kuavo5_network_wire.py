"""Pure protocol for the loopback-only offline prototype; no device or socket I/O.

HMAC authenticates messages, not encryption. Each connection needs a fresh
server-generated secrets.token_hex(16) identifier. Objects have one thread owner.
Packet byte limits include the trailing LF. A framing failure ends a connection.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re


MAX_PACKET_BYTES = 32768
WIRE_VERSION = 1
_FIELDS = {'version', 'connection_id', 'direction', 'sequence', 'payload', 'mac'}
_DIRECTIONS = {'client': 'client_to_server', 'server': 'server_to_client'}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      allow_nan=False, ensure_ascii=True).encode('ascii')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('non-finite JSON number: '+value)


class AuthenticatedCodec:
    """Authenticate exact connection, direction and contiguous message sequence."""

    def __init__(self, key: bytes, connection_id: str, role: str):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError('authentication key must contain at least 32 bytes')
        if not isinstance(connection_id, str) or not re.fullmatch('[0-9a-f]{32}', connection_id):
            raise ValueError('connection_id must be a fresh token_hex(16) value')
        if role not in _DIRECTIONS:
            raise ValueError('role must be client or server')
        self._key, self._connection_id = key, connection_id
        self._send_direction = _DIRECTIONS[role]
        self._receive_direction = _DIRECTIONS['server' if role == 'client' else 'client']
        self._sent = self._received = 0

    def pack(self, payload: dict) -> bytes:
        if not isinstance(payload, dict):
            raise ValueError('payload must be an object')
        envelope = dict(version=WIRE_VERSION, connection_id=self._connection_id,
                        direction=self._send_direction, sequence=self._sent+1, payload=payload)
        try:
            envelope['mac'] = hmac.new(self._key, _canonical(envelope), hashlib.sha256).hexdigest()
            line = _canonical(envelope)+b'\n'
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError('payload must be finite JSON') from exc
        if len(line) > MAX_PACKET_BYTES:
            raise ValueError('packet too large')
        self._sent += 1
        return line

    def unpack(self, line: bytes) -> dict:
        if not isinstance(line, bytes) or not line.endswith(b'\n') or b'\n' in line[:-1]:
            raise ValueError('one LF-terminated bytes packet required')
        if len(line) > MAX_PACKET_BYTES:
            raise ValueError('packet too large')
        try:
            envelope = json.loads(line.decode('utf-8'), object_pairs_hook=_unique_object,
                                  parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            raise ValueError('invalid JSON packet') from exc
        if not isinstance(envelope, dict) or set(envelope) != _FIELDS:
            raise ValueError('invalid authenticated envelope')
        if type(envelope['version']) is not int or envelope['version'] != WIRE_VERSION:
            raise ValueError('unsupported wire version')
        if envelope['connection_id'] != self._connection_id:
            raise ValueError('wrong connection')
        if envelope['direction'] != self._receive_direction:
            raise ValueError('wrong direction')
        if type(envelope['sequence']) is not int or envelope['sequence'] != self._received+1:
            raise ValueError('replayed, missing or reordered sequence')
        if not isinstance(envelope['payload'], dict):
            raise ValueError('payload must be an object')
        mac = envelope.pop('mac')
        if not isinstance(mac, str) or not re.fullmatch('[0-9a-f]{64}', mac):
            raise ValueError('invalid authentication tag')
        try:
            expected = hmac.new(self._key, _canonical(envelope), hashlib.sha256).hexdigest()
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError('payload must be finite JSON') from exc
        if not hmac.compare_digest(mac, expected):
            raise ValueError('authentication failed')
        self._received = envelope['sequence']
        return envelope['payload']


class FrameBuffer:
    """Bound each complete frame, including drip-fed partial lines, by size/time."""

    def __init__(self, max_bytes=MAX_PACKET_BYTES, timeout_s=.25):
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_PACKET_BYTES:
            raise ValueError('invalid frame byte limit')
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) \
                or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('finite positive frame timeout required')
        self._max_bytes, self._timeout_s = max_bytes, timeout_s
        self._buffer = bytearray()
        self._started = self._last_now = None
        self._failed = False

    def check_timeout(self, now):
        if self._failed:
            raise ValueError('frame buffer failed; discard connection')
        if isinstance(now, bool) or not isinstance(now, (int, float)) \
                or not math.isfinite(now) or now < 0 \
                or (self._last_now is not None and now < self._last_now):
            self._failed = True
            raise ValueError('finite nondecreasing monotonic time required')
        self._last_now = now
        if self._started is not None and now-self._started >= self._timeout_s:
            self._failed = True
            raise TimeoutError('whole frame deadline expired')

    def feed(self, data: bytes, now: float) -> list[bytes]:
        self.check_timeout(now)
        if not isinstance(data, bytes):
            raise ValueError('frame input must be bytes')
        frames, offset = [], 0
        while offset < len(data):
            if self._started is None:
                self._started = now
            newline = data.find(b'\n', offset)
            end = len(data) if newline < 0 else newline+1
            if len(self._buffer)+end-offset > self._max_bytes:
                self._failed = True
                raise ValueError('frame too large')
            self._buffer.extend(data[offset:end])
            offset = end
            if newline >= 0:
                frames.append(bytes(self._buffer))
                self._buffer.clear()
                self._started = None
        return frames


def validate_loopback(host):
    """Return a canonical numeric IPv4 loopback address without DNS resolution."""
    if not isinstance(host, str):
        raise ValueError('numeric IPv4 loopback address required')
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as exc:
        raise ValueError('numeric IPv4 loopback address required') from exc
    if not address.is_loopback:
        raise ValueError('only IPv4 loopback addresses are allowed')
    return str(address)
