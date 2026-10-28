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
            from teleop.robots.kuavo5_service_contract import validate_service_contract, compatible_requirements
            error = None
            try:
                validate_service_contract(status, compatible_requirements(status, cfg['required_contract']))
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
        metadata = {}
        from .kuavo5_service_contract import CONFIGURABLE_SESSION_PROTOCOLS, validate_pinned_settings_contract
        if (status.get('session_contract') or {}).get('protocol') in CONFIGURABLE_SESSION_PROTOCOLS:
            error = None
            try:
                contract = validate_pinned_settings_contract(status,
                    expected_installation_sha256=cfg['installation_sha256'],
                    expected_bundle_sha256=cfg['bundle_sha256'], expected_profile=cfg['profile'])
            except ValueError as exc:
                error = str(exc)
            matches.update(profile=error is None, contract=error is None)
            metadata.update(connection_policy='pinned_release_v1', compatibility_error=error,
                profile_match_policy='fixed_installation_and_validated_runtime_settings')
            if error is None:
                settings = contract['session_settings']
                metadata['runtime_settings'] = dict(revision=settings['revision'],
                    settings_sha256=settings['settings_sha256'],
                    gripper_config_changed=contract['profile']['gripper_config_sha256'] !=
                                           cfg['profile']['gripper_config_sha256'])
        return dict(event='KUAVO_SERVICE_STATUS', service=status, expected_match=matches,
            management_ready=(all(v is True for v in matches.values()) and
                              status.get('management_protocol') == 'kuavo_service_requests_v1' and
                              isinstance(status.get('service_instance_id'), str) and
                              re.fullmatch('[0-9a-f]{32}', status['service_instance_id']) is not None),
            session_start_requested=False, stop_requested=False,
            physical_robot_command_sent=False, **metadata)

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
            confirm_stop=True, allow_validated_runtime_settings=True)

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
                    accept_unverified_gripper_bus=False, request_id=None,
                    expected_service_instance_id=None, expected_installation_sha256=None,
                    on_progress=None, expected_contract_sha256=None):
        """Same preparation requests for robot-local and PC callers.

        Observation does not require hardware readiness: a stopped controller
        is precisely when startup requests are useful. Identity/contract still
        have to match before any request is sent.

        A multi-step caller may pin both identities from its initial observation.
        A subsequent compatible release then still requires a new confirmation.
        """
        from .kuavo5_controller_admin import validate_preparation_request
        import secrets
        rid = secrets.token_hex(16) if request_id is None else request_id
        validate_preparation_request(dict(action=action, request_id=rid,
            expected_installation_sha256='0'*64, confirm_motion=confirm_motion,
            motor_ids_confirmed=motor_ids_confirmed,
            accept_unverified_gripper_bus=accept_unverified_gripper_bus))
        pinned = expected_service_instance_id is not None or expected_installation_sha256 is not None
        if pinned and (not isinstance(expected_service_instance_id, str)
                or re.fullmatch('[0-9a-f]{32}', expected_service_instance_id) is None
                or not valid_digest(expected_installation_sha256)):
            raise ValueError('both valid preparation service instance and installation identities required')
        if on_progress is not None and not callable(on_progress):
            raise ValueError('preparation progress callback must be callable')
        if expected_contract_sha256 is not None and not valid_digest(expected_contract_sha256):
            raise ValueError('valid expected session contract required')
        observed = self.get_status()
        if observed.get('management_ready') is not True:
            raise ValueError('compatible authenticated service required for preparation')
        status = observed['service']
        if pinned and (status.get('service_instance_id') != expected_service_instance_id
                or status.get('installation_sha256') != expected_installation_sha256):
            raise ValueError('preparation service identity changed; no request sent')
        progress = {} if on_progress is None else dict(on_progress=on_progress)
        if expected_contract_sha256 is not None:
            if (status.get('session_contract') or {}).get('contract_sha256') != expected_contract_sha256:
                raise ValueError('preparation settings changed; no request sent')
            progress['expected_contract_sha256'] = expected_contract_sha256
        return self._channel().preparation_request(action,
            expected_service_instance_id=expected_service_instance_id if pinned else status['service_instance_id'],
            expected_installation_sha256=expected_installation_sha256 if pinned else status['installation_sha256'],
            confirm_motion=confirm_motion, motor_ids_confirmed=motor_ids_confirmed,
            accept_unverified_gripper_bus=accept_unverified_gripper_bus,
            request_id=rid, **progress)

    def preparation_job(self, request_id, *, expected_service_instance_id,
                        expected_installation_sha256, on_progress=None):
        """Read a specific previous request; never re-issue preparation."""
        if on_progress is not None and not callable(on_progress):
            raise ValueError('preparation progress callback must be callable')
        progress = {} if on_progress is None else dict(on_progress=on_progress)
        return self._channel().query_preparation_job(request_id,
            expected_service_instance_id=expected_service_instance_id,
            expected_installation_sha256=expected_installation_sha256, **progress)

    def get_session_settings(self):
        """Read the shared robot settings, including whether editing is allowed."""
        return self._channel().settings_request('settings_get')

    def _settings(self, action, *, changes=None, expected_revision,
                  expected_service_instance_id=None, expected_installation_sha256=None,
                  request_id=None, confirm=False):
        from .kuavo5_service_contract import validate_settings_values
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('observed settings revision required')
        if action in ('settings_apply', 'settings_reset') and confirm is not True:
            raise ValueError('explicit settings update confirmation required')
        if changes is not None:
            changes = validate_settings_values(changes, partial=True)
        if (not isinstance(expected_service_instance_id, str)
                or re.fullmatch('[a-f0-9]{32}', expected_service_instance_id) is None
                or not valid_digest(expected_installation_sha256)):
            raise ValueError('both observed service and installation identities required')
        observed = self.get_status()
        if observed.get('management_ready') is not True:
            raise ValueError('compatible authenticated service required for settings')
        status = observed['service']
        if (status.get('service_instance_id') != expected_service_instance_id
                or status.get('installation_sha256') != expected_installation_sha256):
            raise ValueError('settings service changed; read settings again')
        kwargs = dict(expected_revision=expected_revision,
            expected_service_instance_id=expected_service_instance_id,
            expected_installation_sha256=expected_installation_sha256)
        if changes is not None:
            kwargs['changes'] = changes
        if action in ('settings_apply', 'settings_reset'):
            kwargs.update(request_id=request_id, confirm=confirm)
        return self._channel().settings_request(action, **kwargs)

    def preview_session_settings(self, changes, *, expected_revision, **identity):
        return self._settings('settings_preview', changes=changes,
                              expected_revision=expected_revision, **identity)

    def apply_session_settings(self, changes, *, expected_revision, confirm=False,
                               request_id=None, **identity):
        return self._settings('settings_apply', changes=changes, expected_revision=expected_revision,
                              confirm=confirm, request_id=request_id, **identity)

    def reset_session_settings(self, *, expected_revision, confirm=False, request_id=None, **identity):
        return self._settings('settings_reset', expected_revision=expected_revision,
                              confirm=confirm, request_id=request_id, **identity)


