"""Synchronous client for the loopback OFFLINE protocol, not a live XR pump."""
from collections import deque
import json
import select
import socket
import time

from teleop.robots.kuavo5_network_wire import AuthenticatedCodec, FrameBuffer, validate_loopback


class OfflineNetworkClient:
    def __init__(self, address, key):
        host, port = address
        self.address = (validate_loopback(host), port)
        if type(port) is not int or not 0 < port <= 65535:
            raise ValueError('invalid port')
        AuthenticatedCodec(key, '0'*32, 'client')
        self.key = key
        self.sock = self.codec = None
        self.frames = FrameBuffer()
        self.lines = deque()
        self._used = False

    def open(self):
        if self._used:
            raise RuntimeError('client instances are single-use')
        self._used = True
        self.sock = socket.create_connection(self.address, timeout=2.)
        self.sock.setblocking(False)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            hello = json.loads(self._line(time.monotonic()+2.))
            if not isinstance(hello, dict) or set(hello) != {'event', 'version', 'connection_id', 'scope'} or hello['event'] != 'hello' or type(hello['version']) is not int or hello['version'] != 1 or hello['scope'] != 'loopback_offline_only':
                raise ValueError('not an offline network listener')
            self.codec = AuthenticatedCodec(self.key, hello['connection_id'], 'client')
            self.send(dict(event='status'))
            status = self.wait_for('status')
            if status.get('real_backend_available') is not False:
                raise ValueError('offline listener identity mismatch')
            return status
        except BaseException:
            self.close()
            raise

    def _line(self, deadline):
        while not self.lines:
            self.frames.check_timeout(time.monotonic())
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('offline response deadline')
            if not select.select([self.sock], [], [], min(.01, remaining))[0]:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError('offline response deadline')
            try:
                raw = self.sock.recv(16384)
            except BlockingIOError:
                continue
            if not raw:
                raise EOFError('offline connection closed')
            self.lines.extend(self.frames.feed(raw, time.monotonic()))
        if time.monotonic() >= deadline:
            raise TimeoutError('offline response deadline')
        return self.lines.popleft()

    def send(self, packet, *, timeout_s=.2):
        if self.codec is None or self.sock is None:
            raise RuntimeError('offline client is not open')
        deadline = time.monotonic()+timeout_s
        try:
            data = memoryview(self.codec.pack(packet))
            while data:
                remaining = deadline-time.monotonic()
                if remaining <= 0 or not select.select([], [self.sock], [], remaining)[1] or time.monotonic() >= deadline:
                    raise TimeoutError('offline write deadline')
                try:
                    count = self.sock.send(data)
                except BlockingIOError:
                    continue
                if count == 0:
                    raise EOFError('offline write closed')
                data = data[count:]
        except BaseException:
            # A failed or partial frame cannot be followed by another session
            # message: its sequence and framing may already be on the wire.
            self.close()
            raise

    def receive(self, *, timeout_s=2.):
        if self.codec is None:
            raise RuntimeError('offline client is not open')
        try:
            return self.codec.unpack(self._line(time.monotonic()+timeout_s))
        except BaseException:
            self.close()
            raise

    def wait_for(self, *events, timeout_s=2.):
        deadline = time.monotonic()+timeout_s
        while time.monotonic() < deadline:
            packet = self.receive(timeout_s=max(.000001, deadline-time.monotonic()))
            if packet.get('event') in events:
                return packet
        raise TimeoutError('expected offline event missing')

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        self.codec = None
