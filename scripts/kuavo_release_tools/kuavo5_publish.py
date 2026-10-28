#!/usr/bin/env python3
"""Publish one immutable robot release; never start a control worker.

Run locally on the robot, or use an existing SSH master for maintenance upload.
Only a clean, atomically maintained listener may be replaced automatically.
Legacy listeners without that gate are installed alongside, never stopped.
"""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

RELEASE_RE = r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}'
CURRENT = Path('/var/lib/galaxis-kuavo-network/current.json')
LAUNCHER = Path('/usr/local/bin/kuavo-service-request')
MAINTENANCE = 'kuavo_idle_maintenance_v1'


def installer_module():
    path = Path(__file__).with_name('kuavo5_protected_install.py')
    spec = importlib.util.spec_from_file_location('reviewed_protected_installer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_paths(release):
    if not isinstance(release, str) or re.fullmatch(RELEASE_RE, release) is None:
        raise ValueError('exact release ID required')
    code = Path('/opt/galaxis/kuavo-network/releases')/release
    return dict(code=code, config=Path('/var/lib/galaxis-kuavo-network/releases')/release/'access/client.json',
                unit='galaxis-kuavo5-joint-'+release+'.service')


def ensure_idle(status, *, maintained=False):
    if (not isinstance(status, dict) or status.get('backend') != 'robot'
            or status.get('owner_present') is not False or status.get('blocked_reason') is not None
            or status.get('phase') != ('maintenance' if maintained else 'idle')
            or (maintained and status.get('maintenance') is not True)):
        raise RuntimeError('service is active, unclean or unavailable; no version switch')


def same_instance(status, observed):
    if any(status.get(k) != observed.get(k) for k in
           ('service_instance_id', 'installation_sha256', 'bundle_sha256', 'profile')):
        raise RuntimeError('service identity changed during publication')


def result(package, installed, **extra):
    return dict(event='KUAVO_PUBLICATION_RESULT', release_id=package['release_id'],
        installation_sha256=package['installation_sha256'], installed=installed,
        activated=False, current_updated=False, worker_started=False,
        boot_selection_verified=False,
        manufacturer_controller_changed=False, physical_robot_command_sent=False, **extra)


def publish(raw, pin, backend, *, previous_release=None, activate=True, adopt_running=False):
    """Lifecycle coordinator, fakeable without ROS/network/systemd in tests."""
    with backend.publication_slot():
        return _publish(raw, pin, backend, previous_release=previous_release,
                        activate=activate, adopt_running=adopt_running)


def _publish(raw, pin, backend, *, previous_release, activate, adopt_running):
    package = backend.validate(raw, pin)
    previous = backend.previous_release(previous_release)
    if previous is not None:
        backend.verify_connection_identity(package, previous)
    installed = backend.install(raw, pin)
    new = package['release_id']
    if not activate:
        return result(package, installed, reason='installed_only')
    if adopt_running:
        backend.check_legacy_boot()
        observed = backend.status(new, new)
        already_maintained = observed.get('maintenance') is True
        ensure_idle(observed, maintained=already_maintained)
        backend.verify_new_status(package, observed)
        if not already_maintained:
            backend.maintenance(new, new, 'enter', observed)
        try:
            with backend.execution_slot(new):
                status = backend.status(new, new)
                same_instance(status, observed)
                ensure_idle(status, maintained=True)
                backend.configure_boot(new, previous)
                backend.commit_current(package)
            backend.clear_activation_marker(new, optional=True)
        except BaseException:
            if not already_maintained:
                # This attempt temporarily gated an originally idle listener.
                # Restore that admission state only for its observed identity;
                # never remove a maintenance gate inherited from the operator.
                try:
                    backend.maintenance(new, new, 'leave', observed)
                except Exception:
                    pass
            raise
        backend.maintenance(new, new, 'leave', observed)
        answer = result(package, installed, reason='running_release_adopted')
        answer.update(activated=True, current_updated=True, boot_selection_verified=True)
        return answer
    if previous is None:
        return result(package, installed, reason='first_activation_requires_operator', migration_required=True)
    if previous == new:
        status = backend.status(new, new)
        backend.verify_new_status(package, status)
        backend.check_legacy_boot()
        backend.configure_boot(new, None)
        answer = result(package, installed, reason='already_selected', already_selected=True)
        answer.update(activated=True, boot_selection_verified=True)
        return answer
    observed = backend.status(previous, new)
    ensure_idle(observed)
    if observed.get('maintenance_protocol') != MAINTENANCE:
        return result(package, installed, reason='legacy_listener_requires_first_migration', migration_required=True,
            next_step='operator_disables_legacy_console_boot_then_switches_idle_listener_and_adopts_running_release')
    backend.check_legacy_boot()
    backend.maintenance(previous, new, 'enter', observed)
    old_stopped = False
    try:
        # The remote gate prevents new network sessions; this lock also blocks
        # development SSH workers. Neither operation asks a worker to stop.
        with backend.execution_slot(new):
            maintained = backend.status(previous, new)
            same_instance(maintained, observed)
            ensure_idle(maintained, maintained=True)
            backend.stop_listener(previous)
            old_stopped = True
            backend.start_listener(new)
            status = backend.wait_status(new, new)
            ensure_idle(status, maintained=True)
            backend.verify_new_status(package, status)
            backend.configure_boot(new, previous)
            backend.commit_current(package)
        backend.clear_activation_marker(new)
        # Let the new worker acquire the execution slot only after publication
        # released it. A freshly started listener cannot race this verification.
        backend.maintenance(new, new, 'leave', status)
    except BaseException:
        if not old_stopped:
            # Reopen only the exact clean listener which this request gated.
            # Failure leaves maintenance in place for explicit inspection.
            try:
                backend.maintenance(previous, new, 'leave', observed)
            except Exception:
                pass
        raise
    answer = result(package, installed, reason='published')
    answer.update(activated=True, current_updated=True, boot_selection_verified=True)
    return answer


def protected_read(path, *, maximum=4_194_304):
    path = Path(path)
    for parent in reversed(path.parents):
        metadata = parent.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                or metadata.st_mode & 0o022):
            raise ValueError('root-protected directory required: '+str(parent))
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError('root-protected regular file required: '+str(path))
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(maximum+1)
        if len(data) > maximum:
            raise ValueError('protected file too large')
        return data
    finally:
        os.close(fd)


