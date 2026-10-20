"""Offline camera-audit regression; no SSH, USB or robot commands."""
import importlib.util
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load('camera_audit_test', 'fusion_camera_stream/controller/camera_audit.py')
installer = load('camera_audit_installer_test', 'scripts/fix_tianji_camera_audit.py')
BASE = b'[    1.000001] existing kernel message\n'
KEY = 'dmesg_sha256'


@pytest.mark.parametrize('message', [
    'Mot[0][0] process enable', 'Mot[1][6] process disable',
    '_FX_ServoFsmEnableOperation 0x800f', '_FX_ServoFsmDisableOperation 0x8007',
    'Mot[0][0] process change to position mode',
    '_FX_ServoFsmWaitPositionMode set ctrl.b15=0, ctrl.b14=0',
    '_FX_ServoFsmShutDown 0x8006',
    '_FX_ServoFsmSwitchOnAndEnableOperation 0x800f',
    'Mot[0][0] process reset', 'Mot[1][6] process reset',
    '_FX_ServoFsmFaultResetRising 0x80', '_FX_ServoFsmFaultResetRising 0x8087',
    '_FX_ServoFsmFaultResetFalling 0x0', '_FX_ServoFsmFaultResetFalling 0x8007',
])
def test_exact_reported_servo_transitions_only_allowed_during_teleop(message):
    before = audit.dmesg_fingerprint(BASE)
    after = audit.dmesg_fingerprint(BASE + ('[ 2698.114066] '+message+'\n').encode())
    assert before[KEY] != after[KEY]  # Original audit evidence is retained.
    assert after['dmesg_servo_lines'] == 1
    assert not audit.audit_changed(before, after, KEY, 'teleop_observation')
    assert audit.audit_changed(before, after, KEY, 'stationary')


@pytest.mark.parametrize('message', [
    'usb 1-1: device disconnected', 'uvcvideo: Failed to submit URB',
    '_FX_ServoFsmEnableOperation 0x8008', '_FX_ServoFsmDisableOperation 0x800f',
    'Mot[0][0] process enable ERROR', 'Mot[1][7] process disable',
    'Mot[2][0] process enable', 'EtherCAT: link down', 'kernel panic',
    '_FX_ServoFsmShutDown 0x8007',
    '_FX_ServoFsmSwitchOnAndEnableOperation 0x8008',
    '_FX_ServoFsmWaitPositionMode set ctrl.b15=0, ctrl.b14=1',
    'Mot[0][0] process change to position mode ERROR',
    'Mot[0][0] process reset ERROR', 'Mot[1][7] process reset',
    '_FX_ServoFsmFaultResetRising 0x800f', '_FX_ServoFsmFaultResetFalling 0x80',
    'xhci_hcd 0000:00:14.0: ERROR Transfer event TRB DMA ptr not part of current TD',
])
def test_unknown_or_error_logs_still_fault_even_with_normal_servo_logs(message):
    before = audit.dmesg_fingerprint(BASE)
    after = audit.dmesg_fingerprint(
        BASE+b'[ 1.000002] Mot[0][0] process enable\n'+('[ 1.000003] '+message+'\n').encode())
    assert audit.audit_changed(before, after, KEY, 'teleop_observation')


def test_removed_old_log_and_legacy_audits_cannot_bypass_check():
    before = audit.dmesg_fingerprint(BASE)
    after = audit.dmesg_fingerprint(b'[ 2.000001] Mot[0][0] process enable\n')
    assert audit.audit_changed(before, after, KEY, 'teleop_observation')
    after = audit.dmesg_fingerprint(BASE+b'[ 2.000001] Mot[0][0] process enable\n')
    assert audit.audit_changed({KEY: before[KEY]}, after, KEY, 'teleop_observation')
    before['modules'], after['modules'] = 'old', 'new'
    assert audit.audit_changed(before, after, 'modules', 'teleop_observation')


def test_installer_refuses_motion_or_other_camera_faults():
    status = dict(version='2.3.2', idle=True, fault='Audit changed: dmesg_sha256',
                  last_stream=dict(native_exit=0))
    installer.check_idle(dict(state='idle'), status)
    for app in [dict(state='moving'), dict(state='running'),
                dict(state='idle', collection=dict(busy=True))]:
        with pytest.raises(RuntimeError):
            installer.check_idle(app, status)
    for change in [dict(fault='Audit changed: modules'), dict(idle=False),
                   dict(last_stream=dict(native_exit=1)),
                   dict(last_stream=dict(native_exit=0, native_cleanup_timed_out=True))]:
        with pytest.raises(RuntimeError):
            installer.check_idle(dict(state='idle'), dict(status, **change))


def test_source_patch_is_minimal_unique_and_reversible():
    source = (ROOT/'fusion_data_collection/FusionDataCollector/controller_v22/streaming.py').read_text()
    patched = installer.patch_source(source)
    compile(patched, 'streaming.py', 'exec')
    assert "'robot_pids','modules','mounts','config_hashes','dmesg_sha256'" in patched
    assert 'if audit_changed(before, after, key, mode):' in patched
    for before, after in reversed(installer.REPLACEMENTS):
        assert patched.count(after) == 1
        patched = patched.replace(after, before)
    assert patched == source
    with pytest.raises(ValueError):
        installer.patch_source(source+"\nif before[key] != after[key]: pass\n")


