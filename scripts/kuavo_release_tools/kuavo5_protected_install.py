#!/usr/bin/env python3
"""Pinned, additive robot installation. Never starts/enables any robot program.

Default --check reads a package and filesystem metadata. --install additionally
requires root and --confirm-install. No SSH, ROS, CAN or systemctl call exists.
"""
import argparse
import ast
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys

MAX_PACKAGE_BYTES = 4_194_304
MAX_FILE_BYTES = 1_048_576
SERVICE_FILES = frozenset((
    'apps/kuavo5_session_service.py', 'apps/kuavo5_service_request.py',
    'apps/kuavo5_gripper_worker.py', 'src/teleop/__init__.py',
    'src/teleop/robots/__init__.py',
    'src/teleop/robots/kuavo5_execution_lock.py',
    'src/teleop/robots/kuavo5_network_config.py',
    'src/teleop/robots/kuavo5_network_wire.py',
    'src/teleop/robots/kuavo5_session_worker.py',
    'src/teleop/robots/kuavo5_session_service.py',
    'src/teleop/robots/kuavo5_controller_admin.py',
    'src/teleop/robots/kuavo5_session_profile.py',
    'src/teleop/robots/kuavo5_service_contract.py',
    'src/teleop/robots/kuavo5_session_settings.py',
    'src/teleop/robots/kuavo5_session_settings_store.py',
    'src/teleop/robots/kuavo5_return_config.py',
    'src/teleop/robots/kuavo5_direct_channel.py',
    'src/teleop/robots/kuavo5_service_client.py',
    'apps/kuavo5_robot_preparation_helper.py',
    'src/teleop/control/__init__.py',
    'src/teleop/control/kuavo5_gripper_calibration.py',
    'src/teleop/robots/damiao_gripper.py',
    'src/teleop/robots/kuavo5_gripper_live.py',
    'src/teleop/robots/kuavo5_gripper_reference.py',
    'src/teleop/robots/kuavo5_gripper_coordinates.py',
    'src/teleop/robots/kuavo5_gripper_preparation.py',
    'src/teleop/robots/kuavo5_robot_preparation.py',
    'src/teleop/robots/kuavo5_preparation_profile.py',
))
ACCESS_FILES = frozenset(('token', 'server.crt', 'server.key', 'client.json'))
PACKAGE_FIELDS = frozenset(('version', 'release_id', 'installation_sha256',
    'files', 'access', 'enable_gripper_sessions', 'accept_unverified_gripper_bus'))
MANIFEST_FIELDS = frozenset(('format_version', 'bundle_sha256', 'profile',
    'core_file_sha256', 'service_file_sha256', 'physical_execution_verified'))
PROFILE = dict(target_recovery='bounded_candidate', speed_profile='responsive',
               arm_posture='elbow_preference', gripper_mode='damiao')
SAFE_ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}
TOOLS = ('/usr/bin/python3', '/usr/bin/sudo', '/usr/bin/openssl',
         '/usr/sbin/visudo', '/usr/bin/systemd-analyze', '/usr/bin/g++')
