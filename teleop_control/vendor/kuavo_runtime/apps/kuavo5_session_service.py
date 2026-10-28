#!/usr/bin/env python3
"""Operator-started direct-session listener; default is an inert dry run.

Starting this listener never starts the manufacturer controller. Explicit,
authenticated preparation requests may use the separately installed manager.
"""
from __future__ import annotations
import argparse
import ast
import errno
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import signal
import stat
import re
import ssl
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
OWNER_LEDGER = Path('/tmp/kuavo5-arms-network-owner.json')
# The bootstrap is deliberately stdlib-only. Validate every installed Python
# file before importing anything from the mutable installation's src directory.
FIXED_PROFILE = dict(target_recovery='bounded_candidate', speed_profile='responsive',
                     arm_posture='elbow_preference', gripper_mode='off')
SERVICE_FILES = frozenset((
    'src/teleop/robots/kuavo5_manual_control.py',
    'src/teleop/robots/kuavo5_manual_runtime.py',
    'src/teleop/robots/kuavo5_manual_worker.py',

    'apps/kuavo5_session_service.py',
    'apps/kuavo5_service_request.py',
    'src/teleop/__init__.py', 'src/teleop/robots/__init__.py',
    'src/teleop/robots/kuavo5_execution_lock.py',
    'src/teleop/robots/kuavo5_network_config.py',
    'src/teleop/robots/kuavo5_network_wire.py',
    'src/teleop/robots/kuavo5_session_worker.py',
    'src/teleop/robots/kuavo5_session_service.py',
    'src/teleop/robots/kuavo5_controller_admin.py',
    'src/teleop/robots/kuavo5_session_profile.py',
    'src/teleop/robots/kuavo5_session_settings.py',
    'src/teleop/robots/kuavo5_session_settings_store.py',
    'src/teleop/robots/kuavo5_service_contract.py',
    'src/teleop/robots/kuavo5_return_config.py',
    'src/teleop/robots/kuavo5_direct_channel.py',
    'src/teleop/robots/kuavo5_service_client.py',
    'apps/kuavo5_gripper_worker.py',
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
MANIFEST_FIELDS = frozenset(('format_version', 'bundle_sha256', 'profile',
    'core_file_sha256', 'service_file_sha256', 'physical_execution_verified'))
MAX_CORE_BYTES = 1_048_576
# These are populated only after installation validation. Explicit placeholders
# also let offline tests substitute fake listeners without loading ROS.
read_auth_key = SessionWorker = OwnerLedger = SessionSupervisor = SessionNetworkService = None


def validate_numeric_host(host):
    if not isinstance(host, str) or host != host.strip():
        raise ValueError('numeric IPv4 interface required')
    return str(ipaddress.IPv4Address(host))


def startup_maintenance(requested=False, *, environment=None):
    """Publication-only startup gate; ordinary boots have no environment flag.

    The publisher uses a temporary systemd drop-in to set this flag for the
    first start, then removes it without restarting this listener. Explicit
    leave-maintenance is still required after publishing and releasing locks.
    """
    if type(requested) is not bool:
        raise ValueError('explicit startup maintenance boolean required')
    env = os.environ if environment is None else environment
    value = env.get('KUAVO_START_IN_MAINTENANCE', '0')
    if value not in ('0', '1'):
        raise ValueError('KUAVO_START_IN_MAINTENANCE must be 0 or 1')
    return requested or value == '1'


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON installation field')
        value[key] = item
    return value


def _read_installation_file(root, relative, limit):
    if relative not in SERVICE_FILES | {'manifest.json', 'core.json'}:
        raise ValueError('unexpected installation path')
    path = root / relative
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError('installation symlinks are not supported: ' + relative)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('regular installation file required: ' + relative)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('installation file exceeds size limit: ' + relative)
        return raw
    finally:
        os.close(fd)


def _valid_digest(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def _bundle_profile(bundle):
    """Use this installation's checked, passive configuration validators."""
    spec = importlib.util.spec_from_file_location('checked_session_profile', ROOT/'src/teleop/robots/kuavo5_session_profile.py')
    profiles = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profiles)
    return_spec = importlib.util.spec_from_file_location('checked_return_config', ROOT/'src/teleop/robots/kuavo5_return_config.py')
    returns = importlib.util.module_from_spec(return_spec)
    return_spec.loader.exec_module(returns)
    return profiles.bundle_profile(bundle, return_config_validator=returns.load_return_config)


def check_settings_core(bundle):
    """A service-only publication must not advertise knobs the old core ignores."""
    if bundle.get('gripper_mode') != 'damiao':
        return
    tree = ast.parse(bundle.get('sources', {}).get('teleop.robots.kuavo5_live_agent', ''))
    policies = [node.value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'LIVE_NUMERIC_SPEED_POLICY'
                        for target in node.targets)]
    if len(policies) != 1 or ast.literal_eval(policies[0]) != 'bounded_1p0_10_v1':
        raise ValueError('numeric speed policy requires a rebuilt 1.0/10 core')
    acceleration_policies = [node.value for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'LIVE_NUMERIC_ACCELERATION_POLICY'
                for target in node.targets)]
    if len(acceleration_policies) != 1 or ast.literal_eval(acceleration_policies[0]) != 'independent_bounded_10_v1':
        raise ValueError('independent acceleration policy requires a rebuilt compatible motion core')
    if not any(isinstance(node, ast.Assign) and any(isinstance(t, ast.Name)
        and t.id == 'LIVE_PER_ARM_MOTION_POLICY' for t in node.targets)
        and ast.literal_eval(node.value) == 'per_arm_scale_speed_acceleration_v1' for node in tree.body):
        raise ValueError('per-arm motion settings require a rebuilt compatible core')
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    required = {'run_robot': {'arm_velocity_rad_s', 'arm_acceleration_rad_s2', 'arm_motion_parameters', 'settings_identity'},
                'motion_profile': {'arm_velocity_rad_s', 'arm_acceleration_rad_s2'},
                'arm_motion_profile': {'arm_motion_parameters'},
                'make_live_controller': {'arm_velocity_rad_s', 'arm_acceleration_rad_s2', 'arm_motion_parameters'}}
    for name, parameters in required.items():
        node = functions.get(name)
        arguments = set() if node is None else {a.arg for a in node.args.args+node.args.kwonlyargs}
        if not parameters <= arguments:
            raise ValueError('session-settings capable motion core required; rebuild the reviewed core, '
                             'not a service-only export of the older core: '+name)


