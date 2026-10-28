"""Direct network implementation of HostSessionChannel; no SSH or SDK imports.

Construction is inert. open() authenticates the service and requests exactly one
session using an installed-code digest, never uploaded Python. No reconnect or
start retry is automatic. Non-loopback connections require a pinned TLS cert.
"""
from collections import deque
import hashlib
import hmac
import json
import math
import re
import secrets
import select
import socket
import ssl
import threading
import time

from teleop.robots.kuavo5_network_config import validate_endpoint
from teleop.robots.kuavo5_network_wire import AuthenticatedCodec, FrameBuffer
from teleop.robots.kuavo5_session_profile import bundle_profile

SCOPE = 'kuavo_live_session_v1'
SAVED_PROFILE = dict(target_recovery='bounded_candidate', speed_profile='responsive',
                     arm_posture='elbow_preference', gripper_mode='off')


class StopOutcomeUnknown(RuntimeError):
    """A stop write may have reached the service, without a reliable response."""


class MaintenanceOutcomeUnknown(RuntimeError):
    """A maintenance write may have arrived; observe status, never auto-retry."""


class PreparationOutcomeUnknown(RuntimeError):
    """Retain the request ID for observation; never repeat an uncertain action."""
    def __init__(self, request_id, message='preparation outcome unknown; query status, do not retry'):
        self.request_id = request_id
        super().__init__(message)


class SettingsOutcomeUnknown(RuntimeError):
    """An idle settings update may have arrived; read back, never overwrite."""
    def __init__(self, request_id):
        self.request_id = request_id
        super().__init__('session settings outcome unknown; read settings and last_request_id, do not retry')


def _preparation_progress(callback, reply, started_at):
    """Notify an optional UI without exporting helper output or credentials.

    A result receipt is not a claim of hardware readiness. Display failures
    must not discard a valid response or change the operation's retry policy.
    KeyboardInterrupt is deliberately handled by the dispatching method.
    """
    if callback is None:
        return
    progress = {key: reply[key] for key in (
        'action', 'request_id', 'service_instance_id', 'installation_sha256')}
    progress.update(event='preparation_progress',
        phase='pending' if reply['event'] == 'preparation_pending' else 'result_received',
        elapsed_s=max(0., time.monotonic()-started_at))
    try:
        callback(progress)
    except Exception:
        # Progress is observational: a broken terminal must not replace the
        # authoritative result with an ambiguous presentation exception.
        pass


def session_manifest(bundle):
    profile = bundle_profile(bundle)
    digest = bundle.get('sha256')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('valid installed bundle SHA256 required')
    if bundle.get('mode') != 'execute':
        raise ValueError('direct session open requires an explicitly executable bundle')
    return dict(bundle_sha256=digest, profile=profile)


