"""Read-only identity binding for an operator-run gripper position survey.

Import is inert. The caller must hold the shared ExecutionLock before calling
from_current() and through the survey. This module only reads files/proc and
runs the hash-checked, audited operator_deployment.py --check entry. It never
opens CAN/owner sockets, imports the installed business runtime, or starts the
controller. A live CAN owner is required; robot initialization is not required.
"""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess


CURRENT = Path('/var/lib/galaxis-kuavo-network/current.json')
RELEASES = Path('/opt/galaxis/kuavo-network/releases')
PROFILE = 'src/teleop/robots/kuavo5_preparation_profile.py'
AUDITED_LAUNCHER_SHA256 = '8b6f28ed5d707a4694b50185bc9a00c6a4b79f7fbc66ff2297c929c49eb34323'
BUILD_ID = 'kuavo-dm-r8-20261014'


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate survey identity field: ' + key)
        result[key] = value
    return result


def _parse(raw):
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON value')))


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def _read_file(path, *, protected=True, limit=1_048_576):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute survey metadata path required')
    if protected:
        for item in (path, *path.parents):
            info = item.lstat()
            kind = stat.S_ISREG if item == path else stat.S_ISDIR
            if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise PermissionError('unprotected formal identity file: ' + str(item))
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('regular identity file required')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        if not raw or len(raw) > limit:
            raise ValueError('empty or oversized survey metadata file')
        return raw
    finally:
        os.close(fd)


def _run_check(command):
    return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=15, check=False,
                          env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})


def _profile_constants(raw):
    wanted = {'CONTROLLER_ROOT', 'CONTROLLER_RUNTIME_SHA256',
              'CONTROLLER_CONFIG_SHA256', 'CONTROLLER_LAUNCHER_SHA256'}
    result = {}
    for node in ast.parse(raw.decode('utf-8')).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    if target.id in result:
                        raise ValueError('duplicate controller profile constant')
                    result[target.id] = ast.literal_eval(node.value)
    if set(result) != wanted:
        raise ValueError('formal controller profile constants missing')
    return result