def check_mobile_base_core(bundle):
    """An installed capability must be backed by the actual reviewed motion core."""
    if bundle.get('base_mode', 'disabled') != 'vendor_vr_x':
        return
    sources = bundle.get('sources', {})
    required = {'teleop.contracts.base_motion', 'teleop.retargeting.mobile_base',
        'teleop.control.kuavo5_base_session', 'teleop.control.kuavo5_mobile_return',
        'teleop.robots.kuavo5_base_ros', 'teleop.robots.kuavo5_mobile_session',
        'teleop.robots.kuavo5_base_lock',
        'teleop.robots.kuavo5_live_agent'}
    if not required <= set(sources):
        raise ValueError('integrated base requires its complete motion-core dependencies')
    tree = ast.parse(sources['teleop.robots.kuavo5_live_agent'])
    policies = [node.value for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'LIVE_MOBILE_BASE_POLICY' for t in node.targets)]
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'run_robot']
    if (len(policies) != 1 or ast.literal_eval(policies[0]) != 'owned_body_relative_v1'
            or len(functions) != 1 or 'base_mode' not in {
                arg.arg for arg in functions[0].args.args+functions[0].args.kwonlyargs}):
        raise ValueError('integrated base requires a rebuilt compatible motion core')
    buttons = ast.parse(sources.get('teleop.robots.kuavo5_return_config', ''))
    declarations = [node.value for node in buttons.body if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'RETURN_BUTTONS' for t in node.targets)]
    if len(declarations) != 1 or ast.literal_eval(declarations[0]) != {'request': 'left_y', 'cancel': 'left_y'}:
        raise ValueError('integrated base requires Y-toggle return behavior')


