"""Assemble isolated robot SDKs beside the desktop's separate Python/Qt."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from bundle_gdk_runtime import bundle_gdk_runtime
from bundle_xr_runtime import bundle_xr_runtime

PREFIX = Path('/opt/xiaokai-lab-data-collection')


def bundle_robot_runtime(package, *, sdk_source, python_sites, extra_native_dirs,
                         service_root, sdk_module, patchelf, elf_type, private_libs):
    package = Path(package)
    robot = package / 'runtime/robot'
    manifest = {
        'gdk': bundle_gdk_runtime(package, sdk_source, python_sites,
                                  extra_native_dirs=extra_native_dirs),
        'xr': bundle_xr_runtime(package, service_root, sdk_module),
    }
    # A PYTHONPATH entry alone does not process .pth files (used by cmeel/pin).
    (robot / 'python-site/sitecustomize.py').write_text(
        'import site\nfrom pathlib import Path\nsite.addsitedir(str(Path(__file__).resolve().parent))\n')
    binaries = robot / 'bin'
    binaries.mkdir(exist_ok=True)
    launcher = binaries / 'python3'
    launcher.write_text('''#!/bin/sh
set -eu
ROBOT_RUNTIME=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONNOUSERSITE=1
export PYTHONPATH="$ROBOT_RUNTIME/python-site:$ROBOT_RUNTIME/gdk/gdk/lib${PYTHONPATH:+:$PYTHONPATH}"
unset PYTHONHOME
exec "$ROBOT_RUNTIME/../sysroot/usr/bin/python3.10" "$@"
''')
    launcher.chmod(0o755)
    files = [path for path in robot.rglob('*')
             if path.is_file() and not path.is_symlink() and elf_type(path, 2)]
    library_dirs = sorted({PREFIX / path.parent.relative_to(package) for path in files
                           if '.so' in path.name})
    xr = PREFIX / 'runtime/robot/xr'
    xr_dirs = [xr, xr / 'lib', xr / 'SDK/x64']
    loader = PREFIX / 'runtime/sysroot/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2'
    for path in files:
        old = subprocess.check_output([str(patchelf), '--print-rpath', str(path)], text=True).strip()
        # Keep relocatable wheel paths; drop upstream /home and /tmp build paths.
        relative = [item for item in old.split(':') if item.startswith(('$ORIGIN', '${ORIGIN}'))]
        ordered = xr_dirs + library_dirs if path.is_relative_to(robot / 'xr') else library_dirs
        search = ':'.join(dict.fromkeys([*relative, *map(str, ordered), *private_libs.split(':')]))
        command = [str(patchelf), '--force-rpath', '--set-rpath', search]
        if elf_type(path, 3):
            command += ['--set-interpreter', str(loader)]
        subprocess.run([*command, str(path)], check=True)
    manifest['elf_count'] = len(files)
    manifest['python'] = 'private Ubuntu CPython 3.10'
    (robot / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f'已内置 G2/天机/XR 运行依赖，配置 {len(files)} 个机器人 ELF 文件', flush=True)
    return manifest