class SurveyContext:
    """No hardware side effects; bind one survey to the current formal owner."""

    @classmethod
    def from_current(cls, *, _read=_read_file, _run=_run_check, _proc=Path('/proc')):
        instance = cls()
        instance._read, instance._run, instance._proc = _read, _run, Path(_proc)
        instance._pins = {}
        instance._current_raw = _read(CURRENT, limit=16_384)
        current = _parse(instance._current_raw)
        release = current.get('release_id') if isinstance(current, dict) else None
        if (not isinstance(release, str)
                or re.fullmatch('[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}', release) is None
                or current.get('schema_version') != 1
                or not _hash(current.get('installation_sha256'))
                or not _hash(current.get('bundle_sha256'))):
            raise ValueError('invalid current formal release record')
        root = RELEASES / release
        if (current.get('request_app') != str(root/'apps/kuavo5_service_request.py')
                or current.get('robot_config') !=
                '/var/lib/galaxis-kuavo-network/releases/'+release+'/access/client.json'):
            raise ValueError('current release paths differ from formal installation')
        manifest = _parse(instance._pinned(root/'manifest.json', current['installation_sha256'], 16_384))
        if manifest.get('bundle_sha256') != current['bundle_sha256']:
            raise ValueError('current bundle and manifest differ')
        core = _parse(instance._pinned(root/'core.json', manifest.get('core_file_sha256')))
        sources = core.get('sources')
        if (not isinstance(sources, dict) or core.get('sha256') != current['bundle_sha256']
                or _digest(json.dumps(sources, sort_keys=True).encode()) != current['bundle_sha256']
                or core.get('gripper_mode') != 'damiao'):
            raise ValueError('formal core source identity or gripper mode differs')
        document = core.get('gripper_config')
        if not isinstance(document, dict):
            raise ValueError('formal gripper configuration missing')
        config_hash = _digest(json.dumps(document, sort_keys=True, allow_nan=False).encode())
        if (config_hash != core.get('gripper_config_sha256')
                or manifest.get('profile', {}).get('gripper_config_sha256') != config_hash):
            raise ValueError('formal gripper configuration hash mismatch')
        hashes = manifest.get('service_file_sha256', {})
        profile = _profile_constants(instance._pinned(root/PROFILE, hashes.get(PROFILE)))
        controller_root = Path(profile['CONTROLLER_ROOT'])
        if re.fullmatch(r'/opt/galaxis/kuavo-controller/releases/[A-Za-z0-9_-]+', str(controller_root)) is None:
            raise ValueError('unexpected formal controller path')
        if profile['CONTROLLER_LAUNCHER_SHA256'] != AUDITED_LAUNCHER_SHA256:
            raise ValueError('controller checker changed; read-only audit required before survey')
        instance._pinned(controller_root/'controller_runtime.py', profile['CONTROLLER_RUNTIME_SHA256'])
        launcher = controller_root/'operator_deployment.py'
        instance._pinned(launcher, profile['CONTROLLER_LAUNCHER_SHA256'])
        controller_config = _parse(instance._pinned(controller_root/'controller-config.json',
                                                   profile['CONTROLLER_CONFIG_SHA256']))
        owner = Path(document.get('owner_directory', ''))
        if (controller_config.get('schema_version') != 1
                or not owner.is_absolute() or '..' in owner.parts or owner.name != 'sockets'
                or str(owner.parent) != controller_config.get('deployment')
                or controller_config.get('operator_deployment_sha256') != AUDITED_LAUNCHER_SHA256):
            raise ValueError('formal gripper socket and controller deployment differ')
        # The audited --check validates deployment ownership, library and launch
        # digests, mapped library inode and the two sockets held by the owner.
        instance._command = ['/usr/bin/python3', '-I', '-B', str(launcher),
                             '--deployment', str(owner.parent), '--check']
        instance.document = copy.deepcopy(document)
        instance.context = dict(boot_id=None, controller_pid=None, controller_start_ticks=None,
            release_id=release, installation_sha256=current['installation_sha256'],
            gripper_config_sha256=config_hash, owner_directory=str(owner))
        identity = instance._identity()
        instance.context.update(identity)
        # Validate again after the external checker, including the initial PID.
        instance._unchanged()
        if instance._process_identity(identity['controller_pid']) != identity:
            raise RuntimeError('CAN owner changed during survey identity check')
        return instance

    def _pinned(self, path, expected, limit=1_048_576):
        if not _hash(expected):
            raise ValueError('missing formal file hash: ' + str(path))
        raw = self._read(path, limit=limit)
        if _digest(raw) != expected:
            raise ValueError('formal file integrity mismatch: ' + str(path))
        self._pins[Path(path)] = (expected, limit)
        return raw

    def _unchanged(self):
        if self._read(CURRENT, limit=16_384) != self._current_raw:
            raise RuntimeError('正式版本在采样期间变化；本轮停止，保留已有记录')
        for path, (expected, limit) in self._pins.items():
            if _digest(self._read(path, limit=limit)) != expected:
                raise RuntimeError('formal survey dependency changed: ' + str(path))
        if _digest(json.dumps(self.document, sort_keys=True, allow_nan=False).encode()) != self.context['gripper_config_sha256']:
            raise RuntimeError('survey configuration changed in memory')

    def _process_identity(self, pid):
        boot_id = self._read(self._proc/'sys/kernel/random/boot_id', protected=False, limit=256).decode().strip()
        if re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot_id) is None:
            raise ValueError('invalid robot boot identity')
        try:
            raw = self._read(self._proc/str(pid)/'stat', protected=False, limit=8192).decode()
            leading, fields = raw.rsplit(')', 1)
            values = fields.split()
            ticks = int(values[19])
            if int(leading.split('(', 1)[0].strip()) != pid or values[0] in ('Z', 'X') or ticks < 1:
                raise ValueError('invalid owner process identity')
        except (OSError, ValueError, IndexError) as exc:
            raise RuntimeError('CAN 收发主控已退出或身份变化；未发送 CAN 查询') from exc
        return dict(boot_id=boot_id, controller_pid=pid, controller_start_ticks=ticks)

    def _identity(self):
        self._unchanged()
        result = self._run(list(self._command))
        try:
            checked = _parse(result.stdout)
        except (ValueError, TypeError) as exc:
            raise RuntimeError('无法确认 CAN 收发主控状态；只读检查未返回有效结果') from exc
        if not isinstance(checked, dict):
            raise RuntimeError('无法确认 CAN 收发主控状态；只读检查未返回对象')
        owners = checked.get('owner_pids', [])
        if (result.returncode != 0 or checked.get('result') != 'deployment_verified'
                or checked.get('build_id') != BUILD_ID or checked.get('problems') != []
                or checked.get('can_frames_sent') != 0 or type(checked.get('can_frames_sent')) is not int
                or not isinstance(owners, list) or len(owners) != 1
                or type(owners[0]) is not int or owners[0] < 1):
            raise RuntimeError('CAN 收发主控未就绪，当前不能采样；本工具不会启动主控，也不要求输入 o 初始化。'
                               ' 检查结果：' + str(checked.get('problems', [])))
        identity = self._process_identity(owners[0])
        self._unchanged()
        return identity

    def check(self):
        """Recheck formal pins and the same live owner/socket binding; no CAN."""
        identity = self._identity()
        if any(identity[key] != self.context[key] for key in identity):
            raise RuntimeError('机器人重启或 CAN 收发主控更换；本轮停止，保留已有记录')
        return copy.deepcopy(self.context)
