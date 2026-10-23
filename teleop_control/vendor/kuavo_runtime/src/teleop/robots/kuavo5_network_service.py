"""Loopback-only, authenticated OFFLINE service. No robot backend is registered.

This tests the future session boundary, not IK, robot dynamics, or real stop
behaviour. Listening and status queries cannot start a worker. Demo operations
are in-memory counters, never calls to the live agent. Do not expose this
prototype to a robot LAN: authentication here does not encrypt traffic.
"""
from collections import Counter, deque
import json
import secrets
import selectors
import socket
import time

from teleop.robots.kuavo5_live_protocol import InputLease, INPUT_TTL_NS, StaleInputLease
from teleop.robots.kuavo5_network_wire import AuthenticatedCodec, FrameBuffer, validate_loopback


class DemoSessions:
    """Single-owner fake session model, driven by an independent clock tick.

    Deliberately has no real-worker factory. A real worker will require process
    isolation, verified cleanup, version negotiation and a separate deployment.
    """
    def __init__(self, *, allow_demo=False, clock=time.monotonic_ns):
        if type(allow_demo) is not bool:
            raise ValueError("allow_demo must be bool")
        self.allow_demo = allow_demo
        self._notifications = deque()
        self.clock = clock
        self.owner = None
        self.session_id = None
        self.lease = None
        self.end_ns = None
        self.input_end_ns = None
        self.state = 'idle'
        self.needs_release = True
        self.release_frames = 0
        self.accepted_frames = 0
        self.fake_command_count = 0
        self.lease_expirations = 0
        self.sessions_started = 0
        self.last_close_reason = None

    def status(self, event='status', **extra):
        return dict(event=event, backend='fake' if self.owner else 'none',
                    real_backend_available=False, demo_available=self.allow_demo,
                    physical_robot_command_sent=False, ssh_started=False,
                    owner_present=self.owner is not None, session_id=self.session_id,
                    state=self.state, requires_release=self.needs_release,
                    accepted_frames=self.accepted_frames,
                    fake_command_count=self.fake_command_count,
                    lease_expirations=self.lease_expirations,
                    sessions_started=self.sessions_started,
                    last_close_reason=self.last_close_reason, **extra)

    def _close(self, reason):
        result = self.status('demo_closed', reason=reason)
        result.update(owner_present=False, state='closed', requires_release=True,
                      last_close_reason=reason)
        self.owner = self.session_id = self.lease = None
        self.end_ns = self.input_end_ns = None
        self.state = 'idle'
        self.needs_release = True
        self.release_frames = 0
        self.last_close_reason = reason
        # Nothing external exists in a demo. This is NOT real worker cleanup.
        return result

    def _advance_time(self):
        now = self.clock()
        if self.owner is None:
            return
        if now >= self.end_ns:
            owner = self.owner
            self._notifications.append((owner, self._close('duration_elapsed')))
            return
        if self.input_end_ns is not None and now >= self.input_end_ns:
            self.input_end_ns = None
            self.needs_release = True
            self.release_frames = 0
            self.state = 'waiting_release'
            self.lease_expirations += 1
            self._notifications.append((self.owner, self.status('demo_state', reason='input_expired')))

    def tick(self):
        """Drain notifications, including deadlines crossed inside handle()."""
        self._advance_time()
        events = list(self._notifications)
        self._notifications.clear()
        return events

    def disconnect(self, peer):
        if self.owner == peer:
            self._close('connection_closed')

    def challenge(self, peer):
        self._advance_time()
        if self.owner != peer or self.lease is None:
            return None
        return dict(self.lease.challenge(self.clock()), session_id=self.session_id)

    def handle(self, peer, packet):
        self._advance_time()  # Preserve notices for the next service tick.
        event = packet.get('event')
        if event == 'status' and set(packet) == {'event'}:
            return self.status()
        if event == 'start_demo':
            if set(packet) != {'event', 'duration_s'}:
                raise ValueError('start_demo requires only duration_s')
            if not self.allow_demo:
                return self.status('refused', reason='demo_disabled')
            if self.owner is not None:
                return self.status('refused', reason='session_busy')
            duration = packet['duration_s']
            if type(duration) not in (int, float) or not .05 <= duration <= 300:
                raise ValueError('demo duration must be finite and within 0.05..300 s')
            self.owner = peer
            self.session_id = secrets.token_hex(16)
            self.lease = InputLease()
            self.end_ns = self.clock() + int(duration * 1e9)
            self.input_end_ns = None
            self.state = 'waiting_release'
            self.needs_release = True
            self.release_frames = 0
            self.sessions_started += 1
            return self.status('demo_ready')
        # No request can upload code, instantiate a real robot, configure modes,
        # or invoke the existing SSH entry. Unknown operations always fail closed.
        if event not in ('input', 'close_demo'):
            return self.status('refused', reason='operation_unavailable')
        if self.owner != peer or packet.get('session_id') != self.session_id or self.owner is None:
            return self.status('refused', reason='not_session_owner')
        if event == 'close_demo':
            if set(packet) != {'event', 'session_id'}:
                raise ValueError('invalid close_demo request')
            return self._close('client_closed')
        if set(packet) != {'event', 'session_id', 'packet'}:
            raise ValueError('invalid demo input request')
        try:
            frame = self.lease.accept(packet['packet'], now_ns=self.clock())
        except StaleInputLease:
            return self.status('input_dropped', reason='stale_or_replayed_input')
        if frame is None:
            return self.status('input_dropped', reason='device_frame_repeated')
        self.input_end_ns = frame.received_monotonic_ns + INPUT_TTL_NS
        self.accepted_frames += 1
        released = max(frame.left_controller.grip, frame.right_controller.grip) < .25
        held = max(frame.left_controller.grip, frame.right_controller.grip) >= .55
        if self.needs_release:
            self.release_frames = self.release_frames + 1 if released else 0
            if self.release_frames >= 3:
                self.needs_release = False
                self.state = 'idle'
        elif released:
            self.state = 'idle'
        elif held:
            self.state = 'following_fake'
            self.fake_command_count += 1
        return self.status('input_accepted')