def check_gripper_coordinate_core(bundle):
    """Reject mixed boot-coordinate integrations without executing candidate code.

    This checks the integration contract, not a firmware strategy or one frozen
    source hash. Configurations without a reference policy retain their existing
    behavior; future policy implementations may change their internal algorithm.
    """
    document = bundle.get('gripper_config') or {}
    motors = document.get('motors', {})
    if not isinstance(motors, dict):
        raise ValueError('gripper motor records must be a mapping')
    if not any(isinstance(record, dict) and record.get('position_reference_policy') is not None
               for record in motors.values()):
        return
    sources = bundle.get('sources', {})
    required = {'teleop.robots.damiao_gripper', 'teleop.control.kuavo5_gripper_calibration',
                'teleop.robots.kuavo5_gripper_live', 'teleop.robots.kuavo5_gripper_owner',
                'teleop.robots.kuavo5_gripper_reference', 'teleop.robots.kuavo5_gripper_coordinates',
                'teleop.robots.kuavo5_live_agent'}
    if not required <= set(sources) or any(not isinstance(sources[name], str)
                                         or not sources[name].strip() for name in required):
        raise ValueError('gripper coordinate policy requires the complete motion-core dependencies')
    owner = ast.parse(sources['teleop.robots.kuavo5_gripper_owner'])
    classes = [node for node in owner.body if isinstance(node, ast.ClassDef)
               and node.name == 'OwnerGripperSession']
    constructors = [] if len(classes) != 1 else [node for node in classes[0].body
        if isinstance(node, ast.FunctionDef) and node.name == '__init__']
    parameters = set() if len(constructors) != 1 else {
        arg.arg for arg in constructors[0].args.args+constructors[0].args.kwonlyargs}
    if 'motor_records' not in parameters:
        raise ValueError('gripper coordinate owner must accept original motor_records')
    agent = ast.parse(sources['teleop.robots.kuavo5_live_agent'])
    production = [node for node in agent.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'run_robot']
    calls = [] if len(production) != 1 else [node for node in ast.walk(production[0])
        if isinstance(node, ast.Call) and
        ((isinstance(node.func, ast.Name) and node.func.id == 'OwnerGripperSession')
         or (isinstance(node.func, ast.Attribute) and node.func.attr == 'OwnerGripperSession'))]
    expected = ast.dump(ast.parse("gripper_settings.document['motors']", mode='eval').body)
    if not calls or any(len([kw for kw in call.keywords if kw.arg == 'motor_records'
                            and ast.dump(kw.value) == expected]) != 1 for call in calls):
        raise ValueError('production gripper owner must receive '
                         "motor_records=gripper_settings.document['motors']")


def check_shared_gripper_sources(bundle, service_files):
    """Preparation and motion must use identical copies of shared dependencies."""
    sources = bundle.get('sources', {})
    for relative, data in service_files.items():
        if not relative.startswith('src/') or not relative.endswith('.py'):
            continue
        module = relative[4:-3].replace('/', '.')
        name = module.rsplit('.', 1)[-1]
        if module in sources and (name == 'damiao_gripper' or name.startswith('kuavo5_gripper_')):
            if data != sources[module].encode('utf-8'):
                raise ValueError('shared gripper dependency differs between preparation and motion: '+module)


