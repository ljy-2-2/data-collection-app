"""Queue visibility and monitoring must not interfere with a live recorder."""
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'teleop_control/src'))
from teleop_control.umi_queue import queue_summary
from teleop_control.umi_uploader import UmiRosbagUploader

LOCAL = {'kind': 'local', 'api_url': 'http://127.0.0.1:8000', 'web_url': 'http://127.0.0.1:5173'}
CLOUD = {'kind': 'cloud', 'api_url': 'https://example.test/api', 'web_url': 'https://example.test'}


def test_status_poll_does_not_deadlock_session_start(tmp_path, monkeypatch):
    import threading
    from teleop_control.umi import UmiManager
    manager = UmiManager(tmp_path/'project', tmp_path/'data', python=sys.executable,
                         runtime_dir=tmp_path/'runtime')
    monkeypatch.setattr(manager, '_monitor_state', lambda: (False, {}))
    held, snapshot_started = threading.Event(), threading.Event()
    target_lock = threading.RLock()
    acquired = []
    class Targets:
        def snapshot(self):
            snapshot_started.set()
            with target_lock:
                return LOCAL
    manager.upload_targets = Targets()
    def start_session_lock_order():
        with target_lock:
            held.set()
            assert snapshot_started.wait(3)
            got = manager._lock.acquire(timeout=1)
            acquired.append(got)
            if got:
                manager._lock.release()
    thread = threading.Thread(target=start_session_lock_order, daemon=True)
    thread.start()
    assert held.wait(3)
    assert manager.status().state == 'idle'
    thread.join(timeout=3)
    assert acquired == [True]


def episode(root, name, target=None):
    p = root / 'rosbags/task' / name
    p.mkdir(parents=True)
    (p / 'meta.json').write_text(json.dumps({'author': 'operator', **({'upload_destination': target} if target else {})}))
    (p / 'metadata.yaml').write_text('rosbag2_bagfile_information:\n  relative_file_paths: [data.mcap]\n')
    (p / 'data.mcap').write_bytes(b'\x89MCAP0\r\npayload\x89MCAP0\r\n')
    return p


def test_queue_retains_original_destinations_and_explains_auth(tmp_path):
    config = tmp_path / '.background-upload'
    config.mkdir()
    (config / 'config.json').write_text(json.dumps({'default_destination': CLOUD}))
    old = episode(tmp_path, 'old')
    (old / '.upload-status.json').write_text(json.dumps({'state': 'waiting_auth', 'message': '等待原账号登录'}))
    current = episode(tmp_path, 'current', LOCAL)
    (current / '.data-platform-uploaded.json').write_text('{}')
    pending = episode(tmp_path, 'pending', LOCAL)
    (pending / '.mp4-pending.json').write_text('{}')
    local = queue_summary(tmp_path, LOCAL)
    assert (local['pending'], local['uploaded'], local['processing'], local['other_pending']) == (0, 1, 1, 1)
    cloud = queue_summary(tmp_path, CLOUD)
    assert cloud['waiting_auth'] == 1 and cloud['pending'] == 1
    assert '等待原账号登录' in cloud['message']
    assert old.joinpath('data.mcap').is_file()
    assert json.loads(old.joinpath('meta.json').read_text()) == {'author': 'operator'}


def test_one_missing_authorization_does_not_block_other_queue(tmp_path, monkeypatch, caplog):
    from types import SimpleNamespace
    old = episode(tmp_path, '001-old', CLOUD)
    new = episode(tmp_path, '002-local', LOCAL)
    class Targets:
        def resolve(self, meta):
            if meta['upload_destination'] == CLOUD:
                raise ValueError('等待采集账号登录以恢复后台上传授权')
            return LOCAL, SimpleNamespace(store=SimpleNamespace(path=tmp_path / 'session.json'))
    monkeypatch.setattr('teleop_control.umi_uploader.platform_session_available', lambda *_: True)
    monkeypatch.setattr('teleop_control.umi_uploader.current_platform_username', lambda *_: 'operator')
    uploaded = []
    uploader = UmiRosbagUploader(tmp_path, 'http://unused.test', targets=Targets())
    def upload(folder, meta):
        uploaded.append(folder)
        uploader._receipt(folder, 'test-episode')
    monkeypatch.setattr(uploader, '_upload', upload)
    try:
        uploader.scan_once()
        uploader.scan_once()
    finally:
        uploader.stop()
    assert uploaded == [new]
    assert json.loads((old / '.upload-status.json').read_text())['state'] == 'waiting_auth'
    assert len([r for r in caplog.records if '等待采集账号' in r.message]) == 1


@pytest.mark.parametrize('payload, expected', [
    ({'ros_connected': True, 'recording': False, 'task_status': 'received_task:demo'}, ('received_task:demo', 'false')),
    ({'ros_connected': True, 'recording': True, 'task_status': 'executing'}, ('executing', 'true')),
    ({'ros_connected': False, 'recording': False}, (None, None)),
    ({'ros_connected': True, 'recording': 'false'}, (None, None)),
])
def test_poll_uses_persistent_subscriptions_without_starting_ros_nodes(monkeypatch, payload, expected):
    spec = importlib.util.spec_from_file_location('continuity_gc', ROOT / 'umi_collection/vendor/galaxis/tools/galaxis_collection.py')
    gc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gc)
    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(gc.urllib.request, 'build_opener', lambda *_: Opener())
    monkeypatch.setattr(gc.subprocess, 'run', lambda *_a, **_k: pytest.fail('poll must not launch a subprocess'))
    assert gc.read_monitor_recording_state() == expected