def settings_cli_options(args):
    """Pure parsing shared by PC and SSH-local tools, before any connection."""
    from .kuavo5_service_contract import validate_settings_values
    action = next((name for chosen, name in (
        (args.settings, 'get'), (args.preview_settings is not None, 'preview'),
        (args.apply_settings is not None, 'apply'), (args.reset_settings, 'reset')) if chosen), None)
    if action is None:
        if args.expected_settings_revision is not None or args.confirm_settings:
            raise ValueError('settings revision/confirmation requires a settings operation')
        return None
    if action == 'get':
        if (args.expected_settings_revision is not None or args.confirm_settings
                or args.expected_service_instance_id is not None or args.expected_installation_sha256 is not None):
            raise ValueError('settings query accepts no revision or confirmation')
        return ('get_session_settings', (), {})
    if type(args.expected_settings_revision) is not int or args.expected_settings_revision < 0:
        raise ValueError('settings preview/update requires --expected-settings-revision from a fresh query')
    kwargs = dict(expected_revision=args.expected_settings_revision)
    if (not isinstance(args.expected_service_instance_id, str)
            or re.fullmatch('[a-f0-9]{32}', args.expected_service_instance_id) is None
            or not valid_digest(args.expected_installation_sha256)):
        raise ValueError('settings require --expected-service-instance-id and --expected-installation-sha256 from the same query')
    kwargs.update(expected_service_instance_id=args.expected_service_instance_id,
                  expected_installation_sha256=args.expected_installation_sha256)
    if action in ('apply', 'reset'):
        if args.confirm_settings is not True:
            raise ValueError('settings writes require --confirm-settings')
        kwargs['confirm'] = True
    elif args.confirm_settings:
        raise ValueError('settings preview does not apply changes')
    positional = ()
    if action in ('preview', 'apply'):
        raw = args.preview_settings if action == 'preview' else args.apply_settings
        if len(raw) > 4096:
            raise ValueError('settings request too large')
        changes = json.loads(raw, object_pairs_hook=_strict_object)
        positional = (validate_settings_values(changes, partial=True),)
    return (action+'_session_settings', positional, kwargs)
