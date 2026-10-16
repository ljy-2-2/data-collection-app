import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'teleop_control/src'))
from teleop_control.collection_gate import CollectionGate, collection_available
from teleop_control.tianji_collection import TianjiCollection
from teleop_control.umi import UmiManager
from teleop_control.app import create_app
from teleop_control.main import MockController


def test_gate_is_closed_when_missing_stale_or_unavailable(tmp_path, monkeypatch):
    path = tmp_path / 'gate.json'
    monkeypatch.setenv('DATA_COLLECTION_GATE_FILE', str(path))
    assert not collection_available()
    gate = CollectionGate(path, Mock())
    gate._write(False)
    assert not collection_available()
    gate._write(True, 'operator')
    assert collection_available()
    path.write_text(json.dumps(dict(available=True, checked_at=time.time()-20)))
    assert not collection_available()
    path.write_text('incomplete')
    assert not collection_available()


def test_offline_tianji_does_not_create_episode(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_COLLECTION_GATE_FILE', str(tmp_path / 'missing'))
    factory = Mock()
    collection = TianjiCollection(SimpleNamespace(), tmp_path / 'episodes', factory)
    collection.start()
    factory.assert_not_called()
    assert not collection.output.exists()
    assert collection.error == ''


def test_offline_umi_start_is_silent_and_does_not_call_recorder(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_COLLECTION_GATE_FILE', str(tmp_path / 'missing'))
    manager = UmiManager(tmp_path, tmp_path / 'data', python=sys.executable, runtime_dir=tmp_path / 'runtime')
    state = object()
    monkeypatch.setattr(manager, 'status', lambda: state)
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: pytest.fail('offline must not call recorder'))
    assert manager.record('start') is state


def test_offline_g2_gate_matches_desktop_gate(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('g2_collector', ROOT / 'teleop_control/vendor/remote_collector.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / 'gate.json'
    monkeypatch.setenv('DATA_COLLECTION_GATE_FILE', str(path))
    assert not module.collection_available()
    gate = CollectionGate(path, Mock())
    gate._write(True, 'operator')
    assert module.collection_available()
    gate._write(False)
    assert not module.collection_available()


@pytest.mark.parametrize('failure, expected_code', [
    (httpx.ConnectError('connection refused'), 502),
    (httpx.ReadTimeout('platform timeout'), 502),
    (httpx.HTTPStatusError('bad password', request=httpx.Request('POST', 'https://platform.test/auth/token'),
                         response=httpx.Response(401)), 401),
    (httpx.HTTPStatusError('platform down', request=httpx.Request('POST', 'https://platform.test/auth/token'),
                         response=httpx.Response(503)), 502),
])
def test_platform_login_failure_never_returns_offline_access(failure, expected_code):
    from fastapi import HTTPException
    login = Mock()
    login.login.side_effect = failure
    app = create_app(MockController(), cors_origins=[], platform_login=login)
    endpoint = next(r.endpoint for r in app.routes if r.path == '/platform/auth/login')
    with pytest.raises(HTTPException) as error:
        endpoint(SimpleNamespace(username='operator', password='wrong', remember_password=False,
                 auto_login=False, use_saved_password=False), SimpleNamespace(headers={}))
    assert error.value.status_code == expected_code


def test_offline_upload_queue_keeps_data_without_connection_notice(tmp_path, monkeypatch):
    from teleop_control.collection_uploader import CollectionUploader
    monkeypatch.setenv('DATA_COLLECTION_GATE_FILE', str(tmp_path / 'missing'))
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('must not upload'))) as client:
        uploader = CollectionUploader(tmp_path / 'episodes', 'https://platform.test', client=client)
        uploader._set_status(tmp_path / 'round', 'failed', 'connection refused')
        assert uploader.status()['error'] == ''
        assert uploader.status()['pending'] == 1


def test_teleop_context_is_frozen_before_finalization(tmp_path,monkeypatch):
    from teleop_control.collection_gate import collection_context
    spec=importlib.util.spec_from_file_location('g2_context',ROOT/'teleop_control/vendor/remote_collector.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    path=tmp_path/'gate.json';monkeypatch.setenv('DATA_COLLECTION_GATE_FILE',str(path))
    gate=CollectionGate(path,Mock())
    local={'kind':'local','api_url':'http://127.0.0.1:8000','web_url':'http://127.0.0.1:5173'}
    gate._write(True,'local-owner',local)
    context=collection_context();assert context==module.collection_context()
    gate._write(True,'cloud-owner',{'kind':'cloud','api_url':'https://cloud.test','web_url':'https://cloud.test'})
    episode=tmp_path/'episode';episode.mkdir()
    module.mark_episode_ready(episode,session_id='session',session_folder='task',platform_context=context)
    saved=json.loads((episode/module.READY_MARKER).read_text())
    assert saved['author']=='local-owner' and saved['upload_destination']==local
    gate._write(False)
    for reader in (collection_context,module.collection_context):
        with pytest.raises(RuntimeError):reader()
