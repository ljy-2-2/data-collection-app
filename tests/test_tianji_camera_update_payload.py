"""Prepare and install into a fake controller filesystem, including rollback."""
import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import update_tianji_camera_stream as updater


@pytest.mark.parametrize('fail_start', [False, True])
def test_payload_installs_all_dependencies_and_restores_supervision_on_failure(tmp_path, monkeypatch, fail_start):
    root = tmp_path / 'controller'
    root.mkdir()
    (root / 'releases').mkdir()
    conf = tmp_path / 'fusion-camera.conf'
    files = updater.source_files()
    prefix = 'FusionDataCollector/controller_v22/'
    new_conf = base64.b64decode(files[prefix + 'fusion-camera.conf'])
    old_conf = new_conf.replace(b'respawn limit unlimited\npost-stop script\n    sleep 2\nend script', b'respawn limit 3 60')
    conf.write_bytes(old_conf)
    before = dict.fromkeys(('server.py', 'streaming.py', 'camera_audit.py'), b'# old reviewed source\n')
    before['stream-manifest.json'] = json.dumps({'sha256': {}}).encode()
    for name, data in before.items(): (root / name).write_bytes(data)
    (root / 'server.json').write_text(json.dumps({'cameras': [{'id': 'usb-1'}]}))
    payload = dict(source=files, profile={'format': 'mjpeg', 'width': 1024, 'height': 768, 'fps': 30}, status={},
                   expected={name: [hashlib.sha256(before[name]).hexdigest()] for name in ('server.py', 'streaming.py')})
    api = SimpleNamespace(device_info=lambda c: dict(available=True, bus=1, address=2), interface_idle=lambda c: True)
    stream = SimpleNamespace(audit=lambda: {'test': 'unchanged'}, unchanged_for_recovery=lambda *a: True)
    monkeypatch.setitem(sys.modules, 'server', api)
    monkeypatch.setitem(sys.modules, 'streaming', stream)
    monkeypatch.setitem(sys.modules, 'camera_audit', SimpleNamespace(audit_changed=lambda *a: False))
    calls = []
    failed = False
    def call(command, **kwargs):
        calls.append(command)
        return 1 if command[0] in ('/usr/bin/pgrep', '/bin/fuser') else 0
    def check_call(command, **kwargs):
        nonlocal failed
        calls.append(command)
        if command[0] == '/usr/bin/timeout': Path(command[-1]).write_bytes(b'test native executable')
        if fail_start and command == ['/sbin/start', 'fusion-camera'] and not failed:
            failed = True
            raise RuntimeError('simulated start failure')
    monkeypatch.setattr(updater.subprocess, 'call', call)
    monkeypatch.setattr(updater.subprocess, 'check_call', check_call)
    monkeypatch.setattr(updater.subprocess, 'check_output', lambda *a, **k: b'fusion-camera start/running')
    import time
    monkeypatch.setattr(time, 'sleep', lambda _: None)
    remote = updater.REMOTE.replace("root = '/mnt/fusion-camera'", 'root = ' + repr(str(root)))
    remote = remote.replace("service_conf = '/etc/init/fusion-camera.conf'", 'service_conf = ' + repr(str(conf)))
    run = lambda: exec(compile(remote, 'fake-controller-update', 'exec'),
                       {'PAYLOAD': base64.b64encode(json.dumps(payload).encode())})
    if fail_start:
        with pytest.raises(RuntimeError, match='simulated start failure'): run()
        assert conf.read_bytes() == old_conf
        for name, data in before.items(): assert (root / name).read_bytes() == data
        for name in ('camera_recheck.py', 'camera_usb_recovery.py', 'stream_checkpoint.py'):
            assert not (root / name).exists()
    else:
        run()
        assert conf.read_bytes() == new_conf
        manifest = json.loads((root / 'stream-manifest.json').read_text())
        assert manifest['stream_checkpoints'] and manifest['camera_manual_recheck']
        for name in ('server.py', 'streaming.py', 'camera_recheck.py', 'camera_usb_recovery.py', 'stream_checkpoint.py'):
            data = (root / name).read_bytes()
            compile(data, name, 'exec')
            assert manifest['sha256'][str(root / name)] == hashlib.sha256(data).hexdigest()
        assert b'respawn limit unlimited' in new_conf and b'sleep 2' in new_conf
    assert all('robot' not in ' '.join(c) for c in calls)
