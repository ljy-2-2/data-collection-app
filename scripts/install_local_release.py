#!/usr/bin/env python3
"""Activate a complete release for the current desktop user, preserving state."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files(root):
    for base, directories, names in os.walk(root):
        directories[:] = [d for d in directories if d not in ('.runtime', '__pycache__')]
        for name in names:
            if name != '.env' and not name.endswith('.pyc'):
                yield Path(base) / name


def install(package, current, data, launcher, config):
    metadata = json.loads((package / 'package-build.json').read_text())
    build = metadata['build_id']
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+~]*', build):
        raise ValueError('Invalid build ID')
    hashes = json.loads((package / 'app-sha256.json').read_text())
    actual = {str(p.relative_to(package / 'app')): digest(p)
              for p in (package / 'app').rglob('*') if p.is_file()}
    if hashes != actual:
        raise ValueError('Application manifest mismatch')
    old_build = json.loads((current / 'package-build.json').read_text())['build_id']
    old_work = data / 'workspaces' / old_build
    destination = data / 'versions' / build
    workspace = data / 'workspaces' / build
    if destination.exists() or workspace.exists():
        raise ValueError('Release or workspace already exists; refusing to overwrite')
    original = launcher.read_bytes()
    if str(current).encode() not in original:
        raise ValueError('Desktop launcher no longer points to the expected release')
    updated = original.replace(str(current).encode(), str(destination).encode())
    with (data / 'app.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                command = (proc / 'cmdline').read_bytes().split(b'\0')
                if any(arg.endswith(b'/scripts/desktop_app.py') for arg in command):
                    if (proc / 'cwd').resolve() == old_work.resolve():
                        raise RuntimeError('Desktop application is still running')
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
        state = {}
        for path in files(old_work):
            relative = path.relative_to(old_work)
            previous = current / 'app' / relative
            if previous.is_file() and digest(path) == digest(previous):
                continue
            # Only mutable state is migrated. Unexpected local source edits
            # must be reconciled explicitly, never silently lost or reused.
            if not any(part in {'runtime', '_runtime', 'local', 'logs'} for part in relative.parts):
                raise ValueError(f'Unexpected local modification: {relative}')
            state[str(relative)] = digest(path)
        config_before = {str(p.relative_to(config)): digest(p)
                         for p in config.rglob('*') if p.is_file()}
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = data / 'updates' / ('before-' + build + '-' + stamp)
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(launcher, backup / 'desktop-launcher')
        stage = Path(tempfile.mkdtemp(prefix='.release-', dir=data / 'versions'))
        work_stage = Path(tempfile.mkdtemp(prefix='.release-', dir=data / 'workspaces'))
        try:
            shutil.copytree(package, stage, dirs_exist_ok=True, symlinks=True)
            shutil.copytree(stage / 'app', work_stage, dirs_exist_ok=True, symlinks=True)
            for relative, expected in state.items():
                target = work_stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_work / relative, target)
                if digest(target) != expected or digest(old_work / relative) != expected:
                    raise ValueError('State changed during installation: ' + relative)
            (work_stage / '.env').symlink_to(config / '.env')
            (work_stage / '.runtime').symlink_to(destination / 'runtime', target_is_directory=True)
            config_after = {str(p.relative_to(config)): digest(p)
                            for p in config.rglob('*') if p.is_file()}
            if config_after != config_before or launcher.read_bytes() != original:
                raise ValueError('Configuration or launcher changed during installation')
            stage.rename(destination)
            work_stage.rename(workspace)
            fd, temporary = tempfile.mkstemp(prefix='.release-', dir=launcher.parent)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(updated)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, launcher.stat().st_mode & 0o777)
                os.replace(temporary, launcher)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            report = dict(build_id=build, package=str(destination), workspace=str(workspace),
                          previous_package=str(current), launcher=str(launcher),
                          backup=str(backup), application_files=len(hashes),
                          preserved_state_sha256=state, config_unchanged=True, status='installed')
            (backup / 'installation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            return report
        finally:
            for path in (stage, work_stage):
                if path.exists():
                    shutil.rmtree(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('package', 'current', 'data', 'launcher', 'config', 'report'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    result = install(args.package.resolve(), args.current.resolve(), args.data.resolve(),
                     args.launcher.resolve(), args.config.resolve())
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
