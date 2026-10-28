#!/usr/bin/env python3
"""Build an inert, self-contained manual-control release; no robot/network I/O.

Preserve the currently accepted XR/recording core byte for byte. Only the
supervisor and fixed worker gain the separately selected manual protocol.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ('kuavo5_manual_control','kuavo5_manual_runtime','kuavo5_manual_worker')
CHANGED = ('kuavo5_session_service','kuavo5_session_worker',*MANUAL)


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path,value): path.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+'\n')
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def build(destination):
    baseline=ROOT/'teleop_control/runtime/kuavo/recording-update-20261027-r3'
    if destination.exists(): raise ValueError('Output directory exists; preserve previous builds')
    os.umask(0o077);destination.mkdir(parents=True,mode=0o700)
    builder=destination/'builder';shutil.copytree(baseline/'builder',builder)
    installation=destination/'installation';shutil.copytree(baseline/'installation',installation)
    current=json.loads((installation/'core.json').read_text())
    manifest=json.loads((installation/'manifest.json').read_text())
    changed={}
    for name in CHANGED:
        relative=Path('src/teleop/robots')/(name+'.py')
        source=ROOT/'teleop_control/vendor/kuavo_runtime'/relative
        ast.parse(source.read_text(),feature_version=(3,8))
        for target in (installation/relative,builder/relative): shutil.copyfile(source,target)
        manifest['service_file_sha256'][str(relative)]=digest(source)
        changed[str(relative)]=digest(source)
    relative=Path('apps/kuavo5_session_service.py')
    source=ROOT/'teleop_control/vendor/kuavo_runtime'/relative
    for target in (installation/relative,builder/relative): shutil.copyfile(source,target)
    manifest['service_file_sha256'][str(relative)]=digest(source)
    changed[str(relative)]=digest(source)
    write(installation/'manifest.json',manifest)
    # The protected installer independently checks the same exact whitelist.
    installer=builder/'apps/kuavo5_protected_install.py'
    source=installer.read_text()
    anchor="SERVICE_FILES = frozenset(("
    assert anchor in source
    source=source.replace(anchor,anchor+'\n'+''.join("    'src/teleop/robots/"+name+".py',\n" for name in MANUAL),1)
    installer.write_text(source)
    module('manual_installation_check',installation/relative).check_installation(installation/'core.json',current['sha256'])
    sys.path.insert(0,str(builder/'src'))
    release=module('manual_release_builder',builder/'apps/kuavo5_network_release.py')
    report=release.prepare_release(installation=installation,installation_sha256=digest(installation/'manifest.json'),
        gripper_config=baseline/'gripper-config.json',return_home_config=baseline/'return-home-config.json',
        destination=destination/'release',release_id=release.release_name(),enable_gripper_sessions=True,
        accept_unverified_gripper_bus=True,enable_robot_preparation=True,
        reuse_credentials=baseline.parent/'cleanup-update-20261027/credentials')
    release.local_app('kuavo5_protected_install').validate_package(
        (destination/'release/package.json').read_bytes(),report['package_sha256'])
    old=json.loads((baseline/'release/release-report.json').read_text())
    wrapper=(baseline/'release/install-files.sh').read_text()
    for key in ('package_sha256','installer_sha256'): wrapper=wrapper.replace(old[key],report[key])
    (destination/'release/install-files.sh').write_text(wrapper)
    report.update(changed_service_sources=changed,teleop_core_unchanged=True,
        previous_bundle_sha256=current['sha256'],manual_protocol='kuavo_manual_v1',
        previous_installation_sha256=digest(baseline/'installation/manifest.json'),
        settings_migration_required_before_activation=True,
        installed=False,activated=False,physical_execution_verified=False)
    write(destination/'manual-update-report.json',report)
    print(json.dumps({k:report[k] for k in ('release_id','package_sha256','bundle_sha256','installation_sha256','installed','activated')},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    build(parser.parse_args().output.resolve())