RUNTIME_PATHS = (
    '/home/lab/kuavo-ros-control/devel/lib/python3/dist-packages/kuavo_msgs',
    '/opt/ros/noetic/lib/python3/dist-packages/rospy',
    '/opt/ros/noetic/lib/python3/dist-packages/rosgraph',
    '/opt/ros/noetic/lib/python3/dist-packages/rosservice',
    '/opt/ros/noetic/lib/python3/dist-packages/rosnode',
    '/opt/ros/noetic/lib/python3/dist-packages/geometry_msgs',
    '/opt/ros/noetic/lib/python3/dist-packages/nav_msgs',
    '/opt/ros/noetic/lib/python3/dist-packages/std_srvs',
    '/home/lab/kuavo-ros-control/src/manipulation_nodes/motion_capture_ik/include/plantIK.h',
    '/home/lab/kuavo-ros-control/src/manipulation_nodes/motion_capture_ik/include/motion_capture_ik/json.hpp',
    '/home/lab/kuavo-ros-control/src/manipulation_nodes/motion_capture_ik/lib/libplantIK.so',
    '/home/lab/kuavo-ros-control/src/kuavo_assets/config/kuavo_v63/kuavo.json',
    '/home/lab/kuavo-ros-control/devel/lib/motion_capture_ik/arms_ik_node',
    '/opt/drake/lib/libdrake.so', '/opt/drake/include', '/opt/drake/share',
    '/usr/include/eigen3',
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def parse(raw):
    return json.loads(raw, object_pairs_hook=strict_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON value')))


def get_paths(package):
    release = package.get('release_id')
    if not isinstance(release, str) or re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}', release) is None:
        raise ValueError('generated release identity required')
    state = '/var/lib/galaxis-kuavo-network/releases/' + release
    return dict(code='/opt/galaxis/kuavo-network/releases/' + release,
        state=state, access=state + '/access',
        unit='/etc/systemd/system/galaxis-kuavo5-joint-' + release + '.service',
        sudoers='/etc/sudoers.d/kuavo-network-' + release)


def decode_files(value, expected, limit):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('exact file whitelist required')
    result = {}
    for name, encoded in value.items():
        if not isinstance(encoded, str) or len(encoded) > (limit + 2) * 4 // 3:
            raise ValueError('invalid or oversized encoded file')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise ValueError('invalid base64 file') from None
        if not data or len(data) > limit:
            raise ValueError('empty or oversized file')
        result[name] = data
    return result


def checked_return(document):
    """Mirror the passive return-config contract without executing package code."""
    limits = {'max_velocity_rad_s': (.05, .3), 'max_acceleration_rad_s2': (.1, .6),
        'timeout_s': (5., 120.), 'position_tolerance_rad': (.005, .03),
        'quiet_velocity_rad_s': (.005, .05), 'settle_seconds': (.2, 2.)}
    fixed = dict(schema_version=1, pose_source='robot_init_state_param',
                 gripper_target='session_initial_feedback')
    if not isinstance(document, dict) or set(document) != set(fixed) | set(limits):
        raise ValueError('exact return-home configuration required')
    if type(document['schema_version']) is not int or any(document[k] != v for k, v in fixed.items()):
        raise ValueError('invalid return-home source or schema')
    result = dict(fixed)
    for key, (low, high) in limits.items():
        value = document[key]
        if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('return-home parameter outside permitted range')
        result[key] = float(value)
    speed, acceleration = result['max_velocity_rad_s'], result['max_acceleration_rad_s2']
    if .06 + speed * speed / (2 * acceleration) + speed * .02 >= .2:
        raise ValueError('return-home lacks braking reserve')
    return result


def command(runner, argv, *, data=None, timeout=15):
    """No shell. Never echo stderr because it may concern credential bytes."""
    result = runner(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=timeout, env=dict(SAFE_ENV))
    if result.returncode:
        raise ValueError('installation verification command failed: ' + Path(argv[0]).name)
    return result.stdout


def validate_package(raw, pin, *, runner=subprocess.run):
    if not isinstance(raw, bytes) or len(raw) > MAX_PACKAGE_BYTES or not valid_digest(pin) or digest(raw) != pin:
        raise ValueError('package SHA256 mismatch or size limit exceeded')
    package = parse(raw)
    if not isinstance(package, dict) or set(package) not in (PACKAGE_FIELDS, PACKAGE_FIELDS | {'enable_robot_preparation'}):
        raise ValueError('exact package fields required')
    if type(package['version']) is not int or package['version'] != 1:
        raise ValueError('unsupported package schema')
    paths = get_paths(package)
    for flag in ('enable_gripper_sessions', 'accept_unverified_gripper_bus'):
        if type(package[flag]) is not bool:
            raise ValueError('explicit boolean installation permission required')
    if package['accept_unverified_gripper_bus'] and not package['enable_gripper_sessions']:
        raise ValueError('bus permission requires enabled gripper sessions')
    if type(package.get('enable_robot_preparation', False)) is not bool:
        raise ValueError('explicit boolean preparation permission required')
    if package.get('enable_robot_preparation') and not package['enable_gripper_sessions']:
        raise ValueError('robot preparation requires enabled combined sessions')
    files = decode_files(package['files'], SERVICE_FILES | {'manifest.json', 'core.json'}, MAX_FILE_BYTES)
    if len(files['manifest.json']) > 16_384 or not valid_digest(package['installation_sha256']) \
            or digest(files['manifest.json']) != package['installation_sha256']:
        raise ValueError('installation manifest hash or size mismatch')
    manifest = parse(files['manifest.json'])
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS \
            or type(manifest['format_version']) is not int or manifest['format_version'] != 1 \
            or manifest['physical_execution_verified'] is not False:
        raise ValueError('exact unverified installation manifest required')
    hashes = manifest['service_file_sha256']
    if not isinstance(hashes, dict) or set(hashes) != SERVICE_FILES:
        raise ValueError('exact service manifest required')
    for name in SERVICE_FILES:
        if not valid_digest(hashes[name]) or digest(files[name]) != hashes[name]:
            raise ValueError('service file hash mismatch: ' + name)
        compile(files[name], name, 'exec')
    if digest(files['core.json']) != manifest['core_file_sha256']:
        raise ValueError('core file hash mismatch')
    core = parse(files['core.json'])
    extra = {'gripper_config', 'gripper_config_sha256'}
    if isinstance(core, dict) and 'base_mode' in core:
        extra.add('base_mode')
        if core['base_mode'] not in ('disabled', 'vendor_vr_x'):
            raise ValueError('unsupported installed base mode')
        if core['base_mode'] == 'vendor_vr_x' and 'return_home_config' not in core:
            raise ValueError('integrated base requires the return-home coordinator')
    if isinstance(core, dict) and ('return_home_config' in core or 'return_home_config_sha256' in core):
        extra |= {'return_home_config', 'return_home_config_sha256'}
    if not isinstance(core, dict) or set(core) != {'mode', 'sources', 'sha256', *PROFILE} | extra \
            or core['mode'] != 'validate' or any(core[k] != v for k, v in PROFILE.items()):
        raise ValueError('exact saved combined profile required')
    sources = core['sources']
    if not isinstance(sources, dict) or not sources or any(
            not isinstance(k, str) or not re.fullmatch(r'teleop(?:\.[A-Za-z_][A-Za-z_0-9]*)+', k)
            or not isinstance(v, str) for k, v in sources.items()):
        raise ValueError('invalid core source manifest')
    source_hash = digest(json.dumps(sources, sort_keys=True).encode())
    if source_hash != core['sha256'] or source_hash != manifest['bundle_sha256']:
        raise ValueError('core source hash mismatch')
    profile = dict(PROFILE)
    if 'base_mode' in core:
        profile['base_mode'] = core['base_mode']
    grippers = core['gripper_config']
    if not isinstance(grippers, dict):
        raise ValueError('fixed gripper configuration required')
    owner_directory = grippers.get('owner_directory')
    preparation = grippers.get('preparation', {})
    if not isinstance(owner_directory, str) or not Path(owner_directory).is_absolute() \
            or '..' in Path(owner_directory).parts or not isinstance(preparation, dict):
        raise ValueError('fixed gripper owner path and preparation configuration required')
    gripper_hash = digest(json.dumps(grippers, sort_keys=True, allow_nan=False).encode())
    if gripper_hash != core['gripper_config_sha256']:
        raise ValueError('gripper configuration hash mismatch')
    profile['gripper_config_sha256'] = gripper_hash
    if package['enable_gripper_sessions'] and preparation.get('shared_bus_compatibility_verified') is False \
            and not package['accept_unverified_gripper_bus']:
        raise ValueError('explicit acceptance of unverified shared gripper bus required')
    if 'return_home_config' in core:
        config = checked_return(core['return_home_config'])
        return_hash = digest(json.dumps(config, sort_keys=True, allow_nan=False).encode())
        if return_hash != core['return_home_config_sha256']:
            raise ValueError('return-home configuration hash mismatch')
        profile['return_home_config_sha256'] = return_hash
    if profile != manifest['profile']:
        raise ValueError('manifest profile mismatch')
    access = decode_files(package['access'], ACCESS_FILES, 32_768)
    if re.fullmatch(rb'[a-f0-9]{64}\n?', access['token']) is None:
        raise ValueError('private token must contain 64 lowercase hexadecimal characters')
    if len(access['client.json']) > 16_384:
        raise ValueError('robot client configuration too large')
    client = parse(access['client.json'])
    required_client = {'network_host', 'network_port', 'network_token_file',
        'network_cert_sha256', 'bundle_sha256', 'installation_sha256', 'profile'}
    if not isinstance(client, dict) or set(client) != required_client \
            or client['network_host'] != '192.168.26.1' or type(client['network_port']) is not int \
            or client['network_port'] != 7443 or client['network_token_file'] != paths['access'] + '/token' \
            or client['bundle_sha256'] != source_hash or client['profile'] != profile \
            or client['installation_sha256'] != package['installation_sha256']:
        raise ValueError('robot client endpoint, paths or identity mismatch')
    der = command(runner, ['/usr/bin/openssl', 'x509', '-outform', 'DER'], data=access['server.crt'])
    certificate_key = command(runner, ['/usr/bin/openssl', 'x509', '-pubkey', '-noout'], data=access['server.crt'])
    private_key = command(runner, ['/usr/bin/openssl', 'pkey', '-pubout'], data=access['server.key'])
    if not der or certificate_key != private_key or digest(der) != client['network_cert_sha256']:
        raise ValueError('TLS certificate, private key or client pin mismatch')
    return package


def manifest_for(package):
    return parse(base64.b64decode(package['files']['manifest.json'], validate=True))


def render_unit(package):
    paths = get_paths(package)
    pin = manifest_for(package)['bundle_sha256']
    if not valid_digest(pin):
        raise ValueError('fixed bundle digest required')
    extra = ''
    if package['enable_gripper_sessions']:
        extra = ' --enable-arm-sessions --enable-gripper-sessions'
        if package['accept_unverified_gripper_bus']:
            extra += ' --accept-unverified-gripper-bus'
        if package.get('enable_robot_preparation'):
            extra += ' --enable-robot-preparation'
    code, access = paths['code'], paths['access']
    return ('[Unit]\nDescription=Kuavo reviewed joint session ' + package['release_id'] + '\nAfter=network.target\n\n'
        '[Service]\nType=simple\nUser=lab\nGroup=lab\nWorkingDirectory=' + code + '\n'
        'Environment=HOME=/home/lab\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'
        'Environment=PYTHONUNBUFFERED=1\nUMask=0077\n'
        'ExecStart=/usr/bin/python3 -I -B ' + code + '/apps/kuavo5_session_service.py --serve'
        ' --host 192.168.26.1 --port 7443 --wait-network --bundle ' + code + '/core.json'
        ' --bundle-sha256 ' + pin + ' --token-file ' + access + '/token'
        ' --tls-cert ' + access + '/server.crt --tls-key ' + access + '/server.key' + extra + '\n'
        'Restart=no\nKillMode=process\nKillSignal=SIGTERM\nSendSIGKILL=no\n'
        'TimeoutStopSec=infinity\nPrivateTmp=false\nNoNewPrivileges=false\n'
        'StandardOutput=journal\nStandardError=journal\n\n'
        '[Install]\nWantedBy=multi-user.target\n')


def render_sudoers(package):
    launcher = get_paths(package)['code'] + '/apps/kuavo5_gripper_worker.py'
    rule = ('# Only reviewed no-argument helpers; no shell or arbitrary Python.\n'
        'lab ALL=(root) NOPASSWD: /usr/bin/python3 -I -B ' + launcher + '\n')
    if package.get('enable_robot_preparation'):
        rule += ('lab ALL=(root) NOPASSWD: /usr/bin/python3 -I -B ' +
                 get_paths(package)['code'] + '/apps/kuavo5_robot_preparation_helper.py\n')
    return rule


def preparation_dependencies(package, fs):
    """Read pinned dependencies as data; never execute code from the package."""
    if not package.get('enable_robot_preparation'):
        return {}
    raw = base64.b64decode(package['files']['src/teleop/robots/kuavo5_preparation_profile.py'], validate=True)
    constants = {}
    for node in ast.parse(raw).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise ValueError('preparation profile must contain literal constants only')
        name = node.targets[0].id
        if name in constants:
            raise ValueError('duplicate preparation constant')
        constants[name] = ast.literal_eval(node.value)
    required = {'CONTROLLER_ROOT', 'CONTROLLER_RUNTIME_SHA256', 'CONTROLLER_CONFIG_SHA256',
                'CONTROLLER_LAUNCHER_SHA256', 'VENDOR_PINS', 'STATE_DIRECTORY'}
    if set(constants) != required or constants['STATE_DIRECTORY'] != '/var/lib/galaxis-kuavo-preparation':
        raise ValueError('exact preparation dependency profile required')
    root = constants['CONTROLLER_ROOT']
    if not isinstance(root, str) or re.fullmatch('/opt/galaxis/kuavo-controller/releases/[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}', root) is None:
        raise ValueError('protected controller dependency path required')
    dependencies = {}
    for filename, key in (('controller_runtime.py', 'CONTROLLER_RUNTIME_SHA256'),
                          ('controller-config.json', 'CONTROLLER_CONFIG_SHA256'),
                          ('operator_deployment.py', 'CONTROLLER_LAUNCHER_SHA256')):
        pin = constants[key]
        if not valid_digest(pin):
            raise ValueError('invalid controller dependency digest')
        name = root + '/' + filename
        fs.protected_parents(root)
        path = fs.path(name)
        try:
            info = path.lstat()
            valid = stat.S_ISREG(info.st_mode) and fs.uid(path) == 0 and not info.st_mode & 0o022
            raw = path.read_bytes() if valid else b''
            dependencies[name] = valid and digest(raw) == pin
        except OSError:
            dependencies[name] = False
    # The helper independently rechecks these identities at each request.
    state = constants['STATE_DIRECTORY']
    fs.protected_parents(state)
    if fs.path(state).exists() and stat.S_IMODE(fs.path(state).stat().st_mode) != 0o700:
        raise ValueError('preparation state directory must already be root-private (0700)')
    dependencies['preparation_state_location'] = True
    return dependencies


class InstallFilesystem:
    """CLI always uses /. Tests inject a temporary root and simulated ownership."""
    def __init__(self, root=Path('/'), *, owner=None, chown=os.chown):
        self.root = Path(root).absolute()
        self.owner = owner
        self.chown = chown

    def path(self, absolute):
        value = Path(absolute)
        if not value.is_absolute() or '..' in value.parts:
            raise ValueError('fixed absolute filesystem path required')
        return self.root.joinpath(*value.parts[1:])

    def uid(self, path):
        return path.lstat().st_uid if self.owner is None else self.owner(path)

    def ancestors(self, target):
        path = self.path(target)
        result = []
        while True:
            result.append(path)
            if path == self.root:
                return reversed(result)
            path = path.parent

    def protected_parents(self, target, *, operator_traversable=False):
        for path in self.ancestors(target):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or self.uid(path) != 0 or info.st_mode & 0o022:
                raise ValueError('unprotected or symlink installation ancestor: ' + str(path))
            # A lab process that matches the directory's group does not fall
            # back to "other" permission bits. Require both classes to traverse
            # rather than assuming this root-owned ancestor's group identity.
            if operator_traversable and info.st_mode & 0o011 != 0o011:
                raise ValueError('installation ancestor is not traversable by lab: ' + str(path))

    def mkdirs(self, target, mode=0o755):
        self.protected_parents(target)
        for path in self.ancestors(target):
            if not path.exists():
                path.mkdir(mode=mode)
                self.chown(path, 0, 0)
                os.chmod(path, mode)
            self.protected_parents('/' + str(path.relative_to(self.root)))

    def write_new(self, absolute, data, mode=0o644):
        path = self.path(absolute)
        self.protected_parents(str(Path(absolute).parent))
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            self.chown(path, 0, 0)
        finally:
            os.close(fd)


SETTINGS_DIRECTORY = '/var/lib/galaxis-kuavo-network/runtime-settings'


def check_settings_directory(fs, account):
    """One private DATA directory; formal program files remain root-owned."""
    fs.protected_parents(str(Path(SETTINGS_DIRECTORY).parent), operator_traversable=True)
    path = fs.path(SETTINGS_DIRECTORY)
    if os.path.lexists(path):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or fs.uid(path) != account.pw_uid
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('session settings directory must be private and owned by lab')


def prepare_settings_directory(fs, account):
    check_settings_directory(fs, account)
    path = fs.path(SETTINGS_DIRECTORY)
    if not path.exists():
        fs.mkdirs(str(Path(SETTINGS_DIRECTORY).parent))
        path.mkdir(mode=0o700)
        fs.chown(path, account.pw_uid, account.pw_gid)
    # Existing parameter data is never removed, migrated or overwritten here.


def targets_unused(package, fs):
    paths = get_paths(package)
    for key in ('code', 'state', 'unit', 'sudoers'):
        target = fs.path(paths[key])
        if os.path.lexists(target):
            raise ValueError('installation target already exists; preserve and choose a new release: ' + paths[key])
        fs.protected_parents(str(Path(paths[key]).parent), operator_traversable=key in ('code', 'state'))
    return paths


def inspect_environment(package, *, fs=None, account=None):
    fs = fs or InstallFilesystem()
    paths = targets_unused(package, fs)
    if account is None:
        try:
            account = pwd.getpwnam('lab')
        except KeyError:
            account = None
    requirements = dict(linux=sys.platform.startswith('linux'), python38_or_newer=sys.version_info >= (3, 8),
        operator_account=(account is not None and account.pw_uid > 0 and account.pw_dir == '/home/lab'))
    if requirements['operator_account']:
        check_settings_directory(fs, account)
    requirements.update(preparation_dependencies(package, fs))
    dependencies = []
    for name in TOOLS + RUNTIME_PATHS:
        path = fs.path(name)
        exists = path.exists()
        info = path.stat() if exists else None
        dependencies.append(dict(path=name, exists=exists,
            uid=None if info is None else fs.uid(path),
            mode=None if info is None else oct(stat.S_IMODE(info.st_mode)),
            symlink=path.is_symlink()))
        requirements[name] = exists
    core = parse(base64.b64decode(package['files']['core.json'], validate=True))
    owner_directory = core['gripper_config'].get('owner_directory')
    owner_metadata = []
    if isinstance(owner_directory, str) and Path(owner_directory).is_absolute():
        for name in ('canbus0.sock', 'canbus1.sock'):
            path = fs.path(str(Path(owner_directory) / name))
            try:
                info = path.lstat()
                owner_metadata.append(dict(path=str(Path(owner_directory) / name), exists=True,
                    socket=stat.S_ISSOCK(info.st_mode), uid=fs.uid(path), mode=oct(stat.S_IMODE(info.st_mode))))
            except OSError as exc:
                owner_metadata.append(dict(path=str(Path(owner_directory) / name),
                    exists=False if isinstance(exc, FileNotFoundError) else None,
                    metadata_read_error=type(exc).__name__))
    return dict(event='KUAVO_PROTECTED_INSTALL_CHECKED', ready_for_install=all(requirements.values()),
        requirements=requirements, dependencies=dependencies, paths=paths,
        gripper_owner_metadata=owner_metadata, hardware_preparation_verified=False,
        external_vendor_dependencies_require_trust=True, files_written=False,
        listener_started=False, worker_started=False, physical_robot_command_sent=False)


def install_package(raw, pin, *, confirm_install=False, fs=None, runner=subprocess.run,
                    account=None, identity=None):
    if confirm_install is not True:
        raise ValueError('explicit --confirm-install required')
    uid, euid = (os.getuid(), os.geteuid()) if identity is None else identity
    if (uid, euid) != (0, 0):
        raise PermissionError('root installation required')
    package = validate_package(raw, pin, runner=runner)
    fs = fs or InstallFilesystem()
    account = pwd.getpwnam('lab') if account is None else account
    check = inspect_environment(package, fs=fs, account=account)
    if not check['ready_for_install']:
        raise ValueError('robot installation dependencies unavailable; no files written')
    paths = check['paths']
    files = decode_files(package['files'], SERVICE_FILES | {'manifest.json', 'core.json'}, MAX_FILE_BYTES)
    access = decode_files(package['access'], ACCESS_FILES, 32_768)
    # All package, target and permission checks precede writes. New artifacts
    # remain after any failure for inspection; no existing file is replaced.
    prepare_settings_directory(fs, account)
    if package.get('enable_robot_preparation'):
        fs.mkdirs('/var/lib/galaxis-kuavo-preparation', mode=0o700)
    fs.mkdirs(paths['code'])
    for name in sorted(files):
        target = paths['code'] + '/' + name
        fs.mkdirs(str(Path(target).parent))
        fs.write_new(target, files[name])
    fs.mkdirs(paths['state'])
    fs.mkdirs(paths['access'])
    os.chmod(fs.path(paths['access']), 0o700)
    for name in sorted(access):
        fs.write_new(paths['access'] + '/' + name, access[name], 0o600)
    # Keep the directory root-owned while writing its contents to avoid a lab
    # process replacing it between root's file creations.
    for name in sorted(access):
        fs.chown(fs.path(paths['access'] + '/' + name), account.pw_uid, account.pw_gid)
    fs.chown(fs.path(paths['access']), account.pw_uid, account.pw_gid)
    support = paths['code'] + '/install-support'
    fs.mkdirs(support)
    staged_unit = support + '/' + Path(paths['unit']).name
    staged_rule = support + '/worker.sudoers'
    unit, rule = render_unit(package).encode(), render_sudoers(package).encode()
    fs.write_new(staged_unit, unit)
    fs.write_new(staged_rule, rule, 0o440)
    command(runner, ['/usr/sbin/visudo', '-cf', str(fs.path(staged_rule))])
    command(runner, ['/usr/bin/systemd-analyze', 'verify', str(fs.path(staged_unit))])
    pin = manifest_for(package)['bundle_sha256']
    command(runner, ['/usr/bin/sudo', '-n', '-H', '-u', 'lab', '/usr/bin/python3', '-I', '-B',
        str(fs.path(paths['code'] + '/apps/kuavo5_session_service.py')), '--check-installation',
        '--bundle', str(fs.path(paths['code'] + '/core.json')), '--bundle-sha256', pin])
    fs.mkdirs(str(Path(paths['unit']).parent))
    fs.mkdirs(str(Path(paths['sudoers']).parent))
    fs.write_new(paths['unit'], unit)
    fs.write_new(paths['sudoers'], rule, 0o440)  # last privileged integration step
    return dict(event='KUAVO_PROTECTED_INSTALLED_NOT_STARTED', paths=paths,
        installation_sha256=package['installation_sha256'], bundle_sha256=pin,
        unit_sha256=digest(unit), sudoers_sha256=digest(rule),
        files_written=True, service_enabled=False, daemon_reloaded=False,
        listener_started=False, worker_started=False, physical_robot_command_sent=False)


def read_package(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('private regular package file required (0600 or 0400)')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_PACKAGE_BYTES + 1)
        if len(raw) > MAX_PACKAGE_BYTES:
            raise ValueError('package too large')
        return raw
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--package-sha256', required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check', action='store_true')
    action.add_argument('--install', action='store_true')
    parser.add_argument('--confirm-install', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.confirm_install and not args.install:
            raise ValueError('--confirm-install only applies to --install')
        raw = read_package(args.package)
        if args.install:
            result = install_package(raw, args.package_sha256, confirm_install=args.confirm_install)
        else:
            package = validate_package(raw, args.package_sha256)
            result = inspect_environment(package)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if args.install or result['ready_for_install'] else 1
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps(dict(event='KUAVO_PROTECTED_INSTALL_ERROR', reason=str(exc),
            listener_started=False, worker_started=False, physical_robot_command_sent=False)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
