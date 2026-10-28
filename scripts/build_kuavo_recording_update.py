#!/usr/bin/env python3
"""Build an inert private recording update. No SSH, services or robot I/O."""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
CHANGED=('kuavo5_diagnostics','kuavo5_live_agent','kuavo5_gripper_owner','damiao_gripper')


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+'\n')


def build(destination):
    baseline=ROOT/'teleop_control/runtime/kuavo/cleanup-update-20261027'
    if destination.exists():raise ValueError('Use a new output directory; old packages are preserved')
    os.umask(0o077);destination.mkdir(parents=True,mode=0o700)
    builder=destination/'builder';shutil.copytree(baseline/'builder',builder)
    installation=destination/'installation';shutil.copytree(baseline/'installation',installation)
    previous=json.loads((installation/'core.json').read_text())
    manifest=json.loads((installation/'manifest.json').read_text())
    # Reproduce the installed source snapshot, not unrelated ongoing edits.
    for name,source in previous['sources'].items():
        target=builder/'src'/Path(*name.split('.')).with_suffix('.py')
        target.parent.mkdir(parents=True,exist_ok=True);target.write_text(source)
    changes={}
    for name in CHANGED:
        relative=Path('src/teleop/robots')/(name+'.py')
        source=ROOT/'teleop_control/vendor/kuavo_runtime'/relative
        ast.parse(source.read_text(),feature_version=(3,8))
        shutil.copyfile(source,builder/relative)
        changes['teleop.robots.'+name]=digest(source)
    for name in ('gripper-config.json','return-home-config.json'):
        shutil.copyfile(baseline/name,destination/name)
    sys.path.insert(0,str(builder/'src'))
    from teleop.robots.kuavo5_live_bundle import build_bundle
    from teleop.robots.kuavo5_session_profile import bundle_profile
    current=build_bundle(builder/'src',mode='validate',
        target_recovery=previous['target_recovery'],speed_profile=previous['speed_profile'],
        arm_posture=previous['arm_posture'],gripper_mode=previous['gripper_mode'],
        gripper_config=previous['gripper_config'],return_home_config=previous['return_home_config'],
        base_mode=previous['base_mode'])
    changed={k for k in current['sources'] if current['sources'][k]!=previous['sources'].get(k)}
    if changed!=set(changes):raise ValueError('Unexpected motion source changes: '+str(sorted(changed)))
    for name,source in current['sources'].items():ast.parse(source,feature_version=(3,8))
    write(installation/'core.json',current)
    for relative in manifest['service_file_sha256']:
        if relative.startswith('src/') and relative.endswith('.py'):
            name=relative[4:-3].replace('/','.')
            if name in changes:
                (installation/relative).write_text(current['sources'][name])
                manifest['service_file_sha256'][relative]=digest(installation/relative)
    manifest.update(bundle_sha256=current['sha256'],core_file_sha256=digest(installation/'core.json'),
                    profile=bundle_profile(current))
    write(installation/'manifest.json',manifest)
    # Match the actual installed entry's passive validator, including copies
    # shared by preparation and motion. Do this before creating a release.
    spec=importlib.util.spec_from_file_location('recording_installation_check',installation/'apps/kuavo5_session_service.py')
    entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
    entry.check_installation(installation/'core.json',current['sha256'])
    spec=importlib.util.spec_from_file_location('recording_release_builder',builder/'apps/kuavo5_network_release.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    report=module.prepare_release(installation=installation,installation_sha256=digest(installation/'manifest.json'),
        gripper_config=destination/'gripper-config.json',return_home_config=destination/'return-home-config.json',
        destination=destination/'release',release_id=module.release_name(),enable_gripper_sessions=True,
        accept_unverified_gripper_bus=True,enable_robot_preparation=True,
        reuse_credentials=baseline/'credentials')
    installer=module.local_app('kuavo5_protected_install')
    installer.validate_package((destination/'release/package.json').read_bytes(),report['package_sha256'])
    # Reuse the audited install-files-only wrapper, replacing just content pins.
    old=json.loads((baseline/'release/release-report.json').read_text())
    wrapper=(baseline/'release/install-files.sh').read_text()
    for key in ('package_sha256','installer_sha256'):wrapper=wrapper.replace(old[key],report[key])
    (destination/'release/install-files.sh').write_text(wrapper)
    report.update(changed_core_sources=changes,previous_bundle_sha256=previous['sha256'],
        active_feedback_policy='accepted_commands_only_native_limits_preserved',
        disabled_feedback_requested_hz=100,real_feedback_rate_verified=False,
        full_publication_recording=True,installed=False,activated=False)
    write(destination/'recording-update-report.json',report)
    print(json.dumps({k:report[k] for k in ('release_id','package_sha256','bundle_sha256','installation_sha256','installed','activated')},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    build(parser.parse_args().output.resolve())