class _Peer:
    def __init__(self, sock, key, now):
        self.sock = sock
        self.id = secrets.token_hex(16)
        self.codec = AuthenticatedCodec(key, self.id, 'server')
        self.frames = FrameBuffer()
        self.out = deque()
        self.out_size = 0
        self.auth_deadline = now + 2.
        self.authenticated = False
        self.next_challenge = now


class OfflineNetworkService:
    """Bounded, nonblocking selector loop. No hidden SSH/SDK/ROS subprocesses.

    At most eight clients; receive, whole-frame, write and handshake work is
    bounded. The expiry tick runs even when a client stops reading/writing.
    All mutable state belongs to the serve thread; stop_event is its only
    cross-thread control. Socket construction is explicitly performed by open().
    """
    def __init__(self, key, *, host='127.0.0.1', port=0, allow_demo=False):
        self.host = validate_loopback(host)
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('invalid port')
        # Validate key before creating any listener.
        AuthenticatedCodec(key, '0'*32, 'server')
        self.key, self.port = key, port
        self.sessions = DemoSessions(allow_demo=allow_demo)
        self.selector = None
        self.listener = None
        self.peers = {}
        self.disconnections = Counter()
        self._used = False

    @property
    def address(self):
        if self.listener is None:
            raise RuntimeError('listener is not open')
        return self.listener.getsockname()

    def open(self):
        if self._used:
            raise RuntimeError('service instances are single-use')
        self._used = True
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((self.host, self.port))
            listener.listen(8)
            listener.setblocking(False)
            self.selector = selectors.DefaultSelector()
            self.selector.register(listener, selectors.EVENT_READ, None)
            self.listener = listener
        except BaseException:
            listener.close()
            if self.selector is not None:
                self.selector.close()
            raise
        return self.address

    def _drop(self, peer, reason):
        if self.peers.pop(peer.id, None) is None:
            return
        self.sessions.disconnect(peer.id)
        self.disconnections[reason] += 1
        try:
            self.selector.unregister(peer.sock)
        finally:
            peer.sock.close()

    def _queue(self, peer, packet, *, plain=False):
        if peer.id not in self.peers:
            return
        data = ((json.dumps(packet, allow_nan=False)+'\n').encode()
                if plain else peer.codec.pack(packet))
        if peer.out_size + len(data) > 65536 or len(peer.out) >= 64:
            self._drop(peer, 'outbound_backpressure')
            return
        peer.out.append((memoryview(data), time.monotonic()+.25))
        peer.out_size += len(data)
        self.selector.modify(peer.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, peer)

    def _tick(self):
        for owner, packet in self.sessions.tick():
            peer = self.peers.get(owner)
            if peer is not None:
                self._queue(peer, packet)
        now = time.monotonic()
        for peer in list(self.peers.values()):
            try:
                peer.frames.check_timeout(now)
                if (not peer.authenticated and now >= peer.auth_deadline) or (peer.out and now >= peer.out[0][1]):
                    raise TimeoutError('handshake or write deadline')
                if self.sessions.owner == peer.id and now >= peer.next_challenge:
                    packet = self.sessions.challenge(peer.id)
                    peer.next_challenge = now + .02
                    if packet is not None:
                        self._queue(peer, packet)
            except (ValueError, TimeoutError):
                self._drop(peer, 'deadline')

    def _accept(self):
        for _ in range(4):
            try:
                sock, _ = self.listener.accept()
            except BlockingIOError:
                return
            if len(self.peers) >= 8:
                sock.close()
                continue
            sock.setblocking(False)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer = _Peer(sock, self.key, time.monotonic())
            self.peers[peer.id] = peer
            self.selector.register(sock, selectors.EVENT_READ, peer)
            self._queue(peer, dict(event='hello', version=1, connection_id=peer.id,
                                   scope='loopback_offline_only'), plain=True)

    def _read(self, peer):
        try:
            raw = peer.sock.recv(16384)
        except BlockingIOError:
            return
        if not raw:
            self._drop(peer, 'eof')
            return
        lines = peer.frames.feed(raw, time.monotonic())
        if len(lines) > 16:
            raise ValueError('request burst exceeds per-tick budget')
        for line in lines:
            # MAC/version/direction/connection/sequence before touching leases.
            packet = peer.codec.unpack(line)
            if not peer.authenticated and time.monotonic() >= peer.auth_deadline:
                self._drop(peer, 'handshake_deadline')
                return
            peer.authenticated = True
            self._tick()
            if peer.id not in self.peers:
                return
            result = self.sessions.handle(peer.id, packet)
            self._queue(peer, result)
            self._tick()
            if peer.id not in self.peers:
                return

    def _write(self, peer):
        if not peer.out:
            return
        data, deadline = peer.out[0]
        if time.monotonic() >= deadline:
            self._drop(peer, 'write_deadline')
            return
        try:
            count = peer.sock.send(data)
        except BlockingIOError:
            return
        if count == 0:
            self._drop(peer, 'write_eof')
            return
        peer.out_size -= count
        if count < len(data):
            peer.out[0] = (data[count:], deadline)
        else:
            peer.out.popleft()
        if not peer.out:
            self.selector.modify(peer.sock, selectors.EVENT_READ, peer)

    def serve(self, stop_event):
        if self.listener is None:
            raise RuntimeError('call open before serve')
        try:
            while not stop_event.is_set():
                self._tick()
                for key, events in self.selector.select(.01):
                    self._tick()
                    peer = key.data
                    if peer is None:
                        self._accept()
                        continue
                    if peer.id not in self.peers:
                        continue
                    try:
                        if events & selectors.EVENT_READ:
                            self._read(peer)
                        if peer.id in self.peers and events & selectors.EVENT_WRITE:
                            self._write(peer)
                    except (ValueError, TypeError, ArithmeticError, OSError, TimeoutError, RecursionError):
                        self._drop(peer, 'invalid_or_closed_connection')
        finally:
            self.close()

    def close(self):
        for peer in list(self.peers.values()):
            self._drop(peer, 'service_closed')
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.selector is not None:
            self.selector.close()
            self.selector = None
