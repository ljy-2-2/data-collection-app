"""Optional latest-input channel. Importing this module creates no sockets.

SSH delivers a fresh session key and endpoint. HMAC authenticates bounded UDP
packets; monotonic packet counters discard replay/reorder, while InputLease
remains the authority for source freshness, tracking and the 100ms deadline.
"""
import hashlib
import hmac
import json
import socket
import struct
import zlib
import time

MAX_DATAGRAM = 1200
MAX_PLAIN = 8192
HEADER = b'KBASE1'


class DatagramChannel:
    def __init__(self, sock, key, *, role, peer=None, clock=time.monotonic_ns):
        if role not in ('host', 'robot') or not isinstance(key, str) or len(key) != 64:
            raise ValueError('invalid datagram session configuration')
        try: secret = bytes.fromhex(key)
        except ValueError: raise ValueError('invalid datagram session key')
        if len(secret) != 32: raise ValueError('invalid datagram session key')
        self.clock = clock
        self.sock = sock
        self.sock.setblocking(False)
        self.role = role
        self.peer = peer
        self.send_key = hmac.digest(secret, role.encode(), 'sha256')
        self.receive_key = hmac.digest(secret, ('robot' if role == 'host' else 'host').encode(), 'sha256')
        self.sent_counter = 0
        self.received_counter = 0
        self.stats = dict(sent=0, received=0, rejected=0, send_dropped=0,
                          coalesced=0, counter_gaps=0, last_send_ns=None,
                          last_receive_ns=None, last_poll_ns=None,
                          max_send_gap_ms=0., max_receive_gap_ms=0., max_poll_gap_ms=0.,
                          last_receive_challenge_id=None)

    def _stamp(self, name):
        now = self.clock()
        previous = self.stats['last_'+name+'_ns']
        if previous is not None:
            key = 'max_'+name+'_gap_ms'
            self.stats[key] = max(self.stats[key], (now-previous)/1e6)
        self.stats['last_'+name+'_ns'] = now

    def send(self, packet):
        allowed = ('hello', 'xr') if self.role == 'host' else ('challenge',)
        if packet.get('event') not in allowed: raise ValueError('wrong datagram direction/event')
        if self.peer is None: return False
        plain = json.dumps(packet, allow_nan=False, separators=(',', ':')).encode()
        if len(plain) > MAX_PLAIN: raise ValueError('datagram input too large')
        compressed = zlib.compress(plain, 1)
        self.sent_counter += 1
        body = HEADER + struct.pack('!Q', self.sent_counter) + compressed
        data = body + hmac.digest(self.send_key, body, 'sha256')
        if len(data) > MAX_DATAGRAM: raise ValueError('datagram exceeds path-safe payload bound')
        try: self.sock.sendto(data, self.peer)
        except (BlockingIOError, InterruptedError):
            self.stats['send_dropped'] += 1
            return False
        self._stamp('send')
        self.stats['sent'] += 1
        return True

    def poll(self):
        self._stamp('poll')
        latest = None
        # Bound work even in a packet flood; do not starve the watchdog.
        for _ in range(32):
            try: data, address = self.sock.recvfrom(MAX_DATAGRAM+1)
            except (BlockingIOError, InterruptedError): break
            try:
                if len(data) > MAX_DATAGRAM or len(data) < len(HEADER)+8+32 or not data.startswith(HEADER):
                    raise ValueError('invalid datagram size/header')
                if self.peer is not None and address != self.peer: raise ValueError('wrong peer')
                body, signature = data[:-32], data[-32:]
                if not hmac.compare_digest(signature, hmac.digest(self.receive_key, body, 'sha256')):
                    raise ValueError('wrong session/authentication')
                counter = struct.unpack('!Q', body[len(HEADER):len(HEADER)+8])[0]
                if counter <= self.received_counter: raise ValueError('old datagram')
                inflater = zlib.decompressobj()
                raw = inflater.decompress(body[len(HEADER)+8:], MAX_PLAIN+1)
                if len(raw)>MAX_PLAIN or not inflater.eof or inflater.unused_data:
                    raise ValueError('invalid compressed input')
                packet = json.loads(raw)
                allowed = ('hello', 'xr') if self.role == 'robot' else ('challenge',)
                if not isinstance(packet, dict) or packet.get('event') not in allowed:
                    raise ValueError('wrong event')
                if self.peer is None:
                    if packet != {'event':'hello'}: raise ValueError('hello required')
                    self.peer = address
                # Counter gaps include drops AND skipped/reordered datagrams;
                # these application timestamps are not kernel/network latency.
                self.stats['counter_gaps'] += counter-self.received_counter-1
                self.received_counter = counter
                self._stamp('receive')
                self.stats['last_receive_challenge_id'] = packet.get('challenge_id')
                self.stats['received'] += 1
                if packet.get('event') != 'hello':
                    if latest is not None: self.stats['coalesced'] += 1
                    latest = packet
            except (ValueError, UnicodeError, zlib.error, struct.error):
                self.stats['rejected'] += 1
        return latest

    def close(self): self.sock.close()


def robot_channel(key, *, bind_address='192.168.26.1'):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((bind_address, 0))
        return DatagramChannel(sock, key, role='robot')
    except BaseException:
        sock.close()
        raise
