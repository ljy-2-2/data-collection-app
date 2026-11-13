#!/usr/bin/env python3
"""Install a verified pose-only bundle into an idle desktop package/workspace."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import tempfile

POSE = 'scripts/qt_ui/poses.py'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace(path, content, mode):
    fd, temporary = tempfile.mkstemp(prefix='.pose-update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(bundle, package, data, check=False):
    manifest = json.loads((bundle/'update-manifest.json').read_text())
    payload = bundle/'payload'/POSE
    if digest(payload) != manifest['payload_sha256']:
        raise ValueError('更新包校验失败')
    actual = runpy.run_path(str(payload))['builtin_poses']('tianji')[0]['targets']
    if actual != manifest['targets']:
        raise ValueError('工作姿态与更新清单不一致')
    metadata_path = package/'package-build.json'
    metadata = json.loads(metadata_path.read_text())
    if metadata['build_id'] != manifest['base_build_id']:
        raise ValueError('当前安装版本与更新包不匹配')
    workspace = data/'workspaces'/metadata['build_id']
    roots = {'package':package/'app','workspace':workspace}
    # Use the desktop launcher's existing lock; no process is stopped here.
    with (data/'app.lock').open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('采集软件正在运行，请先正常退出后再安装')
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                args = (proc/'cmdline').read_bytes().split(b'\0')
                if any(arg.endswith(b'/scripts/desktop_app.py') for arg in args):
                    if (proc/'cwd').resolve() in roots.values():
                        raise RuntimeError('目标工作副本仍在运行，未安装')
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
        for root in roots.values():
            if digest(root/POSE) not in (manifest['previous_sha256'], manifest['payload_sha256']):
                raise ValueError('目标工作姿态含其他修改，未覆盖：'+str(root/POSE))
            for relative, expected in manifest['compatibility_sha256'].items():
                if digest(root/relative) != expected:
                    raise ValueError('目标模型或界面版本不匹配：'+str(root/relative))
        updates = {root/POSE:payload.read_bytes() for root in roots.values()}
        for name in ('app-sha256.json','source-sha256.json'):
            index = json.loads((package/name).read_text())
            index[POSE] = manifest['payload_sha256']
            updates[package/name] = (json.dumps(index,ensure_ascii=False,indent=2)+'\n').encode()
        fixes = metadata.setdefault('hotfixes',[])
        if manifest['update_id'] not in fixes:
            fixes.append(manifest['update_id'])
        metadata['work_pose_revision'] = manifest['update_id']
        updates[metadata_path] = (json.dumps(metadata,ensure_ascii=False,indent=2)+'\n').encode()
        report = dict(update_id=manifest['update_id'],package=str(package),workspace=str(workspace),
                      files=[str(p) for p in updates],targets=actual,status='checked')
        if check:
            return report
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = data/'updates'/('before-'+manifest['update_id']+'-'+stamp)
        backup.mkdir(parents=True,mode=0o700)
        originals = {p:(p.read_bytes(),p.stat().st_mode & 0o777) for p in updates}
        for path in updates:
            relative = ('package',path.relative_to(package)) if path.is_relative_to(package) else ('workspace',path.relative_to(workspace))
            saved = backup/relative[0]/relative[1]
            saved.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,saved)
        written = []
        try:
            for path,content in updates.items():
                if path.read_bytes() != originals[path][0]:
                    raise ValueError('安装期间目标文件发生变化：'+str(path))
                replace(path,content,originals[path][1])
                written.append(path)
            for root in roots.values():
                if digest(root/POSE) != manifest['payload_sha256']:
                    raise ValueError('安装后回读校验失败')
            report.update(status='installed',backup=str(backup),pose_sha256=manifest['payload_sha256'])
            (backup/'installation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        except BaseException:
            for path in reversed(written):
                replace(path,*originals[path])
            raise
        return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle',type=Path,required=True)
    p.add_argument('--package',type=Path,required=True)
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--check',action='store_true')
    args = p.parse_args()
    print(json.dumps(install(args.bundle.resolve(),args.package.resolve(),args.data.resolve(),args.check),
                     ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