@pytest.mark.parametrize('fail_start', [False, True])
@pytest.mark.parametrize('upgrade', [False, True])
@pytest.mark.parametrize('stream_upgrade', [False, True])
def test_remote_install_backs_up_and_rolls_back_on_start_failure(tmp_path, monkeypatch, fail_start, upgrade, stream_upgrade):
    original = (ROOT/'fusion_data_collection/FusionDataCollector/controller_v22/streaming.py').read_bytes()
    old = installer.patch_source(original.decode()).encode() if upgrade else original
    source = tmp_path/'streaming.py'
    source.write_bytes(old)
    source.chmod(0o644)
    conf = tmp_path/'fusion-camera.conf'
    conf.write_text('exec /mnt/fusion-camera/server.py')
    policy = installer.POLICY.read_bytes()
    old_policy = b'# approved prior policy in this simulated installation\n'
    if upgrade:
        (tmp_path/'camera_audit.py').write_bytes(old_policy)
        (tmp_path/'camera_audit.py').chmod(0o644)
    payload = dict(expected=hashlib.sha256(original).hexdigest(), replacements=installer.REPLACEMENTS,
                   previous_policy_sha256=hashlib.sha256(old_policy).hexdigest(),
                   policy=base64.b64encode(policy).decode())
    target = installer.patch_source(original.decode())
    if stream_upgrade:
        target += '\n# reviewed recovery update\n'
        payload.update(expected_stream=hashlib.sha256(old).hexdigest(),
                       stream_source=base64.b64encode(target.encode()).decode())
    remote = installer.REMOTE.replace("root = '/mnt/fusion-camera'", 'root = '+repr(str(tmp_path)))
    remote = remote.replace("read('/etc/init/fusion-camera.conf')", 'read('+repr(str(conf))+')')
    # No privileged operations: fake root identity and all service commands.
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    original_lstat = os.lstat

    def root_owned(path):
        values = list(original_lstat(path))
        values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(os, 'lstat', root_owned)
    calls = []
    failed = False

    def check_call(command, **kwargs):
        nonlocal failed
        calls.append(command)
        if command == ['/sbin/start', 'fusion-camera'] and fail_start and not failed:
            failed = True
            raise subprocess.CalledProcessError(1, command)
        return 0

    monkeypatch.setattr(subprocess, 'check_call', check_call)
    monkeypatch.setattr(subprocess, 'call', lambda command, **kwargs: 1 if command[0] == '/usr/bin/pgrep' else 0)
    monkeypatch.setattr(subprocess, 'check_output', lambda command: BASE if command == ['dmesg'] else b'fusion-camera start/running, process 100\n')
    monkeypatch.setattr('time.sleep', lambda duration: None)
    namespace = {'PAYLOAD': base64.b64encode(json.dumps(payload).encode())}
    if fail_start:
        with pytest.raises(subprocess.CalledProcessError):
            exec(remote, namespace)
        assert source.read_bytes() == old
        if upgrade:
            assert (tmp_path/'camera_audit.py').read_bytes() == old_policy
        else:
            assert not (tmp_path/'camera_audit.py').exists()
        assert calls.count(['/sbin/start', 'fusion-camera']) == 2
    else:
        exec(remote, namespace)
        assert source.read_text() == target
        assert (tmp_path/'camera_audit.py').read_bytes() == policy
        count = len(calls)
        with pytest.raises(SystemExit) as stopped:
            exec(remote, namespace)
        assert stopped.value.code == 0
        assert len(calls) == count  # Reinstall does not silently clear a new fault.
    backups = list(tmp_path.glob('audit-servo-*'))
    assert len(backups) == 1
    assert (backups[0]/'streaming.py.before').read_bytes() == old
    assert (backups[0]/'dmesg.before.txt').read_bytes() == BASE
    if upgrade:
        assert (backups[0]/'camera_audit.py.before').read_bytes() == old_policy


def test_unknown_existing_policy_is_not_overwritten(tmp_path, monkeypatch):
    # Reuse a minimal patched source, but make the policy hash unapproved.
    source = (ROOT/'fusion_data_collection/FusionDataCollector/controller_v22/streaming.py').read_text()
    (tmp_path/'streaming.py').write_text(installer.patch_source(source))
    (tmp_path/'camera_audit.py').write_text('# user modified policy\n')
    (tmp_path/'streaming.py').chmod(0o644)
    (tmp_path/'camera_audit.py').chmod(0o644)
    conf = tmp_path/'fusion-camera.conf'
    conf.write_text('exec /mnt/fusion-camera/server.py')
    remote = installer.REMOTE.replace("root = '/mnt/fusion-camera'", 'root = '+repr(str(tmp_path)))
    remote = remote.replace("read('/etc/init/fusion-camera.conf')", 'read('+repr(str(conf))+')')
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    original_lstat = os.lstat
    def root_owned(path):
        values = list(original_lstat(path)); values[4] = 0
        return os.stat_result(values)
    monkeypatch.setattr(os, 'lstat', root_owned)
    monkeypatch.setattr(subprocess, 'call', lambda *args, **kwargs: 1)
    payload = dict(policy=base64.b64encode(installer.POLICY.read_bytes()).decode(),
                   previous_policy_sha256=installer.PREVIOUS_POLICY_SHA256)
    with pytest.raises(RuntimeError, match='refusing overwrite'):
        exec(remote, {'PAYLOAD': base64.b64encode(json.dumps(payload).encode())})
    assert not list(tmp_path.glob('audit-servo-*'))