def check_installation(bundle_path, expected_sha256):
    """Verify one exported install without loading runtime or touching hardware."""
    if not _valid_digest(expected_sha256):
        raise ValueError('pinned bundle SHA256 required')
    bundle_path = Path(bundle_path)
    if not bundle_path.is_absolute() or bundle_path != ROOT / 'core.json':
        raise ValueError('run the exported installation entry with its absolute core.json path')
    manifest = json.loads(_read_installation_file(ROOT, 'manifest.json', 16_384),
                          object_pairs_hook=_strict_object)
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise ValueError('exact installation manifest fields required; re-export installation')
    if type(manifest['format_version']) is not int or manifest['format_version'] != 1:
        raise ValueError('unsupported installation manifest version')
    if manifest['physical_execution_verified'] is not False:
        raise ValueError('saved profile or installation verification marker mismatch')
    if manifest['bundle_sha256'] != expected_sha256:
        raise ValueError('installation manifest bundle pin mismatch')
    files = manifest['service_file_sha256']
    if not isinstance(files, dict) or set(files) != SERVICE_FILES:
        raise ValueError('exact service file whitelist required; missing or unexpected files')
    checked_service_files = {}
    for relative in sorted(SERVICE_FILES):
        if not _valid_digest(files[relative]):
            raise ValueError('invalid service file digest: ' + relative)
        raw = _read_installation_file(ROOT, relative, MAX_CORE_BYTES)
        if hashlib.sha256(raw).hexdigest() != files[relative]:
            raise ValueError('service file integrity mismatch: ' + relative)
        checked_service_files[relative] = raw
    raw = _read_installation_file(ROOT, 'core.json', MAX_CORE_BYTES)
    if not _valid_digest(manifest['core_file_sha256']) or hashlib.sha256(raw).hexdigest() != manifest['core_file_sha256']:
        raise ValueError('installed core file integrity mismatch')
    bundle = json.loads(raw, object_pairs_hook=_strict_object)
    # Both passive validators passed the exact file hash checks above. Avoid
    # importing a validator from an unrelated development checkout.
    profile = _bundle_profile(bundle)
    if profile != manifest['profile']:
        raise ValueError('installation profile/configuration mismatch')
    extra = {'gripper_config', 'gripper_config_sha256'} if profile['gripper_mode'] == 'damiao' else set()
    if 'base_mode' in profile:
        extra.add('base_mode')
    if 'return_home_config_sha256' in profile:
        extra |= {'return_home_config', 'return_home_config_sha256'}
    if set(bundle) != {'mode', 'sources', 'sha256', *FIXED_PROFILE} | extra:
        raise ValueError('exact installed bundle metadata required')
    if bundle['mode'] != 'validate':
        raise ValueError('only exported saved 0.7 bounded elbow-preference bundle is supported')
    sources = bundle['sources']
    if not isinstance(sources, dict) or not sources or any(
            not isinstance(k, str) or not k.startswith('teleop.') or not isinstance(v, str)
            for k, v in sources.items()):
        raise ValueError('invalid installed source manifest')
    actual = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    if actual != expected_sha256 or actual != bundle['sha256']:
        raise ValueError('installed bundle integrity mismatch')
    check_settings_core(bundle)
    check_mobile_base_core(bundle)
    check_gripper_coordinate_core(bundle)
    check_shared_gripper_sources(bundle, checked_service_files)
    return bundle


def _load_runtime():
    global read_auth_key, SessionWorker, OwnerLedger, SessionSupervisor, SessionNetworkService
    sys.path.insert(0, str(ROOT / 'src'))
    from teleop.robots import kuavo5_network_config as config
    from teleop.robots import kuavo5_session_worker as worker
    from teleop.robots import kuavo5_session_service as service
    from teleop.robots import kuavo5_session_profile as profile
    from teleop.robots import kuavo5_service_contract as contract
    from teleop.robots import kuavo5_return_config as returns
    from teleop.robots import kuavo5_session_settings as settings
    from teleop.robots import kuavo5_session_settings_store as settings_store
    import teleop
    import teleop.robots
    for module, relative in ((teleop, 'src/teleop/__init__.py'),
            (teleop.robots, 'src/teleop/robots/__init__.py'),
            (config, 'src/teleop/robots/kuavo5_network_config.py'),
            (worker, 'src/teleop/robots/kuavo5_session_worker.py'),
            (service, 'src/teleop/robots/kuavo5_session_service.py'),
            (profile, 'src/teleop/robots/kuavo5_session_profile.py'),
            (contract, 'src/teleop/robots/kuavo5_service_contract.py'),
            (settings, 'src/teleop/robots/kuavo5_session_settings.py'),
            (settings_store, 'src/teleop/robots/kuavo5_session_settings_store.py'),
            (returns, 'src/teleop/robots/kuavo5_return_config.py')):
        if Path(module.__file__).resolve() != ROOT / relative:
            raise ValueError('refusing mixed installation imports: ' + module.__name__)
    if worker.FIXED_PROFILE != FIXED_PROFILE:
        raise ValueError('installed worker profile mismatch')
    for name, value in (('read_auth_key', config.read_auth_key),
                        ('SessionWorker', worker.SessionWorker),
                        ('OwnerLedger', service.OwnerLedger),
                        ('SessionSupervisor', service.SessionSupervisor),
                        ('SessionNetworkService', service.SessionNetworkService)):
        if globals()[name] is None:
            globals()[name] = value


