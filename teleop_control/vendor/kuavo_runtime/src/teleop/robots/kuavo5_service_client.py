"""Same management API for robot-local requests and direct PC requests.

No SSH, source upload, ROS, PICO or hardware discovery. Construction and config
loading do not connect. A request connects once, authenticates and disconnects.
Starting/streaming PICO remains the existing KuavoArmSession responsibility.
"""
import json
import os
from pathlib import Path
import re
import stat

from teleop.robots.kuavo5_direct_channel import DirectSessionChannel
from teleop.robots.kuavo5_network_config import read_auth_key, validate_endpoint
from teleop.robots.kuavo5_session_profile import valid_digest, validate_profile


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate client configuration field')
        result[key] = value
    return result


def load_request_config(path):
    """Read explicit private configuration; never trust pins learned from a peer."""
    fd = os.open(os.fspath(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('client config must be an owner-only regular file (0600 or 0400)')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise ValueError('client config too large')
        config = json.loads(raw, object_pairs_hook=_strict_object)
    finally:
        os.close(fd)
    return validate_request_config(config)


def validate_request_config(config):
    if not isinstance(config, dict):
        raise ValueError('client config must be an object')
    required = {'network_host', 'network_port', 'network_token_file',
                'network_cert_sha256'}
    if not required <= set(config):
        raise ValueError('missing client endpoint or credentials')
    host, port, cert = validate_endpoint(config['network_host'], config['network_port'],
                                         config['network_cert_sha256'])
    token = config['network_token_file']
    if not isinstance(token, str) or not Path(token).is_absolute():
        raise ValueError('absolute local token file path required')
    connection = dict(network_host=host, network_port=port, network_cert_sha256=cert,
                      network_token_file=token)
    policy = config.get('connection_policy', 'pinned_release_v1')
    if policy == 'compatible_service_v1':
        from teleop.robots.kuavo5_service_contract import validate_requirements
        if set(config) != required | {'connection_policy', 'required_contract'}:
            raise ValueError('compatible service config requires only endpoint, credentials and explicit contract')
        return dict(connection, connection_policy=policy,
                    required_contract=validate_requirements(config['required_contract']))
    if policy != 'pinned_release_v1':
        raise ValueError('unknown client connection policy')
    if not {'bundle_sha256', 'profile'} <= set(config):
        raise ValueError('missing installed profile or pinned bundle')
    if not valid_digest(config['bundle_sha256']):
        raise ValueError('pinned bundle SHA256 required')
    installation = config.get('installation_sha256')
    if installation is not None and not valid_digest(installation):
        raise ValueError('invalid installation SHA256')
    return dict(connection, bundle_sha256=config['bundle_sha256'],
                profile=validate_profile(config['profile']), installation_sha256=installation)


class KuavoServiceClient:
    """Management requests shared by the CLI and platform backend.

    stop_session uses the ROBOT session ID returned by get_status, not the local
    KuavoArmSession handle. It requests existing orderly cleanup, not motor disable.
    """
    def __init__(self, config, *, socket_factory=None, expected_backend='robot'):
        self._config = validate_request_config(config)
        if expected_backend not in ('robot', 'fake'):
            raise ValueError('invalid expected backend')
        self._backend, self._socket_factory = expected_backend, socket_factory

    @classmethod
    def from_config(cls, path, **kwargs):
        return cls(load_request_config(path), **kwargs)

    def _channel(self):
        cfg = self._config
        return DirectSessionChannel((cfg['network_host'], cfg['network_port']),
            read_auth_key(cfg['network_token_file']), cert_sha256=cfg['network_cert_sha256'],
            socket_factory=self._socket_factory, expected_backend=self._backend)

    def get_status(self):
        """Observe even a busy/older service; report mismatches without changing it."""
        status = self._channel().query_status()
        cfg = self._config
        if cfg.get('connection_policy') == 'compatible_service_v1':
            from teleop.robots.kuavo5_service_contract import validate_service_contract
            error = None
            try:
                validate_service_contract(status, cfg['required_contract'])
            except ValueError as exc:
                error = str(exc)
            matches = dict(backend=status.get('backend') == self._backend,
                           contract=error is None)
            return dict(event='KUAVO_SERVICE_STATUS', service=status,
                connection_policy='compatible_service_v1', expected_match=matches,
                compatibility_error=error,
                management_ready=(all(matches.values()) and
                    status.get('management_protocol') == 'kuavo_service_requests_v1' and
                    isinstance(status.get('service_instance_id'), str) and
                    re.fullmatch('[0-9a-f]{32}', status['service_instance_id']) is not None),
                session_start_requested=False, stop_requested=False,
                physical_robot_command_sent=False)
        matches = dict(backend=status.get('backend') == self._backend,
            bundle=status.get('bundle_sha256') == cfg['bundle_sha256'],
            profile=status.get('profile') == cfg['profile'],
            installation=(None if cfg['installation_sha256'] is None else
                          status.get('installation_sha256') == cfg['installation_sha256']))
        return dict(event='KUAVO_SERVICE_STATUS', service=status, expected_match=matches,
            management_ready=(all(v is True for v in matches.values()) and
                              status.get('management_protocol') == 'kuavo_service_requests_v1' and
                              isinstance(status.get('service_instance_id'), str) and
                              re.fullmatch('[0-9a-f]{32}', status['service_instance_id']) is not None),
            session_start_requested=False, stop_requested=False,
            physical_robot_command_sent=False)

    def stop_session(self, robot_session_id, *, confirm_stop=False):
        """One explicit stop request; lost acknowledgments require status inspection."""
        if confirm_stop is not True:
            raise ValueError('explicit stop confirmation required')
        cfg = self._config
        if cfg.get('connection_policy') == 'compatible_service_v1':
            return self._channel().request_stop_compatible_session(robot_session_id,
                required_contract=cfg['required_contract'], confirm_stop=True)
        if cfg['installation_sha256'] is None:
            raise ValueError('installation pin missing; update reviewed deployment config before stopping')
        return self._channel().request_stop_session(robot_session_id,
            expected_installation_sha256=cfg['installation_sha256'],
            expected_bundle_sha256=cfg['bundle_sha256'], expected_profile=cfg['profile'],
            confirm_stop=True)

    def maintenance(self, action, *, expected_service_instance_id,
                    expected_installation_sha256, confirm=False):
        """Publisher-only admission fence; never closes an active session.

        Identity is supplied explicitly by the publisher's reviewed observation.
        A newly installed request tool can therefore fence an older installation,
        without changing the configuration or silently accepting another instance.
        """
        if confirm is not True:
            raise ValueError('explicit maintenance confirmation required')
        if action not in ('enter', 'leave'):
            raise ValueError('maintenance action must be enter or leave')
        if not isinstance(expected_service_instance_id, str) or re.fullmatch(
                '[0-9a-f]{32}', expected_service_instance_id) is None:
            raise ValueError('valid expected service instance ID required')
        if not valid_digest(expected_installation_sha256):
            raise ValueError('valid expected installation SHA256 required')
        return self._channel().maintenance_request(action,
            expected_service_instance_id=expected_service_instance_id,
            expected_installation_sha256=expected_installation_sha256, confirm=True)

    def preparation(self, action, *, confirm_motion=False, motor_ids_confirmed=False,
                    accept_unverified_gripper_bus=False, request_id=None):
        """Same preparation requests for robot-local and PC callers.

        Observation does not require hardware readiness: a stopped controller
        is precisely when startup requests are useful. Identity/contract still
        have to match before any request is sent.
        """
        from .kuavo5_controller_admin import validate_preparation_request
        import secrets
        rid = secrets.token_hex(16) if request_id is None else request_id
        validate_preparation_request(dict(action=action, request_id=rid,
            expected_installation_sha256='0'*64, confirm_motion=confirm_motion,
            motor_ids_confirmed=motor_ids_confirmed,
            accept_unverified_gripper_bus=accept_unverified_gripper_bus))
        observed = self.get_status()
        if observed.get('management_ready') is not True:
            raise ValueError('compatible authenticated service required for preparation')
        status = observed['service']
        return self._channel().preparation_request(action,
            expected_service_instance_id=status['service_instance_id'],
            expected_installation_sha256=status['installation_sha256'],
            confirm_motion=confirm_motion, motor_ids_confirmed=motor_ids_confirmed,
            accept_unverified_gripper_bus=accept_unverified_gripper_bus,
            request_id=rid)

    def preparation_job(self, request_id, *, expected_service_instance_id,
                        expected_installation_sha256):
        """Read a specific previous request; never re-issue preparation."""
        return self._channel().query_preparation_job(request_id,
            expected_service_instance_id=expected_service_instance_id,
            expected_installation_sha256=expected_installation_sha256)