def atomic_write(path, data, *, mode=0o644):
    path = Path(path)
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe publication parent')
    if path.exists() or path.is_symlink():
        protected_read(path)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.'+path.name+'-', delete=False) as stream:
            pending = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if pending is not None and pending.exists():
            pending.unlink()


def launcher_source():
    # The descriptor is protected, but still validate every path. The operator
    # never chooses a root command and this launcher itself needs no sudo.
    return '''#!/usr/bin/python3 -I
import json, os, pathlib, re, stat, sys
sys.dont_write_bytecode=True
p=pathlib.Path('/var/lib/galaxis-kuavo-network/current.json')
for parent in p.parents:
    s=parent.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode & 0o022:
        raise SystemExit('Unprotected current-version directory')
s=p.lstat()
if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode & 0o022:
    raise SystemExit('Unprotected current-version record')
d=json.loads(p.read_text())
r=d.get('release_id','')
if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}',r):
    raise SystemExit('Invalid current release')
app='/opt/galaxis/kuavo-network/releases/'+r+'/apps/kuavo5_service_request.py'
cfg='/var/lib/galaxis-kuavo-network/releases/'+r+'/access/client.json'
if d.get('request_app')!=app or d.get('robot_config')!=cfg:
    raise SystemExit('Current paths differ')
if any(x=='--config' or x.startswith('--config=') or x=='--network-namespace' or x.startswith('--network-namespace=') for x in sys.argv[1:]):
    raise SystemExit('This entry uses the current robot service configuration')
os.execv('/usr/bin/python3',['/usr/bin/python3','-I','-B',app,'--config',cfg]+sys.argv[1:])
'''