def installation_identity():
    """Hash of the checked manifest: core, service files and fixed configuration."""
    return hashlib.sha256(_read_installation_file(ROOT, 'manifest.json', 16_384)).hexdigest()


def export_installation(destination, *, gripper_config=None, return_home_config=None, base_bundle=None, base_mode='disabled'):
    """PC-only local export. Never overwrite, connect, install or start anything."""
    sys.path.insert(0, str(ROOT / 'src'))
    from teleop.robots.kuavo5_live_bundle import build_bundle
    from teleop.robots.kuavo5_session_profile import bundle_profile
    options = dict(FIXED_PROFILE)
    if base_mode not in ('disabled', 'vendor_vr_x'):
        raise ValueError('unsupported base mode')
    if base_mode != 'disabled':
        options['base_mode'] = base_mode
    if gripper_config is not None:
        options.update(gripper_mode='damiao', gripper_config=gripper_config)
    if return_home_config is not None:
        if gripper_config is None:
            raise ValueError('return-home export requires a calibrated gripper configuration')
        options['return_home_config'] = return_home_config
    if base_bundle is None:
        bundle = build_bundle(ROOT / 'src', mode='validate', **options)
    else:
        # Service-only publication: carry the already reviewed motion core
        # verbatim. The normal source publication still builds a fresh core.
        bundle = json.loads(json.dumps(base_bundle, allow_nan=False))
        if base_mode != 'disabled' and bundle.get('base_mode', 'disabled') != base_mode:
            raise ValueError('reviewed motion bundle base mode differs from export request')
        if (bundle.get('mode') != 'validate' or
                any(bundle.get(k) != v for k, v in FIXED_PROFILE.items() if k != 'gripper_mode') or
                hashlib.sha256(json.dumps(bundle.get('sources'), sort_keys=True).encode()).hexdigest() != bundle.get('sha256')):
            raise ValueError('reviewed fixed-profile base bundle required')
    profile = bundle_profile(bundle)
    check_settings_core(bundle)
    check_mobile_base_core(bundle)
    check_gripper_coordinate_core(bundle)
    if (bundle.get('gripper_mode') == 'damiao' and
            not {'teleop.robots.kuavo5_gripper_reference',
                 'teleop.robots.kuavo5_gripper_coordinates'} <= set(bundle['sources'])):
        # A service-only export of an old core would copy its old two-read
        # sampler alongside the new preparation check. Reject this mixture
        # before creating files; publish the reviewed gripper core together.
        raise ValueError('gripper reference-check core must accompany preparation; '
                         'cannot mix the older motion core with this preparation helper')
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    files = {}
    for package in ('teleop', 'teleop/robots', 'teleop/control'):
        folder = destination / 'src' / package
        folder.mkdir(parents=True, exist_ok=True)
        relative = 'src/' + package + '/__init__.py'
        data = b'"""Minimal passive session installation package."""\n'
        (destination / relative).write_bytes(data)
        files[relative] = hashlib.sha256(data).hexdigest()
    names = ('kuavo5_execution_lock', 'kuavo5_network_config', 'kuavo5_network_wire', 'kuavo5_session_worker', 'kuavo5_session_service', 'kuavo5_controller_admin', 'kuavo5_session_profile', 'kuavo5_service_contract', 'kuavo5_return_config', 'kuavo5_direct_channel', 'kuavo5_service_client')
    for name in names:
        relative = 'src/teleop/robots/' + name + '.py'
        data = (ROOT / relative).read_bytes()
        (destination / relative).write_bytes(data)
        files[relative] = hashlib.sha256(data).hexdigest()
    (destination / 'apps').mkdir()
    relative = 'apps/kuavo5_session_service.py'
    data = Path(__file__).read_bytes()
    (destination / relative).write_bytes(data)
    files[relative] = hashlib.sha256(data).hexdigest()
    relative = 'apps/kuavo5_gripper_worker.py'
    data = (ROOT / relative).read_bytes()
    (destination / relative).write_bytes(data)
    files[relative] = hashlib.sha256(data).hexdigest()
    relative = 'apps/kuavo5_service_request.py'
    data = (ROOT / relative).read_bytes()
    (destination / relative).write_bytes(data)
    files[relative] = hashlib.sha256(data).hexdigest()
    # The helper imports passive gripper dependencies from real disk files;
    # use the exact copies already carried by the motion bundle when present.
    for relative in sorted(SERVICE_FILES - set(files)):
        module = relative[4:-3].replace('/', '.') if relative.startswith('src/') else None
        data = bundle['sources'][module].encode() if module in bundle['sources'] else (ROOT / relative).read_bytes()
        (destination / relative).parent.mkdir(parents=True, exist_ok=True)
        (destination / relative).write_bytes(data)
        files[relative] = hashlib.sha256(data).hexdigest()
    check_shared_gripper_sources(bundle, {relative: (destination/relative).read_bytes()
                                         for relative in files})
    core = json.dumps(bundle, ensure_ascii=True).encode('utf-8')
    (destination / 'core.json').write_bytes(core)
    (destination / 'manifest.json').write_text(json.dumps(dict(
        format_version=1, bundle_sha256=bundle['sha256'], profile=profile,
        core_file_sha256=hashlib.sha256(core).hexdigest(),
        service_file_sha256=files, physical_execution_verified=False), indent=2)+'\n')
    return dict(event='KUAVO_SESSION_INSTALLATION_EXPORTED', directory=str(destination),
        bundle_sha256=bundle['sha256'], profile=profile, robot_installed=False,
        installation_sha256=hashlib.sha256((destination/'manifest.json').read_bytes()).hexdigest(),
        listener_started=False, ssh_started=False, physical_robot_command_sent=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument('--serve', action='store_true')
    operations.add_argument('--check-installation', action='store_true')
    operations.add_argument('--export-installation', metavar='NEW_DIRECTORY')
    parser.add_argument('--bundle')
    parser.add_argument('--bundle-sha256')
    parser.add_argument('--gripper-config', type=Path, help='local export only: calibrated fixed gripper configuration')
    parser.add_argument('--base-mode', choices=('disabled', 'vendor_vr_x'), default='disabled',
                        help='local export only: explicitly include integrated base control')
    parser.add_argument('--return-home-config', type=Path,
                        help='local export only: fixed Y/B return-home configuration; never starts a return')
    parser.add_argument('--token-file')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7443)
    parser.add_argument('--tls-cert')
    parser.add_argument('--tls-key')
    parser.add_argument('--enable-arm-sessions', action='store_true',
                        help='operator permission for explicit authenticated arm starts; not auto-start')
    parser.add_argument('--enable-controller-admin', action='store_true',
                        help='allow explicit authenticated manufacturer prepare/initialize requests')
    parser.add_argument('--enable-robot-preparation', action='store_true',
                        help='explicit r8 controller/gripper requests; never prepares at listener startup')
    parser.add_argument('--enable-gripper-sessions', action='store_true', help='explicit installed gripper-worker permission')
    parser.add_argument('--accept-unverified-gripper-bus', action='store_true')
    parser.add_argument('--wait-network', action='store_true', help='wait for configured local interface at boot')
    parser.add_argument('--start-in-maintenance', action='store_true',
                        help='listen/status only until explicit matching leave-maintenance request')
    args = parser.parse_args(argv)
    try:
        maintained_start = startup_maintenance(args.start_in_maintenance)
        if maintained_start and not args.serve:
            raise ValueError('startup maintenance requires --serve')
        host = validate_numeric_host(args.host)
        if ipaddress.IPv4Address(host).is_unspecified or not 0 < args.port <= 65535:
            raise ValueError('explicit interface address and port 1..65535 required')
        if args.enable_arm_sessions and not args.serve:
            raise ValueError('--enable-arm-sessions requires --serve')
        if (args.enable_gripper_sessions or args.accept_unverified_gripper_bus) and not (args.serve and args.enable_arm_sessions):
            raise ValueError('gripper permission requires an explicitly enabled serving session')
        if args.gripper_config is not None and not args.export_installation:
            raise ValueError('--gripper-config is for local export only; runtime uses installed config')
        if args.return_home_config is not None and not args.export_installation:
            raise ValueError('--return-home-config is for local export only; runtime uses installed config')
        if args.base_mode != 'disabled' and not args.export_installation:
            raise ValueError('--base-mode is for local export only; runtime uses installed config')
        if (args.enable_controller_admin or args.wait_network) and not args.serve:
            raise ValueError('controller admin/network wait requires --serve')
        if args.enable_controller_admin and not args.enable_arm_sessions:
            raise ValueError('controller admin requires explicit arm-session permission')
        if args.enable_robot_preparation and not (args.serve and args.enable_arm_sessions and args.enable_gripper_sessions):
            raise ValueError('robot preparation requires an explicitly enabled combined serving installation')
        if args.enable_robot_preparation and args.enable_controller_admin:
            raise ValueError('choose the r8 preparation manager without legacy controller admin')
        if args.export_installation:
            result = export_installation(args.export_installation, gripper_config=args.gripper_config,
                                         return_home_config=args.return_home_config,
                                         **({'base_mode': args.base_mode} if args.base_mode != 'disabled' else {}))
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if not args.serve and not args.check_installation:
            print(json.dumps(dict(event='KUAVO_SESSION_SERVICE_DRY_RUN',
                listener_started=False, worker_started=False, ssh_started=False,
                ros_started=False, physical_robot_command_sent=False,
                profile=FIXED_PROFILE, real_robot_ready=False)))
            return 0
        if not args.bundle or not args.bundle_sha256:
            raise ValueError('--bundle and --bundle-sha256 required')
        bundle_path = Path(args.bundle)
        if not bundle_path.is_absolute() or bundle_path != ROOT/'core.json':
            raise ValueError('run the exported installation entry with its absolute core.json path')
        installed_identity = installation_identity()
        bundle = check_installation(bundle_path, args.bundle_sha256)
        if installation_identity() != installed_identity:
            raise ValueError('installation changed during validation')
        if bundle['gripper_mode'] == 'damiao':
            if args.enable_controller_admin:
                raise ValueError('gripper installation requires r8-aware controller preparation; old controller admin is forbidden')
            if args.enable_arm_sessions and not args.enable_gripper_sessions:
                raise ValueError('explicit --enable-gripper-sessions required')
            if (args.enable_arm_sessions and bundle['gripper_config'].get('preparation', {}).get('shared_bus_compatibility_verified') is False
                    and not args.accept_unverified_gripper_bus):
                raise ValueError('explicit --accept-unverified-gripper-bus required for supervised configuration')
        elif args.enable_gripper_sessions or args.accept_unverified_gripper_bus or args.enable_robot_preparation:
            raise ValueError('gripper permission does not match installed arms-only profile')
        profile = _bundle_profile(bundle)
        if args.check_installation:
            print(json.dumps(dict(event='KUAVO_SESSION_INSTALLATION_CHECKED',
                bundle_sha256=bundle['sha256'], profile=profile,
                installation_sha256=installed_identity,
                service_files_verified=len(SERVICE_FILES), manifest_version=1,
                listener_started=False, worker_started=False, ros_started=False,
                physical_robot_command_sent=False, real_robot_ready=False)))
            return 0
        if not args.token_file:
            raise ValueError('--token-file required')
        if bool(args.tls_cert) != bool(args.tls_key):
            raise ValueError('--tls-cert and --tls-key must be supplied together')
        if not ipaddress.IPv4Address(host).is_loopback and not args.tls_cert:
            raise ValueError('non-loopback listener requires a TLS certificate and key')
        _load_runtime()
        key = read_auth_key(args.token_file)
        tls = None
        if args.tls_cert:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(args.tls_cert, args.tls_key)
        # Fixed across ports and installation copies. Another uid cannot reuse
        # this owner-only file. Never remove it automatically after a fault.
        ledger = OwnerLedger(OWNER_LEDGER).open()
        service = None
        try:
            settings_store = None
            if profile['gripper_mode'] == 'damiao':
                from teleop.robots.kuavo5_session_settings_store import SettingsStore
                settings_store = SettingsStore(bundle, installed_identity, create_if_missing=True)
                settings_store.load()
            factory = None
            if args.enable_arm_sessions:
                def factory(session_settings=None):
                    # A running listener must also reject an installation that
                    # was partially replaced after its startup check.
                    if installation_identity() != installed_identity:
                        raise ValueError('installation changed; restart reviewed service')
                    current = check_installation(bundle_path, bundle['sha256'])
                    if installation_identity() != installed_identity:
                        raise ValueError('installation changed during session validation')
                    if _bundle_profile(current) != profile:
                        raise ValueError('installed session profile changed; restart reviewed service')
                    return SessionWorker(bundle_path, bundle['sha256'],
                        backend='robot', allow_execute=True, python_executable=sys.executable,
                        session_settings=session_settings, installation_sha256=installed_identity)
            admin = None
            if args.enable_controller_admin:
                from teleop.robots.kuavo5_controller_admin import ControllerAdmin
                admin = ControllerAdmin()
            preparation = None
            if args.enable_robot_preparation:
                from teleop.robots.kuavo5_controller_admin import PreparationAdmin
                preparation = PreparationAdmin(helper_path=ROOT/'apps/kuavo5_robot_preparation_helper.py')
            from teleop.robots.kuavo5_service_contract import prepare_service_contract
            contract = prepare_service_contract(bundle) if profile['gripper_mode'] == 'damiao' else None
            supervisor = SessionSupervisor(bundle['sha256'], worker_factory=factory,
                backend='robot' if factory else 'none', ledger=ledger, controller_admin=admin,
                preparation_admin=preparation,
                profile=profile, installation_sha256=installed_identity,
                session_contract=contract, start_in_maintenance=maintained_start,
                settings_store=settings_store, settings_bundle=bundle if settings_store else None)
            if factory is not None and settings_store is not None:
                from teleop.robots.kuavo5_manual_worker import ManualWorker
                def manual_factory(session_settings):
                    checked = factory(session_settings)
                    return ManualWorker(checked.bundle_path, checked.expected_sha256,
                        backend='robot', allow_execute=True, python_executable=sys.executable,
                        session_settings=session_settings, installation_sha256=installed_identity)
                supervisor.manual_factory = manual_factory
            stop = threading.Event()
            old_handlers = {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.signal(sig, lambda signum, frame: stop.set())
            try:
                while not stop.is_set():
                    service = SessionNetworkService(key, supervisor, host=host, port=args.port, tls_context=tls)
                    try:
                        address = service.open()
                        break
                    except OSError as exc:
                        if not args.wait_network or exc.errno != errno.EADDRNOTAVAIL:
                            raise
                        stop.wait(1.)
                if stop.is_set():
                    return 0
                print(json.dumps(dict(event='KUAVO_SESSION_LISTENING', host=address[0], port=address[1],
                    backend=supervisor.backend, tls=tls is not None,
                    worker_started=False, physical_robot_command_sent=False,
                    service_status=supervisor.status())), flush=True)
                service.serve(stop)
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
            preparation_pending = bool(preparation and preparation.busy)
            reason = (supervisor.blocked_reason or (preparation.blocked_reason if preparation else None)
                or ('preparation_job_still_running' if preparation_pending else None)
                or ('operation_cleanup_unconfirmed' if ledger.state['active'] else None))
            blocked = supervisor.active is not None or reason is not None or ledger.state['active']
            print(json.dumps(dict(event='KUAVO_SESSION_SERVICE_CLOSED',
                software_cleanup_confirmed=not blocked, physical_stop_verified=False,
                reason=reason, preparation_pending=preparation_pending,
                owner_marker_retained=ledger.state['active'])), flush=True)
            return 1 if blocked else 0
        finally:
            if service is not None:
                service.close()
            ledger.close()
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps(dict(event='KUAVO_SESSION_SERVICE_ERROR', error=str(exc)), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
