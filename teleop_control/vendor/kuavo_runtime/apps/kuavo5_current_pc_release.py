#!/usr/bin/env python3
"""Install/verify one PC client snapshot, independent of robot release revisions.

Default is an inert plan. Preparation copies Python/client helper source only;
credentials and compatible connection configuration remain outside the snapshot.
Launching verifies PC source integrity then execs isolated Python. It never
uploads robot code, picks robot parameters, invokes SSH or starts a session by
itself: the selected client still requires explicit --execute.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


KIND = 'compatible_pc_client_v1'
APPS = ('kuavo5_current_client.py', 'pico_kuavo5_arms_live.py',
        'kuavo5_service_request.py', 'kuavo5_current_pc_release.py')
REQUIRED = {'apps/' + name for name in APPS} | {
    'scripts/kuavo-network', 'src/teleop/__init__.py',
    'src/teleop/robots/kuavo5_service_contract.py',
    'src/teleop/robots/kuavo5_service_client.py',
    'src/teleop/robots/kuavo5_pc_session.py'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON field')
        value[key] = item
    return value


def _plain_path(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('parent traversal refused')
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('symlink traversal refused: ' + str(parent))
    return path


def _read(path, *, private=False, limit=8_388_608):
    path = _plain_path(path)
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('regular file required: ' + str(path))
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('owner-only PC file required: ' + str(path))
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('PC file too large: ' + str(path))
        return data
    finally:
        os.close(fd)


def _code_name(name):
    if not isinstance(name, str):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != name:
        return False
    return (name in {'apps/' + app for app in APPS} or name == 'scripts/kuavo-network'
            or (name.startswith('src/teleop/') and name.endswith('.py')
                and '__pycache__' not in path.parts))


def prepare(source_root, destination):
    """Capture source bytes once; never copy configuration, tokens or caches."""
    source_root, destination = _plain_path(source_root), _plain_path(destination)
    if destination.exists():
        raise ValueError('PC destination already exists; existing releases are preserved')
    files = {}
    for source in (source_root / 'src/teleop').rglob('*.py'):
        name = source.relative_to(source_root).as_posix()
        if _code_name(name):
            files[name] = _read(source)
    for name in ('scripts/kuavo-network', *('apps/' + app for app in APPS)):
        files[name] = _read(source_root / name)
    if not REQUIRED <= set(files):
        raise ValueError('incomplete PC client source')
    # A second editor may be changing the development tree. Do not silently
    # publish a cross-file mixture read during those changes.
    for name, data in files.items():
        if _read(source_root / name) != data:
            raise ValueError('PC source changed during snapshot preparation: ' + name)
    manifest = dict(format_version=1, kind=KIND,
        configuration_policy='external_compatible_service_v1',
        files={name: digest(data) for name, data in sorted(files.items())})
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    # All input reads/validation precede mutation. Partial failures are preserved
    # and cannot verify without the final manifest; no existing files are deleted.
    destination.mkdir(mode=0o700)
    folders = {parent for name in files for parent in PurePosixPath(name).parents
               if parent != PurePosixPath('.')}
    for folder in sorted(folders, key=lambda value: (len(value.parts), str(value))):
        (destination / str(folder)).mkdir(mode=0o700)
    for name, data in files.items():
        path = destination / name
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(0o700 if name == 'scripts/kuavo-network' else 0o600)
    with (destination / 'pc-manifest.json').open('xb') as stream:
        stream.write(raw)
    (destination / 'pc-manifest.json').chmod(0o600)
    verify_release(destination, digest(raw))
    return dict(event='KUAVO_CURRENT_PC_PREPARED', directory=str(destination),
        manifest_sha256=digest(raw), files=len(files),
        configuration_source='external_compatible_service', robot_version_pinned=False,
        network_connected=False, robot_modified=False)


def verify_release(directory, expected_manifest):
    directory = _plain_path(directory)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('owner-only PC release directory required')
    if not isinstance(expected_manifest, str) or re.fullmatch('[a-f0-9]{64}', expected_manifest) is None:
        raise ValueError('explicit PC manifest SHA256 required')
    raw = _read(directory / 'pc-manifest.json', private=True, limit=1_048_576)
    if digest(raw) != expected_manifest:
        raise ValueError('PC manifest hash mismatch')
    manifest = json.loads(raw, object_pairs_hook=_object)
    if (not isinstance(manifest, dict) or set(manifest) != {
            'format_version', 'kind', 'configuration_policy', 'files'}
            or type(manifest['format_version']) is not int or manifest['format_version'] != 1
            or manifest['kind'] != KIND
            or manifest['configuration_policy'] != 'external_compatible_service_v1'):
        raise ValueError('unsupported PC manifest')
    files = manifest['files']
    if not isinstance(files, dict) or not REQUIRED <= set(files):
        raise ValueError('incomplete PC release')
    for name, pin in files.items():
        if not _code_name(name) or not isinstance(pin, str) or re.fullmatch('[a-f0-9]{64}', pin) is None:
            raise ValueError('invalid PC code inventory')
    actual = set()
    for path in directory.rglob('*'):
        relative = path.relative_to(directory).as_posix()
        if relative.split('/')[0] == 'artifacts':
            continue  # Runtime journals are not imported code.
        if path.is_symlink():
            raise ValueError('PC release symlink refused: ' + relative)
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('owner-only PC directory required: ' + relative)
        elif stat.S_ISREG(info.st_mode):
            if relative != 'pc-manifest.json':
                actual.add(relative)
        else:
            raise ValueError('unexpected PC file type: ' + relative)
    if actual != set(files):
        raise ValueError('PC release file inventory differs')
    for name, pin in files.items():
        if digest(_read(directory / name, private=True)) != pin:
            raise ValueError('PC release file changed: ' + name)
    return directory


def _compatible_config(path):
    """Check external connection structure, without reading its secret token.

    The verified current client repeats full contract validation after exec.
    This bootstrap performs no imports from mutable development directories.
    """
    path = _plain_path(path)
    config = json.loads(_read(path, private=True, limit=16_384), object_pairs_hook=_object)
    fields = {'connection_policy', 'required_contract', 'network_host', 'network_port',
              'network_token_file', 'network_cert_sha256'}
    if (not isinstance(config, dict) or set(config) != fields
            or config.get('connection_policy') != 'compatible_service_v1'):
        raise ValueError('external compatible service config required; no robot release pins or parameters')
    if not isinstance(config['network_host'], str):
        raise ValueError('numeric IPv4 address required')
    host = ipaddress.IPv4Address(config['network_host'])
    if type(config['network_port']) is not int or not 0 < config['network_port'] <= 65535:
        raise ValueError('valid network port required')
    cert = config['network_cert_sha256']
    if cert is not None and (not isinstance(cert, str) or re.fullmatch('[a-fA-F0-9]{64}', cert) is None):
        raise ValueError('valid TLS certificate pin required')
    if cert is None and not host.is_loopback:
        raise ValueError('remote service requires TLS certificate pin')
    if not isinstance(config['network_token_file'], str) or not Path(config['network_token_file']).is_absolute():
        raise ValueError('absolute local token file path required')
    if not isinstance(config['required_contract'], dict):
        raise ValueError('explicit service contract requirements required')
    return path


def launch(directory, manifest_sha256, config_path, options, *, exec_fn=os.execve):
    root = verify_release(directory, manifest_sha256)
    if not isinstance(options, (list, tuple)) or any(not isinstance(value, str) for value in options):
        raise ValueError('client options must be a list of strings')
    for value in options:
        if value == '--' or value.split('=', 1)[0] in (
                '--config', '--gripper-config', '--return-home-config', '--client-release',
                '--manifest-sha256', '--source-root', '--output'):
            raise ValueError('PC client uses its explicit connection config; no overrides')
    config = _compatible_config(config_path)
    if config == root or root in config.parents:
        raise ValueError('connection configuration must remain outside PC code snapshot')
    environment = dict(os.environ)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    bootstrap = ('import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); '
                 'p=sys.argv.pop(1); sys.argv[0]=p; runpy.run_path(p,run_name="__main__")')
    command = [sys.executable, '-I', '-B', '-c', bootstrap, str(root / 'src'),
               str(root / 'apps/kuavo5_current_client.py'), '--config', str(config), *options]
    exec_fn(sys.executable, command, environment)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--prepare', action='store_true')
    actions.add_argument('--verify', action='store_true')
    actions.add_argument('--launch', action='store_true')
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--client-release', type=Path)
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--config', type=Path)
    args, options = parser.parse_known_args(argv)
    try:
        for flag in ('--config', '--client-release', '--manifest-sha256', '--source-root', '--output'):
            if sum(value.split('=', 1)[0] == flag for value in argv) > 1:
                raise ValueError('duplicate protected option: ' + flag)
        if not args.prepare and not args.verify and not args.launch:
            if options:
                raise ValueError('explicit action required for client options')
            print(json.dumps(dict(event='KUAVO_CURRENT_PC_PLAN', files_written=False,
                network_connected=False, robot_modified=False)))
            return 0
        if not args.launch and options:
            raise ValueError('unexpected options for PC preparation/verification')
        if args.prepare:
            if args.source_root is None or args.output is None:
                raise ValueError('--prepare requires --source-root and --output')
            result = prepare(args.source_root, args.output)
        elif args.verify:
            if args.client_release is None or args.manifest_sha256 is None:
                raise ValueError('--verify requires --client-release and --manifest-sha256')
            root = verify_release(args.client_release, args.manifest_sha256)
            result = dict(event='KUAVO_CURRENT_PC_VERIFIED', directory=str(root),
                          network_connected=False, robot_modified=False)
        else:
            if args.client_release is None or args.manifest_sha256 is None or args.config is None:
                raise ValueError('--launch requires --client-release, --manifest-sha256 and --config')
            launch(args.client_release, args.manifest_sha256, args.config, options)
            return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(event='KUAVO_CURRENT_PC_ERROR', reason=str(exc)),
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
