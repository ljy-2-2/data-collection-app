"""Passive direct-session supervisor. No ROS import or motion on listen/status.

Only a fixed installed worker can execute; the listener never accepts Python,
shell commands, model paths or arbitrary profiles over the network. Parent is
Python 3.8 compatible. Worker process owns ROS and original input watchdog.
"""
from __future__ import annotations
from collections import Counter, deque
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import selectors
import socket
import ssl
import stat
import threading
import time

from teleop.robots.kuavo5_network_wire import AuthenticatedCodec, FrameBuffer
from teleop.robots.kuavo5_network_config import validate_numeric_host
from teleop.robots.kuavo5_session_worker import FIXED_PROFILE
from teleop.robots.kuavo5_session_profile import validate_profile
from teleop.robots.kuavo5_service_contract import (MAINTENANCE_PROTOCOL, SESSION_PROTOCOL,
                                                 bind_service_contract)

SCOPE = 'kuavo_live_session_v1'
MANAGEMENT_PROTOCOL = 'kuavo_service_requests_v1'
DIAGNOSTICS = frozenset(('diagnostic_zlib', 'ik_trace', 'ik_failure', 'control_trace',
                        'input_drop', 'guard_trace', 'reference_not_sent', 'gripper_trace'))


class OwnerLedger:
    """Exclusive local installation lock plus durable uncertain-session marker.

    A crash cannot erase a running/uncertain worker's ownership. A dirty file
    refuses further starts, even after restart; no automatic reset/kill exists.
    Keep one fixed path per robot installation, independent of listening port.
    """
    def __init__(self, path):
        self.path, self.fd, self.state = Path(path), None, None

    def open(self):
        if self.fd is not None:
            raise RuntimeError('ledger already open')
        if not self.path.is_absolute():
            raise ValueError('absolute owner ledger path required')
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(str(self.path), flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(str(self.path), flags)
            created = False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('owner ledger requires a private regular file owned by this user')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data = os.read(fd, 8193)
            if len(data) > 8192:
                raise ValueError('owner ledger too large')
            # Only exclusive creation proves an empty file is new. Existing
            # empty/truncated state is uncertain, never silently reset to idle.
            state = dict(active=False) if created else json.loads(data)
            if not isinstance(state, dict) or type(state.get('active')) is not bool:
                raise ValueError('owner ledger invalid; manual inspection required')
            self.fd, self.state = fd, state
            if created:
                self._write(state)
        except BaseException:
            self.fd = self.state = None
            os.close(fd)
            raise
        return self

    @property
    def blocked(self):
        return self.fd is None or self.state['active']

    def _write(self, value):
        if self.fd is None:
            raise RuntimeError('owner ledger closed')
        data = (json.dumps(value, allow_nan=False)+'\n').encode()
        os.lseek(self.fd, 0, os.SEEK_SET)
        count = os.write(self.fd, data)
        if count != len(data):
            raise OSError('incomplete owner ledger write')
        os.ftruncate(self.fd, len(data))
        os.fsync(self.fd)
        self.state = value

    def reserve(self, session):
        if self.blocked:
            raise RuntimeError('previous session cleanup is unconfirmed')
        self._write(dict(active=True, session_id=session, supervisor_pid=os.getpid()))

    def release(self, session):
        if self.state.get('session_id') != session or self.state.get('active') is not True:
            raise RuntimeError('owner ledger identity mismatch')
        self._write(dict(active=False, last_clean_session_id=session))

    def reserve_preparation(self, request_id, action, installation_sha256):
        if self.blocked:
            raise RuntimeError('previous operation cleanup is unconfirmed')
        self._write(dict(active=True, operation='preparation', request_id=request_id,
            action=action, installation_sha256=installation_sha256, supervisor_pid=os.getpid()))

    def release_preparation(self, request_id):
        if (self.state.get('operation') != 'preparation' or self.state.get('request_id') != request_id
                or self.state.get('active') is not True):
            raise RuntimeError('preparation owner ledger identity mismatch')
        self._write(dict(active=False, last_clean_preparation_request_id=request_id))

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class SessionSupervisor:
    """Nonblocking lifecycle; old owner remains until report AND process exit.

    Input/challenges are forwarded verbatim. No proxy lease, modified source
    timestamp, or demo Grip policy is used: the original core decides expiry.
    """
    def __init__(self, bundle_sha256, *, worker_factory=None, backend='none',
                 ledger=None, clock=time.monotonic, controller_admin=None, profile=None,
                 installation_sha256=None, session_contract=None, start_in_maintenance=False,
                 preparation_admin=None):
        if backend not in ('none', 'fake', 'robot'):
            raise ValueError('unknown session backend')
        if type(start_in_maintenance) is not bool:
            raise ValueError('explicit startup maintenance boolean required')
        if backend != 'none' and (worker_factory is None or ledger is None):
            raise ValueError('execution requires fixed worker factory and owner ledger')
        if installation_sha256 is not None and (not isinstance(installation_sha256, str)
                or re.fullmatch('[a-f0-9]{64}', installation_sha256) is None):
            raise ValueError('installation identity must be a lowercase SHA256 digest')
        self.bundle_sha256, self.backend = bundle_sha256, backend
        self.installation_sha256 = installation_sha256
        self.service_instance_id = secrets.token_hex(16)
        self.profile = validate_profile(FIXED_PROFILE if profile is None else profile)
        self.session_contract = (None if session_contract is None else bind_service_contract(
            session_contract, service_instance_id=self.service_instance_id,
            installation_sha256=installation_sha256, bundle_sha256=bundle_sha256))
        if self.session_contract is not None and self.session_contract['profile'] != self.profile:
            raise ValueError('contract and installed profile differ')
        self.factory, self.ledger, self.clock = worker_factory, ledger, clock
        self.active = None
        self.blocked_reason = None
        self.shutting_down = False
        self.controller_admin = controller_admin
        if controller_admin is not None and preparation_admin is not None:
            raise ValueError('legacy and r8 preparation administration cannot coexist')
        self.preparation_admin = preparation_admin
        if preparation_admin is not None:
            preparation_admin.bind_ledger(ledger)
        self.last_session = None
        self.maintenance = start_in_maintenance

    @property
    def owner(self):
        return None if self.active is None else self.active['owner']

    def status(self):
        blocked = (self.blocked_reason or
                   (self.preparation_admin.blocked_reason if self.preparation_admin else None) or
                   (('previous_preparation_unconfirmed' if self.ledger.state.get('operation') == 'preparation'
                     else 'previous_cleanup_unconfirmed')
                    if self.ledger and self.ledger.blocked and self.active is None else None))
        result = dict(event='service_status', scope=SCOPE, backend=self.backend,
                    bundle_sha256=self.bundle_sha256, profile=dict(self.profile),
                    installation_sha256=self.installation_sha256,
                    service_instance_id=self.service_instance_id,
                    management_protocol=MANAGEMENT_PROTOCOL,
                    maintenance_protocol=MAINTENANCE_PROTOCOL, maintenance=self.maintenance,
                    session_contract=(None if self.session_contract is None else
                                      json.loads(json.dumps(self.session_contract))),
                    last_session=None if self.last_session is None else dict(self.last_session),
                    ready_to_start=self.backend != 'none' and self.active is None and not blocked and not self.shutting_down and not self.maintenance and not (self.controller_admin and self.controller_admin.busy) and not (self.preparation_admin and self.preparation_admin.busy),
                    readiness_scope='session_admission_only_not_hardware',
                    controller_admin_enabled=self.controller_admin is not None,
                    session_duration_policy='zero_is_unbounded_v1',
                    owner_present=self.active is not None, blocked_reason=blocked,
                    phase=self.active['phase'] if self.active else ('maintenance' if self.maintenance else 'idle'),
                    session_id=self.active['id'] if self.active else None)
        if self.preparation_admin is not None:
            from .kuavo5_controller_admin import PREPARATION_PROTOCOL, PREPARATION_ACTIONS
            result.update(robot_preparation_protocol=PREPARATION_PROTOCOL,
                          robot_preparation_actions=list(PREPARATION_ACTIONS),
                          robot_preparation_busy=self.preparation_admin.busy)
        if self.ledger and self.ledger.state.get('active') and self.ledger.state.get('operation') == 'preparation':
            result['unconfirmed_preparation'] = {name: self.ledger.state[name]
                for name in ('request_id', 'action', 'installation_sha256')}
        return result

    def _record_cleanup(self, state):
        """Call only after the durable owner release has succeeded."""
        closed = state['closed']
        code = closed.get('exit_code') if isinstance(closed, dict) else None
        self.last_session = dict(session_id=state['id'],
                                 software_cleanup_confirmed=True,
                                 physical_stop_verified=False,
                                 exit_code=code if type(code) is int else None)

    def _stop_session(self, packet):
        """Authenticated management request; never transfers input ownership.

        A caller pins the observed service, installation and exact session. A
        late request cannot stop a replacement session or silently target an
        upgraded service. Acceptance is not evidence that cleanup completed.
        """
        fields = {'event', 'session_id', 'expected_service_instance_id',
                  'expected_installation_sha256', 'expected_bundle_sha256',
                  'expected_profile', 'confirm_stop'}
        if set(packet) != fields:
            return dict(event='refused', reason='invalid_stop_fields')
        if packet['confirm_stop'] is not True:
            return dict(event='refused', reason='explicit_stop_confirmation_required')
        if self.installation_sha256 is None:
            return dict(event='refused', reason='installation_identity_unavailable')
        for name, length in (('session_id', 32), ('expected_service_instance_id', 32),
                             ('expected_installation_sha256', 64), ('expected_bundle_sha256', 64)):
            if not isinstance(packet[name], str) or re.fullmatch('[a-f0-9]{'+str(length)+'}', packet[name]) is None:
                return dict(event='refused', reason='invalid_stop_identity')
        if (packet['expected_service_instance_id'] != self.service_instance_id
                or packet['expected_installation_sha256'] != self.installation_sha256
                or packet['expected_bundle_sha256'] != self.bundle_sha256
                or packet['expected_profile'] != self.profile):
            return dict(event='refused', reason='stop_service_identity_mismatch')
        if self.active is None or packet['session_id'] != self.active['id']:
            return dict(event='refused', reason='stop_session_identity_mismatch')
        self._begin_close('operator_stop_requested')
        return dict(event='session_stop_requested', session_id=self.active['id'],
                    service_instance_id=self.service_instance_id,
                    installation_sha256=self.installation_sha256, phase='closing',
                    software_cleanup_confirmed=False, physical_stop_verified=False)

    def _begin_close(self, reason):
        state = self.active
        if state is None:
            return
        state['phase'] = 'closing'
        if state['close_reason'] is None:
            state['close_reason'] = reason
        if state['started'].is_set() and not state['close_requested'] and state['worker'] is not None:
            state['close_requested'] = True
            state['worker'].request_close()

    def _maintenance_request(self, packet):
        """One event-loop operation: gate future starts without touching a worker.

        This gate survives request-client disconnect. It is cleared only by an
        explicit matching request or replacement of the entire idle service.
        """
        fields = {'event', 'expected_service_instance_id', 'expected_installation_sha256', 'confirm'}
        if set(packet) != fields or packet.get('confirm') is not True:
            return dict(event='refused', reason='invalid_maintenance_request')
        if (self.installation_sha256 is None
                or packet['expected_service_instance_id'] != self.service_instance_id
                or packet['expected_installation_sha256'] != self.installation_sha256):
            return dict(event='refused', reason='maintenance_service_identity_mismatch')
        if (self.active is not None or self.blocked_reason or self.shutting_down
                or (self.ledger and self.ledger.blocked)
                or (self.controller_admin and self.controller_admin.busy)
                or (self.preparation_admin and (self.preparation_admin.busy or self.preparation_admin.blocked_reason))):
            return dict(event='refused', reason='maintenance_requires_clean_idle')
        self.maintenance = packet['event'] == 'enter_maintenance'
        return dict(event='maintenance_entered' if self.maintenance else 'maintenance_left',
            service_instance_id=self.service_instance_id,
            installation_sha256=self.installation_sha256, maintenance=self.maintenance,
            owner_present=False, phase='maintenance' if self.maintenance else 'idle',
            physical_robot_command_sent=False)

    def disconnect(self, owner):
        if self.controller_admin:
            self.controller_admin.disconnect(owner)
        if self.preparation_admin:
            self.preparation_admin.disconnect(owner)
        if self.owner == owner:
            self._begin_close('connection_closed')

    def shutdown(self):
        self.shutting_down = True
        if self.controller_admin:
            self.controller_admin.shutdown()
        if self.preparation_admin:
            self.preparation_admin.shutdown()
        self._begin_close('service_closed')

    def handle(self, owner, packet):
        if self.preparation_admin is not None:
            self.preparation_admin.settle()
        event = packet.get('event')
        if event == 'status' and set(packet) == {'event'}:
            return self.status()
        if event == 'stop_session':
            return self._stop_session(packet)
        if event in ('enter_maintenance', 'leave_maintenance'):
            return self._maintenance_request(packet)
        if event in ('preparation_request', 'preparation_job'):
            if self.preparation_admin is None:
                return dict(event='refused', reason='robot_preparation_not_installed_or_disabled')
            return self.preparation_admin.handle(owner, packet,
                session_available=self.active is None and not self.shutting_down and not self.maintenance
                and not self.blocked_reason and not (self.ledger and self.ledger.blocked),
                service_instance_id=self.service_instance_id, installation_sha256=self.installation_sha256)
        if event in ('controller_request', 'controller_job'):
            if self.controller_admin is None:
                return dict(event='refused', reason='controller_admin_not_installed_or_disabled')
            return self.controller_admin.handle(owner, packet,
                session_available=self.active is None and not self.shutting_down and not self.maintenance
                and not self.blocked_reason and not (self.ledger and self.ledger.blocked))
        negotiated = event == 'start_session_contract'
        if negotiated:
            extra = {'expected_service_instance_id', 'expected_installation_sha256',
                     'session_protocol', 'contract_sha256'}
            if self.session_contract is None or not extra <= set(packet):
                return dict(event='refused', reason='session_contract_unavailable')
            if (packet['expected_service_instance_id'] != self.service_instance_id
                    or packet['expected_installation_sha256'] != self.installation_sha256
                    or packet['session_protocol'] != SESSION_PROTOCOL
                    or packet['contract_sha256'] != self.session_contract['contract_sha256']):
                return dict(event='refused', reason='session_contract_identity_mismatch')
            packet = {k: v for k, v in packet.items() if k not in extra}
            packet['event'] = event = 'start_session'
        if event == 'start_session':
            fields = {'event', 'bundle_sha256', 'profile', 'duration_s', 'motion_enabled', 'configure_vendor_profile', 'expected_backend'}
            if self.profile['gripper_mode'] == 'damiao':
                fields.add('allow_gripper_control')
                if packet.get('allow_gripper_control') is not True:
                    return dict(event='refused', reason='explicit_gripper_permission_required')
            if set(packet) != fields:
                return dict(event='refused', reason='invalid_start_fields')
            if not self.status()['ready_to_start']:
                return dict(event='refused', reason=self.status()['blocked_reason'] or 'session_busy_or_disabled')
            duration = packet['duration_s']
            if (packet['motion_enabled'] is not True or packet['configure_vendor_profile'] is not True
                or packet['expected_backend'] != self.backend or packet['profile'] != self.profile
                or packet['bundle_sha256'] != self.bundle_sha256
                or type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
                return dict(event='refused', reason='installation_profile_or_permission_mismatch')
            sid = secrets.token_hex(16)
            self.ledger.reserve(sid)  # durable reservation precedes any worker action
            state = dict(id=sid, owner=owner, phase='starting', started=threading.Event(),
                         worker=None, startup_error=None, close_reason=None, close_requested=False,
                         deadline=self.clock()+90., duration=duration, closed=None, terminal_sent=False)
            self.active = state
            def start():
                try:
                    state['worker'] = self.factory()
                    if state['close_reason'] is not None:
                        state['close_requested'] = True
                        state['worker'].request_close()
                    state['worker'].start()
                except Exception as exc:
                    state['startup_error'] = type(exc).__name__+': '+str(exc)
                finally:
                    state['started'].set()
            # Non-daemon: shutdown cannot abandon a start that may create a child.
            thread = threading.Thread(target=start, name='kuavo-session-start', daemon=False)
            state['thread'] = thread
            try:
                thread.start()
            except Exception as exc:
                state['startup_error'] = type(exc).__name__+': '+str(exc)
                state['started'].set()
            reply = dict(event='session_started', session_id=sid, backend=self.backend)
            if negotiated:
                reply.update(service_instance_id=self.service_instance_id,
                    installation_sha256=self.installation_sha256,
                    contract_sha256=self.session_contract['contract_sha256'])
            return reply
        state = self.active
        if event not in ('input', 'close_session'):
            return dict(event='refused', reason='operation_unavailable')
        if state is None or state['owner'] != owner or packet.get('session_id') != state['id']:
            return dict(event='refused', reason='not_session_owner')
        if event == 'close_session':
            if set(packet) != {'event', 'session_id'}:
                raise ValueError('invalid close request')
            self._begin_close('client_closed')
            return None
        if set(packet) != {'event', 'session_id', 'packet'}:
            raise ValueError('invalid input request')
        value = packet['packet']
        if not isinstance(value, dict) or set(value) != {'event', 'challenge_id', 'message'} or value['event'] != 'xr':
            raise ValueError('only original XR replies may reach the worker')
        if state['phase'] != 'running' or self.clock() >= state['deadline']:
            return None
        # This is a pipe-write deadline, never a replacement for the core's
        # existing challenge/source TTL. Transit time always consumes that TTL.
        try:
            state['worker'].send_input(value, absolute_deadline=min(self.clock()+.05, state['deadline']))
        except (OSError, RuntimeError, BufferError, ValueError) as exc:
            # A worker can exit between the last tick and this write. Keep the
            # listener alive; cleanup evidence, not this send failure, decides
            # whether ownership may be released. No input is retried.
            self._begin_close('input_forward_failed')
            return dict(event='refused', reason='input_forward_failed: '+type(exc).__name__+': '+str(exc))
        return None

    def tick(self):
        if self.preparation_admin is not None:
            self.preparation_admin.settle()
        state = self.active
        if state is None:
            return []
        outgoing = []
        if self.clock() >= state['deadline']:
            self._begin_close('duration_or_preparation_deadline')
        if not state['started'].is_set():
            return outgoing
        if state['phase'] == 'closing':
            self._begin_close(state['close_reason'])
        worker = state['worker']
        if worker is not None and getattr(worker, 'never_spawned', False) is True:
            # The start thread is finished and the worker proves Popen was never
            # attempted. Cancellation/local validation failure cannot leave a
            # robot process behind. process=None alone would not prove this.
            try:
                self.ledger.release(state['id'])
            except Exception as exc:
                self.blocked_reason = 'owner_ledger_commit_failed: '+str(exc)
            else:
                self._record_cleanup(state)
                outgoing.append((state['owner'], dict(event='refused',
                    reason='session_not_started: '+str(state['startup_error'] or state['close_reason'] or 'cancelled'))))
                self.active = None
                return outgoing
        if state['startup_error'] is not None:
            self.blocked_reason = 'worker_start_failed_cleanup_unconfirmed'
            self._begin_close(self.blocked_reason)
        if worker is not None:
            try:
                for packet in worker.poll_events():
                    if packet.get('event') == 'closed':
                        state['closed'] = packet
                        self._begin_close('worker_closed')
                        continue
                    if packet.get('event') == 'ready':
                        if state['phase'] != 'starting':
                            continue
                        state['phase'] = 'running'
                        state['deadline'] = (float('inf') if state['duration'] == 0
                                             else self.clock()+state['duration'])
                    if state['phase'] == 'closing' and packet.get('event') in ('ready', 'challenge'):
                        continue
                    outgoing.append((state['owner'], dict(event='robot_packet', session_id=state['id'], packet=packet)))
                if worker.cleanup_error:
                    self.blocked_reason = 'worker_cleanup_unconfirmed: '+worker.cleanup_error
                    self._begin_close(self.blocked_reason)
                if worker.cleanup_confirmed and state['startup_error'] is None and not self.blocked_reason:
                    # File commit before new ownership; failure keeps us blocked.
                    try:
                        self.ledger.release(state['id'])
                    except Exception as exc:
                        self.blocked_reason = 'owner_ledger_commit_failed: '+str(exc)
                    else:
                        self._record_cleanup(state)
                        if state['closed'] is not None:
                            outgoing.append((state['owner'], dict(event='robot_packet', session_id=state['id'], packet=state['closed'])))
                        self.active = None
                        return outgoing
                elif worker.poll() is not None and worker.output_drained:
                    self.blocked_reason = self.blocked_reason or 'worker_exit_without_confirmed_cleanup'
                    self._begin_close(self.blocked_reason)
            except Exception as exc:
                self.blocked_reason = 'worker_io_failure: '+str(exc)
                self._begin_close(self.blocked_reason)
        if self.blocked_reason and not state['terminal_sent']:
            state['terminal_sent'] = True
            outgoing.append((state['owner'], dict(event='refused', reason=self.blocked_reason)))
        return outgoing


class _Peer:
    def __init__(self, sock, key, now, *, tls=False):
        self.sock, self.id = sock, secrets.token_hex(16)
        self.codec = AuthenticatedCodec(key, self.id, 'server')
        self.frames = FrameBuffer()
        self.out, self.out_size = deque(), 0
        self.authenticated, self.auth_deadline = False, now+3.
        self.tls_ready, self.hello_sent = not tls, False
        self.recv_wants_write = self.send_wants_read = False


class SessionNetworkService:
    """Network pump independent of worker ROS; non-loopback requires TLS."""
    def __init__(self, key, supervisor, *, host='127.0.0.1', port=0, tls_context=None):
        self.host = validate_numeric_host(host)
        if not ipaddress.IPv4Address(self.host).is_loopback and tls_context is None:
            raise ValueError('non-loopback listener requires TLS')
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('invalid listen port')
        AuthenticatedCodec(key, '0'*32, 'server')
        self.key, self.port, self.tls_context = key, port, tls_context
        self.sessions = supervisor
        self.listener = self.selector = None
        self.peers = {}
        self.errors = Counter()
        self.diagnostics_dropped = 0
        self._used = False
        self._closed = False

    @property
    def address(self):
        if self.listener is None:
            raise RuntimeError('listener is not open')
        return self.listener.getsockname()

    def open(self):
        if self._used:
            raise RuntimeError('listener instances are single-use')
        self._used = True
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.port))
            listener.listen(8)
            listener.setblocking(False)
            self.selector = selectors.DefaultSelector()
            self.selector.register(listener, selectors.EVENT_READ, None)
            self.listener = listener
        except BaseException:
            listener.close()
            if self.selector:
                self.selector.close()
            raise
        return self.address

    def _drop(self, peer, reason):
        if self.peers.pop(peer.id, None) is None:
            return
        self.sessions.disconnect(peer.id)
        self.errors[reason] += 1
        try:
            self.selector.unregister(peer.sock)
        except (KeyError, ValueError, OSError):
            pass
        finally:
            peer.sock.close()

    def _interest(self, peer):
        events = selectors.EVENT_READ
        if peer.recv_wants_write or (peer.out and not peer.send_wants_read):
            events |= selectors.EVENT_WRITE
        self.selector.modify(peer.sock, events, peer)

    def _queue(self, peer, packet, *, plain=False):
        if packet is None or peer.id not in self.peers:
            return
        diagnostic = packet.get('event') == 'robot_packet' and packet['packet'].get('event') in DIAGNOSTICS
        if diagnostic and (len(peer.out) >= 8 or peer.out_size >= 16384):
            self.diagnostics_dropped += 1
            return
        try:
            data = (json.dumps(packet, allow_nan=False)+'\n').encode() if plain else peer.codec.pack(packet)
        except (ValueError, RecursionError):
            if diagnostic:
                self.diagnostics_dropped += 1
                return
            self._drop(peer, 'packet_encode_failed')
            return
        if peer.out_size+len(data) > 131072 or len(peer.out) >= 64:
            self._drop(peer, 'network_backpressure')
            return
        peer.out.append((memoryview(data), time.monotonic()+.2))
        peer.out_size += len(data)
        self._interest(peer)

    def _hello(self, peer):
        if not peer.hello_sent:
            peer.hello_sent = True
            self._queue(peer, dict(event='hello', version=1, connection_id=peer.id, scope=SCOPE), plain=True)

    def _handshake(self, peer):
        if peer.tls_ready:
            return True
        try:
            peer.sock.do_handshake()
        except ssl.SSLWantReadError:
            self.selector.modify(peer.sock, selectors.EVENT_READ, peer)
            return False
        except ssl.SSLWantWriteError:
            self.selector.modify(peer.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, peer)
            return False
        peer.tls_ready = True
        self._hello(peer)
        return True

    def _accept(self):
        for _ in range(4):
            try:
                sock, _ = self.listener.accept()
            except BlockingIOError:
                return
            if len(self.peers) >= 8:
                sock.close()
                continue
            peer = None
            try:
                sock.setblocking(False)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if self.tls_context is not None:
                    sock = self.tls_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
                peer = _Peer(sock, self.key, time.monotonic(), tls=self.tls_context is not None)
                self.peers[peer.id] = peer
                self.selector.register(sock, selectors.EVENT_READ | selectors.EVENT_WRITE, peer)
                if peer.tls_ready:
                    self._hello(peer)
            except Exception:
                if peer is not None and peer.id in self.peers:
                    self._drop(peer, 'accept_failed')
                else:
                    sock.close()
                    self.errors['accept_failed'] += 1

    def _read(self, peer):
        if not self._handshake(peer):
            return
        try:
            raw = peer.sock.recv(16384)
        except (BlockingIOError, ssl.SSLWantReadError):
            peer.recv_wants_write = False
            self._interest(peer)
            return
        except ssl.SSLWantWriteError:
            peer.recv_wants_write = True
            self._interest(peer)
            return
        peer.recv_wants_write = False
        self._interest(peer)
        if not raw:
            self._drop(peer, 'eof')
            return
        lines = peer.frames.feed(raw, time.monotonic())
        if len(lines) > 16:
            raise ValueError('request flood')
        for line in lines:
            packet = peer.codec.unpack(line)
            if not peer.authenticated and time.monotonic() >= peer.auth_deadline:
                raise TimeoutError('authentication deadline')
            peer.authenticated = True
            self._pump_core()
            if peer.id not in self.peers:
                return
            response = self.sessions.handle(peer.id, packet)
            self._queue(peer, response)
            self._pump_core()

    def _write(self, peer):
        if not self._handshake(peer):
            return
        if not peer.out:
            peer.send_wants_read = False
            self._interest(peer)
            return
        data, deadline = peer.out[0]
        if time.monotonic() >= deadline:
            raise TimeoutError('network write deadline')
        try:
            count = peer.sock.send(data)
        except (BlockingIOError, ssl.SSLWantWriteError):
            peer.send_wants_read = False
            self._interest(peer)
            return
        except ssl.SSLWantReadError:
            peer.send_wants_read = True
            self._interest(peer)
            return
        peer.send_wants_read = False
        if count == 0:
            raise EOFError('network write closed')
        peer.out_size -= count
        if count == len(data):
            peer.out.popleft()
        else:
            peer.out[0] = (data[count:], deadline)
        self._interest(peer)

    def _dispatch(self, peer, events):
        if not peer.tls_ready:
            self._handshake(peer)
            return
        if events & selectors.EVENT_READ:
            if peer.send_wants_read:
                self._write(peer)
            elif not peer.recv_wants_write:
                self._read(peer)
        if peer.id in self.peers and events & selectors.EVENT_WRITE:
            if peer.recv_wants_write:
                self._read(peer)
            elif not peer.send_wants_read:
                self._write(peer)

    def _pump_core(self):
        for owner, packet in self.sessions.tick():
            peer = self.peers.get(owner)
            if peer is not None:
                self._queue(peer, packet)

    def _tick(self):
        self._pump_core()
        now = time.monotonic()
        for peer in list(self.peers.values()):
            try:
                peer.frames.check_timeout(now)
                if not peer.authenticated and now >= peer.auth_deadline:
                    raise TimeoutError('authentication deadline')
                if peer.out and now >= peer.out[0][1]:
                    raise TimeoutError('network write deadline')
            except (ValueError, TimeoutError):
                self._drop(peer, 'deadline')

    def serve(self, stop_event):
        if self.listener is None:
            raise RuntimeError('call open first')
        try:
            while not stop_event.is_set():
                self._tick()
                for key, events in self.selector.select(.005):
                    self._tick()
                    if key.data is None:
                        self._accept()
                        continue
                    peer = key.data
                    if peer.id not in self.peers:
                        continue
                    try:
                        self._dispatch(peer, events)
                    except (OSError, ValueError, TypeError, ArithmeticError, EOFError, RecursionError):
                        self._drop(peer, 'invalid_or_closed_connection')
                # TLS may already hold decrypted bytes while fd is not readable.
                for peer in list(self.peers.values()):
                    if isinstance(peer.sock, ssl.SSLSocket) and peer.tls_ready and peer.sock.pending():
                        try:
                            self._dispatch(peer, selectors.EVENT_READ)
                        except (OSError, ValueError, TypeError, ArithmeticError, EOFError, RecursionError):
                            self._drop(peer, 'invalid_or_closed_connection')
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.sessions.shutdown()
        for peer in list(self.peers.values()):
            self._drop(peer, 'listener_closed')
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        deadline = time.monotonic()+3.
        while self.sessions.active is not None and time.monotonic() < deadline:
            self.sessions.tick()
            time.sleep(.005)
        # Never kill an uncertain worker. Durable marker remains active and
        # blocks future ownership. This is not a physical stop acknowledgement.
        if self.sessions.ledger is not None:
            self.sessions.ledger.close()