class RobotBackend:
    """Root installation orchestration; all requests execute as the lab user."""
    def __init__(self, *, runner=subprocess.run):
        self.installer = installer_module()
        self.runner = runner

    def command(self, argv, *, timeout=30):
        completed = self.runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, env=dict(self.installer.SAFE_ENV))
        if completed.returncode:
            # Do not print subprocess stderr containing possible secret paths
            # or credential material. Original journals remain on the robot.
            raise RuntimeError('publication command failed: '+Path(argv[0]).name)
        return completed.stdout

    def validate(self, raw, pin):
        return self.installer.validate_package(raw, pin)

    @contextlib.contextmanager
    def publication_slot(self):
        path = CURRENT.parent/'publication.lock'
        if not path.parent.exists():
            self.installer.InstallFilesystem().mkdirs(str(path.parent))
        for parent in path.parents:
            metadata = parent.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                    or metadata.st_mode & 0o022):
                raise ValueError('unprotected publication lock directory')
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
                raise ValueError('publication lock must be root-private')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('another publication is in progress') from None
            yield
        finally:
            os.close(fd)

    def previous_release(self, requested):
        current = None
        if CURRENT.exists() or CURRENT.is_symlink():
            data = self.installer.parse(protected_read(CURRENT, maximum=16_384))
            current = data.get('release_id')
            release_paths(current)
        if requested is not None:
            release_paths(requested)
            if current is not None and current != requested:
                raise ValueError('explicit previous release differs from current record')
            current = requested
        return current

    def verify_connection_identity(self, package, previous):
        access = release_paths(previous)['config'].parent
        import pwd
        lab_uid = pwd.getpwnam('lab').pw_uid
        for parent in access.parents:
            metadata = parent.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                    or metadata.st_mode & 0o022):
                raise ValueError('old credential ancestors are not protected')
        metadata = access.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != lab_uid
                or metadata.st_mode & 0o077):
            raise ValueError('old credential directory is not private lab data')
        for name in ('token', 'server.crt', 'server.key'):
            # Parent credentials are lab-owned/private, intentionally not
            # root-protected; privileged code never executes these data files.
            fd = os.open(str(access/name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != lab_uid or info.st_mode & 0o077:
                    raise ValueError('old credentials are not private regular files')
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    old = stream.read(32_769)
            finally:
                os.close(fd)
            if old != base64.b64decode(package['access'][name], validate=True):
                raise ValueError('connection identity changed; use a separate explicit credential rotation')

    def verify_existing(self, package):
        paths = self.installer.get_paths(package)
        for name, encoded in package['files'].items():
            if protected_read(Path(paths['code'])/name) != base64.b64decode(encoded, validate=True):
                raise ValueError('existing installation differs: '+name)
        if protected_read(paths['unit']) != self.installer.render_unit(package).encode():
            raise ValueError('existing unit differs')
        if protected_read(paths['sudoers']) != self.installer.render_sudoers(package).encode():
            raise ValueError('existing worker rule differs')
        self.verify_connection_identity(package, package['release_id'])
        config = release_paths(package['release_id'])['config']
        fd = os.open(str(config), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError('existing robot request config is not private')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                config_bytes = stream.read(16_385)
        finally:
            os.close(fd)
        if config_bytes != base64.b64decode(package['access']['client.json'], validate=True):
            raise ValueError('existing robot request configuration differs')
        return False

    def install(self, raw, pin):
        package = self.validate(raw, pin)
        if release_paths(package['release_id'])['code'].exists():
            return self.verify_existing(package)
        self.installer.install_package(raw, pin, confirm_install=True)
        return True

    def request(self, target, tool_release, args):
        paths, tool = release_paths(target), release_paths(tool_release)
        output = self.command(['/usr/bin/sudo', '-n', '-H', '-u', 'lab', '/usr/bin/python3',
            '-I', '-B', str(tool['code']/'apps/kuavo5_service_request.py'),
            '--config', str(paths['config']), *args], timeout=15)
        return self.installer.parse(output)

    def status(self, target, tool_release):
        report = self.request(target, tool_release, ['--status'])
        status = report.get('service')
        if not isinstance(status, dict):
            raise RuntimeError('invalid service status response')
        manifest = self.installer.parse(protected_read(release_paths(target)['code']/'manifest.json'))
        expected = dict(installation_sha256=hashlib.sha256(protected_read(release_paths(target)['code']/'manifest.json')).hexdigest(),
                        bundle_sha256=manifest['bundle_sha256'], profile=manifest['profile'])
        self.verify_status_identity(status, expected, tool_release)
        return status

    def verify_status_identity(self, status, expected, tool_release, *, require_contract=False):
        """Pin installed code while allowing authenticated v2 runtime settings.

        Use the newly reviewed request tool's pure contract module for both old
        and new listeners. An old v1 installation does not know the v2 helper.
        This deliberately calls only standalone functions, not the module's
        package-relative import wrapper used by ordinary request clients.
        """
        if any(status.get(key) != expected[key]
               for key in ('installation_sha256', 'bundle_sha256')):
            raise RuntimeError('listening service differs from selected release')
        configurable = False
        if require_contract or status.get('session_contract') is not None:
            path = release_paths(tool_release)['code']/'src/teleop/robots/kuavo5_service_contract.py'
            namespace = {'__name__': 'publication_contract'}
            exec(compile(protected_read(path), str(path), 'exec'), namespace)
            requirements = namespace['compatible_requirements'](status)
            contract = namespace['validate_service_contract'](status, requirements)
            configurable = contract['protocol'] in namespace.get('CONFIGURABLE_SESSION_PROTOCOLS',
                (namespace['CONFIGURABLE_SESSION_PROTOCOL'],))
        actual, installed = status.get('profile'), expected['profile']
        if (not isinstance(actual, dict) or set(actual) != set(installed)
                or any(actual[key] != value for key, value in installed.items()
                       if not (configurable and key == 'gripper_config_sha256'))):
            raise RuntimeError('listening service differs from selected release')

    def wait_status(self, target, tool_release):
        deadline = time.monotonic()+12.
        while True:
            try:
                return self.status(target, tool_release)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)

    def maintenance(self, target, tool_release, action, observed):
        packet = self.request(target, tool_release, ['--'+action+'-maintenance',
            '--expected-service-instance-id', observed['service_instance_id'],
            '--expected-installation-sha256', observed['installation_sha256'], '--confirm-maintenance'])
        if (packet.get('event') != ('maintenance_entered' if action == 'enter' else 'maintenance_left')
                or packet.get('service_instance_id') != observed['service_instance_id']
                or packet.get('installation_sha256') != observed['installation_sha256']):
            raise RuntimeError('maintenance gate was not acknowledged by the observed service')

    def execution_slot(self, new):
        import pwd
        path = release_paths(new)['code']/'src/teleop/robots/kuavo5_execution_lock.py'
        source = protected_read(path)
        namespace = {'__name__': 'publication_execution_lock'}
        exec(compile(source, str(path), 'exec'), namespace)
        return namespace['ExecutionLock'](route='ssh', trusted_operator_uid=pwd.getpwnam('lab').pw_uid)

    def checked_unit(self, release):
        paths = release_paths(release)
        unit = Path('/etc/systemd/system')/paths['unit']
        current = protected_read(unit)
        if current != protected_read(paths['code']/'install-support'/paths['unit']):
            raise ValueError('unit changed since installation')
        if b'--enable-controller-admin' in current or b'kuavo5_session_service.py --serve' not in current:
            raise ValueError('only the installed passive listener may be switched')
        return paths['unit']

    def boot_state(self, unit):
        done = self.runner(['/usr/bin/systemctl', 'is-enabled', unit], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            env=dict(self.installer.SAFE_ENV))
        state = done.stdout.decode().strip()
        if done.returncode not in (0, 1, 3, 4) or state not in (
                'enabled', 'enabled-runtime', 'disabled', 'static', 'indirect', 'masked',
                'masked-runtime', 'not-found', 'linked', 'linked-runtime', ''):
            raise RuntimeError('cannot determine boot state: '+unit)
        if not state:
            if done.returncode == 0:
                raise RuntimeError('empty boot state: '+unit)
            return 'not-found'
        return state

    def check_legacy_boot(self):
        for unit in ('galaxis-kuavo5-console.service', 'galaxis-kuavo5-session-arms.service',
                     'galaxis-kuavo5-session-passive.service'):
            if self.boot_state(unit) in ('enabled', 'enabled-runtime', 'linked', 'linked-runtime'):
                raise RuntimeError('legacy listener boot selection conflicts; operator must disable it first (no --now): '+unit)

    def configure_boot(self, new, previous):
        self.check_legacy_boot()
        unit = self.checked_unit(new)
        self.command(['/usr/bin/systemctl', 'enable', unit])
        if previous is not None and previous != new:
            self.command(['/usr/bin/systemctl', 'disable', self.checked_unit(previous)])
        if self.boot_state(unit) != 'enabled':
            raise RuntimeError('new listener boot selection was not confirmed')
        if previous is not None and previous != new and self.boot_state(release_paths(previous)['unit']) \
                in ('enabled', 'enabled-runtime', 'linked', 'linked-runtime'):
            raise RuntimeError('previous listener remains selected for boot')

    def stop_listener(self, release):
        self.command(['/usr/bin/systemctl', 'stop', self.checked_unit(release)])

    def start_listener(self, release):
        unit = self.checked_unit(release)
        directory = Path('/run/systemd/system')/(unit+'.d')
        self.installer.InstallFilesystem().mkdirs(str(directory))
        marker = directory/'90-kuavo-publication.conf'
        if marker.exists() or marker.is_symlink():
            raise ValueError('pending publication startup marker already exists; inspect previous attempt')
        self.installer.InstallFilesystem().write_new(str(marker), self.activation_marker(), mode=0o644)
        self.command(['/usr/bin/systemctl', 'daemon-reload'])
        self.command(['/usr/bin/systemctl', 'start', unit])

    @staticmethod
    def activation_marker():
        return b'[Service]\nEnvironment=KUAVO_START_IN_MAINTENANCE=1\n'

    def clear_activation_marker(self, release, *, optional=False):
        path = Path('/run/systemd/system')/(release_paths(release)['unit']+'.d')/'90-kuavo-publication.conf'
        if optional and not path.exists() and not path.is_symlink():
            return
        if protected_read(path) != self.activation_marker():
            raise ValueError('publication startup marker changed; preserve for inspection')
        path.unlink()
        self.command(['/usr/bin/systemctl', 'daemon-reload'])

    def verify_new_status(self, package, status):
        manifest = self.installer.manifest_for(package)
        if status.get('maintenance_protocol') != MAINTENANCE:
            raise RuntimeError('new service identity/maintenance support verification failed')
        expected = dict(installation_sha256=package['installation_sha256'],
            bundle_sha256=manifest['bundle_sha256'], profile=manifest['profile'])
        self.verify_status_identity(status, expected, package['release_id'], require_contract=True)

    def commit_current(self, package):
        release = package['release_id']
        paths = release_paths(release)
        wrapper = launcher_source().encode()
        if LAUNCHER.exists() or LAUNCHER.is_symlink():
            if protected_read(LAUNCHER) != wrapper:
                raise ValueError('existing stable request launcher differs; preserve it')
        else:
            atomic_write(LAUNCHER, wrapper, mode=0o755)
        document = dict(schema_version=1, release_id=release, unit=paths['unit'],
            installation_sha256=package['installation_sha256'],
            bundle_sha256=self.installer.manifest_for(package)['bundle_sha256'],
            request_app=str(paths['code']/'apps/kuavo5_service_request.py'), robot_config=str(paths['config']))
        if CURRENT.exists():
            old = protected_read(CURRENT)
            backup = CURRENT.with_name('current.before-'+hashlib.sha256(old).hexdigest()+'.json')
            if not backup.exists():
                atomic_write(backup, old)
        atomic_write(CURRENT, (json.dumps(document, sort_keys=True, indent=2)+'\n').encode())


def remote_error_reason(stderr, package):
    """Allow only our structured error reason, never relay arbitrary stderr."""
    if not isinstance(stderr, bytes):
        return None
    for line in reversed(stderr[-65_536:].decode('utf-8', errors='replace').splitlines()):
        if len(line) > 16_384:
            continue
        try:
            packet = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(packet, dict) or packet.get('event') != 'KUAVO_PUBLICATION_ERROR':
            continue
        reason = packet.get('reason')
        if not isinstance(reason, str) or not reason or len(reason) > 2048:
            return None
        # A future exception could accidentally include credential data. Treat
        # PEM content as secret and also redact the exact package token.
        if '-----BEGIN ' in reason or 'PRIVATE KEY' in reason:
            return 'remote error contained credential material; details withheld'
        try:
            token = base64.b64decode(package['access']['token'], validate=True).decode('ascii').strip()
        except (KeyError, TypeError, ValueError, UnicodeError):
            return None
        if token:
            reason = reason.replace(token, '[redacted token]')
        return ''.join(char if char.isprintable() else ' ' for char in reason)
    return None


def remote_publish(args):
    """Transfer exact local audited tools/data; no caller-supplied remote command."""
    installer = installer_module()
    raw = installer.read_package(args.package)
    package = installer.validate_package(raw, args.package_sha256)
    names = ('kuavo5_publish.py', 'kuavo5_protected_install.py')
    payload = dict(files={name: base64.b64encode(Path(__file__).with_name(name).read_bytes()).decode() for name in names},
        package=base64.b64encode(raw).decode(), package_sha256=args.package_sha256,
        previous_release=args.previous_release, install_only=args.install_only, adopt_running=args.adopt_running)
    receiver = r'''
import base64,hashlib,json,os,pathlib,re,subprocess,sys,tempfile
assert os.getuid()==0 and os.geteuid()==0
p=json.loads(sys.stdin.buffer.read(8*1024*1024+1))
assert set(p)=={'files','package','package_sha256','previous_release','install_only','adopt_running'}
assert set(p['files'])=={'kuavo5_publish.py','kuavo5_protected_install.py'}
raw=base64.b64decode(p['package'],validate=True)
assert hashlib.sha256(raw).hexdigest()==p['package_sha256']
stage=pathlib.Path(tempfile.mkdtemp(prefix='kuavo-publish-',dir='/var/tmp'))
os.chmod(stage,0o700)
for name,encoded in dict(p['files'],**{'package.json':p['package']}).items():
    data=base64.b64decode(encoded,validate=True)
    fd=os.open(str(stage/name),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f:f.write(data)
cmd=['/usr/bin/python3','-I','-B',str(stage/'kuavo5_publish.py'),'--package',str(stage/'package.json'),'--package-sha256',p['package_sha256'],'--confirm-publish','--install-only' if p['install_only'] else '--publish']
if p['previous_release'] is not None:cmd+=['--previous-release',p['previous_release']]
if p['adopt_running']:cmd+=['--adopt-running']
r=subprocess.run(cmd,stdin=subprocess.DEVNULL)
sys.exit(r.returncode)
'''
    command = ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-S', args.ssh_control_path,
        '-o', 'BatchMode=yes', '-o', 'ProxyCommand=false', '-o', 'ConnectTimeout=5',
        'lab@192.168.26.1', '/usr/bin/sudo -n /usr/bin/python3 -I -B -c '+shlex.quote(receiver)]
    completed = subprocess.run(command, input=json.dumps(payload).encode(), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=120)
    if completed.returncode:
        reason = remote_error_reason(completed.stderr, package)
        if reason is not None:
            raise RuntimeError('remote publication refused: '+reason)
        raise RuntimeError('remote publication failed; staged files retained; inspect robot publication log')
    return installer.parse(completed.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--publish', action='store_true')
    actions.add_argument('--install-only', action='store_true')
    parser.add_argument('--confirm-publish', action='store_true')
    parser.add_argument('--adopt-running', action='store_true', help='record an operator-activated clean release; never starts/stops it')
    parser.add_argument('--package', type=Path)
    parser.add_argument('--package-sha256')
    parser.add_argument('--previous-release')
    parser.add_argument('--ssh-control-path')
    args = parser.parse_args(argv)
    try:
        if not args.publish and not args.install_only:
            print(json.dumps(dict(event='KUAVO_PUBLICATION_PLAN', files_written=False,
                network_connected=False, worker_started=False, physical_robot_command_sent=False)))
            return 0
        if not args.confirm_publish or args.package is None or args.package_sha256 is None:
            raise ValueError('publication requires package, digest and --confirm-publish')
        if args.adopt_running and not args.publish:
            raise ValueError('--adopt-running requires --publish')
        if args.ssh_control_path:
            report = remote_publish(args)
        else:
            if (os.getuid(), os.geteuid()) != (0, 0):
                raise PermissionError('robot-local publication requires root')
            backend = RobotBackend()
            raw = backend.installer.read_package(args.package)
            report = publish(raw, args.package_sha256, backend, previous_release=args.previous_release,
                             activate=args.publish, adopt_running=args.adopt_running)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps(dict(event='KUAVO_PUBLICATION_ERROR', reason=str(exc),
            worker_started=False, manufacturer_controller_changed=False,
            physical_robot_command_sent=False)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