class DirectSessionChannel:
    def __init__(self, address, key, *, duration_s=60., cert_sha256=None,
                 expected_backend='robot', connect_timeout_s=3., socket_factory=None, allow_base_control=False):
        if type(allow_base_control) is not bool:
            raise ValueError('base permission must be an explicit boolean')
        self.allow_base_control = allow_base_control
        self.host, self.port, self.cert_sha256 = validate_endpoint(*address, cert_sha256)
        AuthenticatedCodec(key, '0'*32, 'client')
        if type(duration_s) not in (int, float) or not math.isfinite(duration_s) or duration_s < 0:
            raise ValueError('duration must be finite and nonnegative; 0 means until stopped')
        if type(connect_timeout_s) not in (int, float) or not math.isfinite(connect_timeout_s) or not 0 < connect_timeout_s <= 10:
            raise ValueError('connect timeout must be in (0, 10] seconds')
        if expected_backend not in ('robot', 'fake'):
            raise ValueError('unknown direct session backend')
        self.key, self.duration_s = key, duration_s
        self.socket_factory = socket_factory
        self.expected_backend, self.connect_timeout_s = expected_backend, connect_timeout_s
        self.sock = self.codec = self.session_id = None
        self._frames, self._lines = FrameBuffer(), deque()
        self._send_lock, self._receive_lock = threading.Lock(), threading.Lock()
        self._io_lock = threading.Lock()
        self._used = self._input_closed = self._finished = False
        self._exit_code = None
        self.service_status = None

    def _connect_status(self):
        """Authenticate and query only. Never creates or closes a robot session."""
        if self._used:
            raise RuntimeError('network channel instances are single-use')
        self._used = True
        try:
            if self.socket_factory is None:
                sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout_s)
                self.sock = sock
            else:
                # A transferred fd retains its creating network namespace.
                # The rest of this process, including PICO, stays on the host.
                sock = self.socket_factory()
                self.sock = sock
                if sock.family != socket.AF_INET or sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
                    raise ValueError('network socket factory must return an IPv4 TCP socket')
                sock.settimeout(self.connect_timeout_s)
                sock.connect((self.host, self.port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self.cert_sha256 is not None:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                sock = context.wrap_socket(sock, server_hostname=None)
                self.sock = sock
                # A self-signed robot certificate is accepted ONLY by exact pin.
                # No authentication token or session request is sent before this.
                actual = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
                if not hmac.compare_digest(actual, self.cert_sha256):
                    raise ValueError('TLS certificate pin mismatch')
            sock.setblocking(False)
            hello = json.loads(self._line(time.monotonic()+self.connect_timeout_s))
            if not isinstance(hello, dict) or set(hello) != {'event', 'version', 'connection_id', 'scope'} \
                    or hello.get('event') != 'hello' or type(hello.get('version')) is not int \
                    or hello['version'] != 1 or hello.get('scope') != SCOPE:
                raise ValueError('direct service handshake mismatch')
            self.codec = AuthenticatedCodec(self.key, hello['connection_id'], 'client')
            self._send(dict(event='status'), timeout_s=.5)
            status = self._payload(time.monotonic()+3.)
            self.service_status = status
            if status.get('event') != 'service_status':
                raise ValueError('direct service status response mismatch')
            return status
        except BaseException:
            self._abort()
            raise

    def query_status(self):
        """Read-only deployment probe, including passive or busy services."""
        try:
            return self._connect_status()
        finally:
            # No session was requested: close only this query's TCP connection.
            self._abort()

    def request_stop_session(self, session_id, *, expected_installation_sha256,
                             expected_bundle_sha256, expected_profile, confirm_stop=False,
                             allow_validated_runtime_settings=False):
        """Request cleanup of one identified session; never retry or take ownership.

        Acceptance is not cleanup completion. Query last_session later to observe
        software cleanup; neither response proves that hardware has stopped.
        """
        from teleop.robots.kuavo5_session_profile import validate_profile, valid_digest
        if confirm_stop is not True:
            raise ValueError('explicit stop confirmation required')
        if type(allow_validated_runtime_settings) is not bool:
            raise ValueError('explicit runtime settings compatibility flag required')
        if not isinstance(session_id, str) or re.fullmatch('[0-9a-f]{32}', session_id) is None:
            raise ValueError('robot session ID must be 32 lowercase hexadecimal characters')
        if not valid_digest(expected_installation_sha256) or not valid_digest(expected_bundle_sha256):
            raise ValueError('pinned installation and bundle SHA256 required')
        profile = validate_profile(expected_profile)
        try:
            status = self._connect_status()
            from .kuavo5_service_contract import CONFIGURABLE_SESSION_PROTOCOLS, validate_pinned_settings_contract
            if (allow_validated_runtime_settings and
                    (status.get('session_contract') or {}).get('protocol') in CONFIGURABLE_SESSION_PROTOCOLS):
                contract = validate_pinned_settings_contract(status,
                    expected_installation_sha256=expected_installation_sha256,
                    expected_bundle_sha256=expected_bundle_sha256, expected_profile=profile)
                profile = contract['profile']
            if (status.get('backend') != self.expected_backend
                    or status.get('management_protocol') != 'kuavo_service_requests_v1'
                    or status.get('installation_sha256') != expected_installation_sha256
                    or status.get('bundle_sha256') != expected_bundle_sha256
                    or status.get('profile') != profile):
                raise ValueError('service version/configuration mismatch; no stop requested')
            instance = status.get('service_instance_id')
            if not isinstance(instance, str) or re.fullmatch('[0-9a-f]{32}', instance) is None:
                raise ValueError('invalid service instance identity; no stop requested')
            if status.get('session_id') != session_id or status.get('owner_present') is not True:
                raise ValueError('robot session changed or already ended; no stop requested')
            try:
                self._send(dict(event='stop_session', session_id=session_id,
                    expected_service_instance_id=instance,
                    expected_installation_sha256=expected_installation_sha256,
                    expected_bundle_sha256=expected_bundle_sha256,
                    expected_profile=profile, confirm_stop=True), timeout_s=.5)
                reply = self._payload(time.monotonic()+3.)
            except (OSError, ValueError, RuntimeError, EOFError) as exc:
                raise StopOutcomeUnknown('stop outcome unknown; query status, do not retry automatically') from exc
            if reply.get('event') == 'refused':
                raise ValueError('session stop refused: '+str(reply.get('reason')))
            expected = dict(event='session_stop_requested', session_id=session_id,
                service_instance_id=instance, installation_sha256=expected_installation_sha256,
                phase='closing', software_cleanup_confirmed=False, physical_stop_verified=False)
            if reply != expected:
                raise StopOutcomeUnknown('stop outcome unknown: unexpected reply; query status, do not retry automatically')
            return reply
        finally:
            self._abort()

    def request_stop_compatible_session(self, session_id, *, required_contract=None,
                                        confirm_stop=False):
        """Stop precisely the caller's session using one authenticated connection.

        Function releases may differ from a previous PC release. The observed
        instance/installation and exact session remain mandatory request pins.
        """
        from teleop.robots.kuavo5_service_contract import (validate_requirements,
            validate_service_contract, compatible_requirements)
        if confirm_stop is not True:
            raise ValueError('explicit stop confirmation required')
        if not isinstance(session_id, str) or re.fullmatch('[0-9a-f]{32}', session_id) is None:
            raise ValueError('robot session ID must be 32 lowercase hexadecimal characters')
        requirements = validate_requirements(required_contract)
        try:
            status = self._connect_status()
            contract = validate_service_contract(status, compatible_requirements(status, requirements))
            if (status.get('backend') != self.expected_backend
                    or status.get('management_protocol') != 'kuavo_service_requests_v1'):
                raise ValueError('service management protocol/backend mismatch; no stop requested')
            if status.get('session_id') != session_id or status.get('owner_present') is not True:
                raise ValueError('robot session changed or already ended; no stop requested')
            try:
                self._send(dict(event='stop_session', session_id=session_id,
                    expected_service_instance_id=contract['service_instance_id'],
                    expected_installation_sha256=contract['installation_sha256'],
                    expected_bundle_sha256=contract['bundle_sha256'],
                    expected_profile=contract['profile'], confirm_stop=True), timeout_s=.5)
                reply = self._payload(time.monotonic()+3.)
            except (OSError, ValueError, RuntimeError, EOFError) as exc:
                raise StopOutcomeUnknown('stop outcome unknown; query status, do not retry automatically') from exc
            if reply.get('event') == 'refused':
                raise ValueError('session stop refused: '+str(reply.get('reason')))
            expected = dict(event='session_stop_requested', session_id=session_id,
                service_instance_id=contract['service_instance_id'],
                installation_sha256=contract['installation_sha256'], phase='closing',
                software_cleanup_confirmed=False, physical_stop_verified=False)
            if reply != expected:
                raise StopOutcomeUnknown('stop outcome unknown: unexpected reply; query status, do not retry automatically')
            return reply
        finally:
            self._abort()

    def maintenance_request(self, action, *, expected_service_instance_id,
                            expected_installation_sha256, confirm=False):
        """Atomically gate starts on a clean idle listener; never stop a worker."""
        from teleop.robots.kuavo5_service_contract import MAINTENANCE_PROTOCOL
        if action not in ('enter', 'leave') or confirm is not True:
            raise ValueError('explicit enter/leave maintenance confirmation required')
        if (not isinstance(expected_service_instance_id, str)
                or re.fullmatch('[0-9a-f]{32}', expected_service_instance_id) is None
                or not isinstance(expected_installation_sha256, str)
                or re.fullmatch('[0-9a-f]{64}', expected_installation_sha256) is None):
            raise ValueError('observed service instance and installation required')
        try:
            status = self._connect_status()
            if (status.get('maintenance_protocol') != MAINTENANCE_PROTOCOL
                    or status.get('backend') != self.expected_backend
                    or status.get('service_instance_id') != expected_service_instance_id
                    or status.get('installation_sha256') != expected_installation_sha256):
                raise ValueError('maintenance service identity/protocol mismatch')
            if (status.get('owner_present') is not False or status.get('blocked_reason')
                    or status.get('phase') not in ('idle', 'maintenance')):
                raise ValueError('maintenance requires clean idle service')
            try:
                self._send(dict(event=action+'_maintenance',
                    expected_service_instance_id=expected_service_instance_id,
                    expected_installation_sha256=expected_installation_sha256,
                    confirm=True), timeout_s=.5)
                reply = self._payload(time.monotonic()+3.)
            except (OSError, ValueError, RuntimeError, EOFError) as exc:
                raise MaintenanceOutcomeUnknown('maintenance outcome unknown; query status, do not retry automatically') from exc
            if reply.get('event') == 'refused':
                raise ValueError('maintenance refused: '+str(reply.get('reason')))
            maintenance = action == 'enter'
            expected = dict(event='maintenance_entered' if maintenance else 'maintenance_left',
                service_instance_id=expected_service_instance_id,
                installation_sha256=expected_installation_sha256, maintenance=maintenance,
                owner_present=False, phase='maintenance' if maintenance else 'idle',
                physical_robot_command_sent=False)
            if reply != expected:
                raise MaintenanceOutcomeUnknown('maintenance outcome unknown: unexpected reply; query status')
            return reply
        finally:
            self._abort()

    def controller_request(self, action, *, confirm_motion=False):
        """One explicit request, no SSH, no reconnect/retry of side effects."""
        if action not in ('status', 'prepare', 'initialize') or type(confirm_motion) is not bool:
            raise ValueError('valid controller operation required')
        if action != 'status' and not confirm_motion:
            raise ValueError('explicit motion confirmation required')
        try:
            status = self._connect_status()
            if status.get('controller_admin_enabled') is not True:
                raise ValueError('server does not have controller administration enabled; use the installed boot console')
            rid = secrets.token_hex(16)
            self._send(dict(event='controller_request', action=action, request_id=rid,
                            confirm_motion=confirm_motion), timeout_s=.5)
            deadline = time.monotonic()+25.
            while True:
                reply = self._payload(min(deadline, time.monotonic()+3.))
                if reply.get('event') == 'refused':
                    raise ValueError('controller request refused: '+str(reply.get('reason')))
                if reply.get('request_id') != rid:
                    raise ValueError('controller reply identity mismatch')
                if reply.get('event') == 'controller_result':
                    return reply
                if reply.get('event') != 'controller_pending' or time.monotonic() >= deadline:
                    raise TimeoutError('controller outcome unknown; observe status, do not retry automatically')
                time.sleep(.1)
                self._send(dict(event='controller_job', request_id=rid), timeout_s=.5)
        finally:
            self._abort()

    def settings_request(self, action, *, changes=None, expected_service_instance_id=None,
                         expected_installation_sha256=None, expected_revision=None,
                         request_id=None, confirm=False):
        """One settings request. Updates are idle-only on the authoritative server."""
        from .kuavo5_service_contract import (CONFIGURABLE_SESSION_PROTOCOLS,
            SESSION_PROTOCOL, ARM_CAPABILITY, GRIPPER_CAPABILITY,
            compatible_requirements, validate_service_contract,
            validate_settings_values, validate_settings_snapshot, validate_settings_schema)
        if action not in ('settings_get', 'settings_preview', 'settings_apply', 'settings_reset'):
            raise ValueError('unsupported settings operation')
        mutate = action in ('settings_apply', 'settings_reset')
        packet = dict(event=action)
        if action == 'settings_get':
            if any(x is not None for x in (changes, expected_service_instance_id,
                    expected_installation_sha256, expected_revision, request_id)) or confirm is not False:
                raise ValueError('settings get accepts no mutation fields')
        else:
            if (not isinstance(expected_service_instance_id, str)
                    or re.fullmatch('[a-f0-9]{32}', expected_service_instance_id) is None
                    or not isinstance(expected_installation_sha256, str)
                    or re.fullmatch('[a-f0-9]{64}', expected_installation_sha256) is None
                    or type(expected_revision) is not int or expected_revision < 0):
                raise ValueError('settings update requires observed identity and nonnegative revision')
            packet.update(expected_service_instance_id=expected_service_instance_id,
                expected_installation_sha256=expected_installation_sha256, expected_revision=expected_revision)
            if action != 'settings_reset':
                packet['changes'] = validate_settings_values(changes, partial=True)
            elif changes is not None:
                raise ValueError('reset accepts no changes')
            if mutate:
                if confirm is not True:
                    raise ValueError('explicit settings update confirmation required')
                request_id = secrets.token_hex(16) if request_id is None else request_id
                if not isinstance(request_id, str) or re.fullmatch('[a-f0-9]{32}', request_id) is None:
                    raise ValueError('valid settings request identity required')
                packet.update(request_id=request_id, confirm=True)
            elif confirm is not False or request_id is not None:
                raise ValueError('settings preview accepts no update confirmation')
        dispatched = False
        instance, installation = expected_service_instance_id, expected_installation_sha256
        try:
            status = self._connect_status()
            if (status.get('backend') != self.expected_backend
                    or (status.get('session_contract') or {}).get('protocol') not in CONFIGURABLE_SESSION_PROTOCOLS):
                raise ValueError('versioned settings service unavailable')
            validate_service_contract(status, compatible_requirements(status,
                dict(protocol=SESSION_PROTOCOL, capabilities=[ARM_CAPABILITY, GRIPPER_CAPABILITY])))
            if changes is not None:
                from .kuavo5_service_contract import expand_settings_changes
                expand_settings_changes(changes, status['session_contract']['session_settings']['values'])
            observed = (status.get('service_instance_id'), status.get('installation_sha256'))
            if action != 'settings_get' and observed != (instance, installation):
                raise ValueError('settings service changed; no update sent')
            instance, installation = observed
            dispatched = mutate
            self._send(packet, timeout_s=.5)
            reply = self._payload(time.monotonic()+3.)
            if reply.get('event') == 'refused':
                dispatched = False
                raise ValueError('settings request refused: '+str(reply.get('reason')))
            fields = {'event', 'action', 'ok', 'error', 'request_id', 'service_instance_id',
                'installation_sha256', 'editable', 'locked_reason', 'snapshot', 'defaults', 'schema', 'last_request_id',
                'outcome_unknown'}
            if (set(reply) != fields or reply.get('event') != 'session_settings_result'
                    or reply.get('action') != action or reply.get('request_id') != request_id
                    or reply.get('service_instance_id') != instance
                    or reply.get('installation_sha256') != installation
                    or type(reply.get('ok')) is not bool or type(reply.get('editable')) is not bool
                    or type(reply.get('outcome_unknown')) is not bool
                    or (reply.get('locked_reason') is not None and
                        (not isinstance(reply['locked_reason'], str) or not reply['locked_reason']))
                    or reply['editable'] != (reply['locked_reason'] is None)
                    or (reply.get('last_request_id') is not None and
                        (not isinstance(reply['last_request_id'], str) or
                         re.fullmatch('[a-f0-9]{32}', reply['last_request_id']) is None))
                    or (reply['ok'] and reply.get('error') is not None)
                    or (not reply['ok'] and (not isinstance(reply.get('error'), str) or not reply['error']))
                    or not isinstance(reply.get('schema'), dict)):
                raise ValueError('settings reply identity or fields mismatch')
            if reply['outcome_unknown']:
                if not mutate or reply['ok']:
                    raise ValueError('invalid settings outcome state')
                raise SettingsOutcomeUnknown(request_id)
            if reply['snapshot'] is None:
                if reply['ok'] or reply['editable'] or reply['locked_reason'] is None:
                    raise ValueError('missing settings readback without explicit refusal')
                if reply['defaults'] or reply['schema']:
                    validate_settings_schema(reply['schema'], reply['defaults'])
                elif reply['defaults'] != {}:
                    raise ValueError('invalid settings defaults')
                return reply  # Server explicitly confirms no uncertain write.
            snapshot = validate_settings_snapshot(reply['snapshot'])
            if (snapshot['installation_sha256'] != installation
                    or snapshot['bundle_sha256'] != status.get('bundle_sha256')):
                raise ValueError('settings snapshot installation mismatch')
            validate_settings_schema(reply['schema'], reply['defaults'])
            for name, value in snapshot['values'].items():
                field = reply['schema'][name]
                if (not field['min'] <= value <= field['max']
                        or (field.get('min_exclusive', False) and value == field['min'])):
                    raise ValueError('settings snapshot exceeds its published bounds')
            if reply['ok'] is True and mutate and (snapshot['revision'] != expected_revision+1
                    or reply['last_request_id'] != request_id):
                raise ValueError('settings update receipt mismatch')
            return reply
        except (OSError, EOFError, RuntimeError, ValueError, KeyboardInterrupt) as exc:
            if dispatched:
                unknown = SettingsOutcomeUnknown(request_id)
                unknown.service_instance_id, unknown.installation_sha256 = instance, installation
                raise unknown from exc
            raise
        finally:
            self._abort()

    def preparation_request(self, action, *, expected_service_instance_id,
                            expected_installation_sha256, confirm_motion=False,
                            motor_ids_confirmed=False, accept_unverified_gripper_bus=False,
                            request_id=None, on_progress=None, expected_contract_sha256=None):
        """Exactly one dispatch, with optional validated receipt notifications.

        on_progress receives only identity, action, phase and elapsed seconds.
        It must return promptly; it cannot change a request or its result.
        """
        from .kuavo5_controller_admin import PREPARATION_PROTOCOL, validate_preparation_request
        rid = secrets.token_hex(16) if request_id is None else request_id
        request = validate_preparation_request(dict(action=action, request_id=rid,
            expected_installation_sha256=expected_installation_sha256,
            confirm_motion=confirm_motion, motor_ids_confirmed=motor_ids_confirmed,
            accept_unverified_gripper_bus=accept_unverified_gripper_bus))
        if not isinstance(expected_service_instance_id, str) or re.fullmatch('[a-f0-9]{32}', expected_service_instance_id) is None:
            raise ValueError('valid expected service instance required')
        if on_progress is not None and not callable(on_progress):
            raise ValueError('preparation progress callback must be callable')
        if expected_contract_sha256 is not None and (not isinstance(expected_contract_sha256, str)
                or re.fullmatch('[a-f0-9]{64}', expected_contract_sha256) is None):
            raise ValueError('valid expected contract digest required')
        started_at = time.monotonic()
        dispatched = False
        pending_seen = False
        try:
            status = self._connect_status()
            if (status.get('backend') != self.expected_backend
                    or status.get('robot_preparation_protocol') != PREPARATION_PROTOCOL
                    or not isinstance(status.get('robot_preparation_actions'), list)
                    or action not in status['robot_preparation_actions']
                    or status.get('service_instance_id') != expected_service_instance_id
                    or status.get('installation_sha256') != expected_installation_sha256):
                raise ValueError('preparation service protocol/capability/identity mismatch')
            if action != 'status' and (status.get('owner_present') is not False
                    or status.get('phase') != 'idle' or status.get('blocked_reason')
                    or status.get('robot_preparation_busy')):
                raise ValueError('preparation requires clean idle service')
            if expected_contract_sha256 is not None:
                if (status.get('session_contract') or {}).get('contract_sha256') != expected_contract_sha256:
                    raise ValueError('preparation settings changed; no request sent')
                request['expected_contract_sha256'] = expected_contract_sha256
            dispatched = True  # A partial write also has an uncertain outcome.
            self._send(dict(event='preparation_request',
                expected_service_instance_id=expected_service_instance_id, **request), timeout_s=.5)
            deadline = time.monotonic()+210.
            identity = dict(request_id=rid, action=action, service_instance_id=expected_service_instance_id,
                            installation_sha256=expected_installation_sha256)
            while True:
                reply = self._payload(min(deadline, time.monotonic()+3.))
                if reply.get('event') == 'refused':
                    # A refusal to the initial request proves no new dispatch.
                    # A missing poll result, however, cannot undo the first one.
                    if pending_seen:
                        raise PreparationOutcomeUnknown(rid, 'preparation receipt unavailable; do not retry')
                    dispatched = False
                    raise ValueError('preparation request refused: '+str(reply.get('reason')))
                if any(reply.get(key) != value for key, value in identity.items()):
                    raise PreparationOutcomeUnknown(rid, 'preparation reply identity mismatch; do not retry')
                if reply.get('event') == 'preparation_result':
                    if (set(reply) != {'event', *identity, 'result', 'error'}
                            or (reply['result'] is not None and not isinstance(reply['result'], dict))
                            or (reply['error'] is not None and not isinstance(reply['error'], dict))):
                        raise PreparationOutcomeUnknown(rid, 'invalid preparation result; do not retry')
                    _preparation_progress(on_progress, reply, started_at)
                    return reply
                if (reply.get('event') != 'preparation_pending' or set(reply) != {'event', *identity}
                        or time.monotonic() >= deadline):
                    raise PreparationOutcomeUnknown(rid)
                pending_seen = True
                _preparation_progress(on_progress, reply, started_at)
                time.sleep(.1)
                self._send(dict(event='preparation_job', request_id=rid,
                    expected_service_instance_id=expected_service_instance_id,
                    expected_installation_sha256=expected_installation_sha256), timeout_s=.5)
        except PreparationOutcomeUnknown as exc:
            exc.service_instance_id = expected_service_instance_id
            exc.installation_sha256 = expected_installation_sha256
            raise
        except (OSError, EOFError, TimeoutError, RuntimeError, ValueError, KeyboardInterrupt) as exc:
            if dispatched:
                unknown = PreparationOutcomeUnknown(rid)
                unknown.service_instance_id = expected_service_instance_id
                unknown.installation_sha256 = expected_installation_sha256
                raise unknown from exc
            raise
        finally:
            self._abort()

    def query_preparation_job(self, request_id, *, expected_service_instance_id,
                              expected_installation_sha256, on_progress=None):
        """Observe an existing receipt after reconnect; never creates a job."""
        from .kuavo5_controller_admin import PREPARATION_PROTOCOL, PREPARATION_ACTIONS
        for value, size in ((request_id, 32), (expected_service_instance_id, 32),
                            (expected_installation_sha256, 64)):
            if not isinstance(value, str) or re.fullmatch('[a-f0-9]{%d}' % size, value) is None:
                raise ValueError('valid preparation receipt identity required')
        if on_progress is not None and not callable(on_progress):
            raise ValueError('preparation progress callback must be callable')
        started_at = time.monotonic()
        try:
            status = self._connect_status()
            if (status.get('backend') != self.expected_backend
                    or status.get('robot_preparation_protocol') != PREPARATION_PROTOCOL
                    or status.get('service_instance_id') != expected_service_instance_id
                    or status.get('installation_sha256') != expected_installation_sha256):
                raise ValueError('preparation receipt service changed; do not resend action')
            self._send(dict(event='preparation_job', request_id=request_id,
                expected_service_instance_id=expected_service_instance_id,
                expected_installation_sha256=expected_installation_sha256), timeout_s=.5)
            reply = self._payload(time.monotonic()+3.)
            if reply.get('event') == 'refused':
                raise ValueError('preparation receipt unavailable: '+str(reply.get('reason')))
            identity = dict(request_id=request_id, service_instance_id=expected_service_instance_id,
                            installation_sha256=expected_installation_sha256)
            if (any(reply.get(k) != v for k, v in identity.items())
                    or reply.get('action') not in PREPARATION_ACTIONS
                    or reply.get('event') not in ('preparation_pending', 'preparation_result')):
                raise ValueError('preparation receipt identity mismatch')
            expected = {'event', 'action', *identity}
            if reply['event'] == 'preparation_result':
                expected.update(('result', 'error'))
                if ((reply.get('result') is not None and not isinstance(reply['result'], dict))
                        or (reply.get('error') is not None and not isinstance(reply['error'], dict))):
                    raise ValueError('invalid preparation receipt result')
            if set(reply) != expected:
                raise ValueError('invalid preparation receipt fields')
            _preparation_progress(on_progress, reply, started_at)
            return reply
        finally:
            self._abort()

    def open(self, bundle):
        manifest = session_manifest(bundle)
        base_options = self._base_permission(manifest['profile'])
        try:
            status = self._connect_status()
            if self.duration_s == 0 and status.get('session_duration_policy') != 'zero_is_unbounded_v1':
                raise ValueError('robot service upgrade required for continuous sessions; no session started')
            if status.get('event') != 'service_status' or status.get('backend') != self.expected_backend:
                raise ValueError('direct service backend identity mismatch')
            if status.get('bundle_sha256') != manifest['bundle_sha256'] or status.get('profile') != manifest['profile']:
                raise ValueError('installed direct worker manifest mismatch')
            if status.get('ready_to_start') is not True or status.get('owner_present') is not False:
                raise ValueError('direct service is unavailable: '+str(status.get('blocked_reason') or 'session busy'))
            self._send(dict(event='start_session', duration_s=self.duration_s,
                            motion_enabled=True, configure_vendor_profile=True,
                            expected_backend=self.expected_backend, **manifest,
                            **base_options,
                            **({'allow_gripper_control': True} if manifest['profile']['gripper_mode'] == 'damiao' else {})), timeout_s=.5)
            started = self._payload(time.monotonic()+3.)
            if started.get('event') == 'refused':
                raise ValueError('direct start refused: '+str(started.get('reason')))
            if started.get('event') != 'session_started' or started.get('backend') != self.expected_backend \
                    or not isinstance(started.get('session_id'), str) \
                    or not re.fullmatch('[0-9a-f]{32}', started['session_id']):
                raise ValueError('direct session start handshake mismatch')
            self.session_id = started['session_id']
        except BaseException:
            self._abort()
            raise

    def open_contract(self, contract):
        """Start one installed session; no robot source/config upload from PC.

        A previously observed descriptor is rechecked on this connection. The
        server rechecks its identity atomically before reservation, preventing
        a publication between status and start from selecting a different build.
        """
        from teleop.robots.kuavo5_service_contract import (SESSION_PROTOCOL,
            ARM_CAPABILITY, GRIPPER_CAPABILITY, validate_contract, validate_service_contract,
            compatible_requirements)
        requirements = compatible_requirements(contract, dict(protocol=SESSION_PROTOCOL,
                            capabilities=[ARM_CAPABILITY, GRIPPER_CAPABILITY]))
        contract = validate_contract(contract, requirements)
        base_options = self._base_permission(contract['profile'])
        try:
            status = self._connect_status()
            current = validate_service_contract(status, requirements)
            if current != contract:
                raise ValueError('observed service contract changed; no session started')
            if status.get('backend') != self.expected_backend:
                raise ValueError('direct service backend identity mismatch')
            if status.get('ready_to_start') is not True or status.get('owner_present') is not False:
                raise ValueError('direct service is unavailable: '+str(status.get('blocked_reason') or status.get('phase')))
            self._send(dict(event='start_session_contract', duration_s=self.duration_s,
                motion_enabled=True, configure_vendor_profile=True, allow_gripper_control=True,
                expected_backend=self.expected_backend, bundle_sha256=contract['bundle_sha256'],
                profile=contract['profile'], session_protocol=contract['protocol'],
                expected_service_instance_id=contract['service_instance_id'],
                expected_installation_sha256=contract['installation_sha256'],
                contract_sha256=contract['contract_sha256'], **base_options), timeout_s=.5)
            started = self._payload(time.monotonic()+3.)
            if started.get('event') == 'refused':
                raise ValueError('direct start refused: '+str(started.get('reason')))
            expected = dict(event='session_started', backend=self.expected_backend,
                service_instance_id=contract['service_instance_id'],
                installation_sha256=contract['installation_sha256'],
                contract_sha256=contract['contract_sha256'])
            if (set(started) != set(expected) | {'session_id'}
                    or any(started.get(k) != v for k, v in expected.items())
                    or not isinstance(started.get('session_id'), str)
                    or re.fullmatch('[0-9a-f]{32}', started['session_id']) is None):
                raise ValueError('direct contract session start handshake mismatch')
            self.session_id = started['session_id']
        except BaseException:
            self._abort()
            raise

    def _base_permission(self, profile):
        enabled = profile.get('base_mode', 'disabled') == 'vendor_vr_x'
        if enabled != self.allow_base_control:
            raise ValueError('installed base mode requires matching explicit --with-base permission')
        return {'allow_base_control': True} if enabled else {}

    def _socket_for_io(self, expected=None):
        """Called under _io_lock; a concurrent close is an EOF, not a receipt."""
        if self._finished or self.sock is None or (expected is not None and self.sock is not expected):
            raise EOFError('direct session connection closed')
        return self.sock

    def _wait_socket(self, sock, *, reading, timeout_s):
        # Do not hold the TLS I/O lock while waiting: the opposite direction
        # must remain able to make progress. finish() can detach/close the fd
        # meanwhile, so re-check identity before the subsequent actual I/O.
        try:
            ready = select.select([sock] if reading else [],
                                  [] if reading else [sock], [], timeout_s)
        except (OSError, ValueError):
            with self._io_lock:
                self._socket_for_io(sock)
            raise
        return bool(ready[0] if reading else ready[1])

    def _line(self, deadline):
        want_write = False
        while not self._lines:
            now = time.monotonic()
            self._frames.check_timeout(now)
            if now >= deadline:
                raise TimeoutError('direct response deadline expired')
            # SSL may already hold decrypted bytes even without socket readability.
            with self._io_lock:
                sock = self._socket_for_io()
                pending = isinstance(sock, ssl.SSLSocket) and sock.pending() > 0
            if not pending or want_write:
                if not self._wait_socket(sock, reading=not want_write,
                                         timeout_s=min(.02, deadline-now)):
                    continue
            if time.monotonic() >= deadline:
                raise TimeoutError('direct response deadline expired')
            try:
                with self._io_lock:
                    self._socket_for_io(sock)
                    raw = sock.recv(16384)
            except ssl.SSLWantWriteError:
                want_write = True
                continue
            except (BlockingIOError, ssl.SSLWantReadError):
                want_write = False
                continue
            want_write = False
            if not raw:
                raise EOFError('direct session connection closed')
            self._lines.extend(self._frames.feed(raw, time.monotonic()))
        if time.monotonic() >= deadline:
            raise TimeoutError('direct response deadline expired')
        return self._lines.popleft()

    def _payload(self, deadline):
        packet = self.codec.unpack(self._line(deadline))
        if not isinstance(packet, dict):
            raise ValueError('invalid direct response')
        return packet

    def _send(self, packet, *, timeout_s, absolute_deadline=None):
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('finite positive send timeout required')
        deadline = time.monotonic()+timeout_s
        if absolute_deadline is not None:
            if type(absolute_deadline) not in (int, float) or not math.isfinite(absolute_deadline):
                raise ValueError('finite absolute deadline required')
            deadline = min(deadline, absolute_deadline)
        locked = False
        try:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not self._send_lock.acquire(timeout=remaining):
                raise TimeoutError('direct send deadline expired')
            locked = True
            with self._io_lock:
                sock = self._socket_for_io()
                if self.codec is None:
                    raise EOFError('direct session connection closed')
            data = memoryview(self.codec.pack(packet))
            want_read = False
            while data:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('direct send deadline expired')
                if not self._wait_socket(sock, reading=want_read, timeout_s=remaining) \
                        or time.monotonic() >= deadline:
                    raise TimeoutError('direct send deadline expired')
                try:
                    with self._io_lock:
                        self._socket_for_io(sock)
                        if time.monotonic() >= deadline:
                            raise TimeoutError('direct send deadline expired')
                        sent = sock.send(data)
                    if not sent:
                        raise EOFError('direct send closed')
                    data = data[sent:]
                    want_read = False
                except ssl.SSLWantReadError:
                    want_read = True
                except (BlockingIOError, ssl.SSLWantWriteError):
                    want_read = False
        except BaseException:
            # After partial writes, continuing would corrupt framing/sequence.
            # Disconnect requests worker cleanup; never reconnect or replay.
            self._abort()
            raise
        finally:
            if locked:
                self._send_lock.release()

    def receive(self):
        from teleop.robots.kuavo5_live_protocol import decode_robot_packet
        with self._receive_lock:
            if self._exit_code is not None or self._finished:
                return None
            try:
                payload = self._payload(time.monotonic()+95.)
                if payload.get('event') == 'refused':
                    return payload
                if set(payload) != {'event', 'session_id', 'packet'} \
                        or payload['event'] != 'robot_packet' or payload['session_id'] != self.session_id:
                    raise ValueError('wrong direct session response')
                packet = decode_robot_packet(json.dumps(payload['packet'], allow_nan=False))
                if packet.get('event') == 'closed':
                    code = packet.get('exit_code')
                    if type(code) is not int:
                        raise ValueError('invalid direct close report')
                    self._exit_code = code
                return packet
            except BaseException:
                self._abort()
                raise

    def stderr_lines(self):
        # Service stdout carries all diagnostic records. No SSH stderr stream.
        return iter(())

    def send_input(self, packet, *, timeout_s, absolute_deadline=None):
        if self._input_closed or self.session_id is None or self._exit_code is not None or self._finished:
            raise ValueError('direct session input is closed')
        if not isinstance(packet, dict) or packet.get('event') != 'xr':
            raise ValueError('direct channel accepts only XR input packets')
        self._send(dict(event='input', session_id=self.session_id, packet=packet),
                   timeout_s=timeout_s, absolute_deadline=absolute_deadline)

    def close_input(self):
        if self._input_closed:
            return
        self._input_closed = True
        if self.session_id is not None and self.sock is not None and self._exit_code is None:
            self._send(dict(event='close_session', session_id=self.session_id), timeout_s=.25)

    def poll(self):
        return self._exit_code

    def _abort(self):
        # Serialize close with nonblocking TLS read/write as well. In
        # particular, never replace self.sock while another thread uses it.
        with self._io_lock:
            self._exit_code = 1 if self._exit_code is None else self._exit_code
            self._finished = True
            sock, self.sock = self.sock, None
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def finish(self):
        self._abort()


NetworkSessionChannel = DirectSessionChannel
