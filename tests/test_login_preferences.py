"""Synthetic credentials only; these tests never touch the real keyring."""
import json
from pathlib import Path
import stat
import sys
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleop_control" / "src"))

from teleop_control.app import create_app
from teleop_control.login_credentials import CredentialError, LoginPreferences
from teleop_control.main import MockController
from teleop_control.platform_auth import PlatformLogin, PlatformTokenStore


class MemoryKeyring:
    def __init__(self):
        self.secrets = {}
        self.calls = []

    def call(self, op, scope, key, password=""):
        self.calls.append(op)
        identity = (scope, key)
        if op == "store":
            self.secrets[identity] = password
        elif op == "clear":
            self.secrets.pop(identity, None)
        return self.secrets.get(identity, "") if op == "lookup" else ""


@pytest.fixture
def login(tmp_path, monkeypatch):
    store = PlatformTokenStore(tmp_path / "platform-auth.json")
    prefs = LoginPreferences(store.path, "https://platform.test", MemoryKeyring())
    service = PlatformLogin("https://platform.test", store, prefs)
    def post(url, **kwargs):
        if url.endswith('/auth/token'):
            assert kwargs['json']['password'] == 'synthetic-password'
            return httpx.Response(200, request=httpx.Request('POST', url), json={
                'access_token': 'synthetic-token', 'expires_in': 3600,
                'user': {'id': '1', 'username': 'collector', 'is_admin': False, 'is_active': True},
            })
        return httpx.Response(200, request=httpx.Request('POST', url), json={})
    monkeypatch.setattr(httpx, 'post', post)
    return service


@pytest.mark.parametrize(('remember', 'auto'), [(False, False), (True, False), (True, True), (False, True)])
def test_login_choices_and_restart(login, remember, auto):
    result = login.login('collector', 'synthetic-password', remember_password=remember, auto_login=auto)
    assert result['authenticated'] and not result['user']['is_admin']
    saved = login.preferences.public()
    assert saved['remember_password'] == (remember or auto)
    assert saved['auto_login'] == auto
    assert saved['password_length'] == (len('synthetic-password') if remember or auto else 0)
    assert 'password' not in saved
    assert 'synthetic-password' not in json.dumps(saved)
    for path in login.store.path.parent.glob('*.json'):
        assert 'synthetic-password' not in path.read_text()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    login.store.clear()  # Launcher clears only this token at close/start.
    reopened = PlatformLogin(login.api_url, login.store, login.preferences)
    assert reopened.preferences.public() == saved
    if remember or auto:
        assert reopened.login('collector', '', use_saved_password=True,
                              remember_password=remember, auto_login=auto)['authenticated']
    else:
        assert login.preferences.keyring.calls == []


def test_legacy_preferences_return_only_password_length(login):
    login.login('collector', 'synthetic-password', remember_password=True)
    record = login.preferences.read()
    del record['password_length']
    login.preferences.write(record)
    public = login.preferences.public()
    assert public['password_length'] == len('synthetic-password')
    assert 'synthetic-password' not in json.dumps(public)
    assert login.preferences.read() == record


def test_legacy_preferences_with_unavailable_keyring_preserve_account(login, monkeypatch):
    login.login('collector', 'synthetic-password', remember_password=True)
    record = login.preferences.read()
    del record['password_length']
    login.preferences.write(record)
    def unavailable(*args):
        raise CredentialError('Locked')
    monkeypatch.setattr(login.preferences.keyring, 'call', unavailable)
    public = login.preferences.public()
    assert public['username'] == 'collector'
    assert public['has_saved_password']
    assert public['password_length'] == 0


def test_unchecking_remember_on_next_login_deletes_previous_secret(login):
    login.login('collector', 'synthetic-password', auto_login=True)
    login.login('collector', '', use_saved_password=True)
    assert not login.preferences.public()['remember_password']
    assert not login.preferences.keyring.secrets
    assert login.store.token()


def test_logout_clears_credentials_before_unreachable_cloud(login, monkeypatch):
    login.login('collector', 'synthetic-password', auto_login=True)
    def offline(url, **kwargs):
        assert not login.store.token()
        assert not login.preferences.public()['auto_login']
        assert not login.preferences.keyring.secrets
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(httpx, 'post', offline)
    assert login.logout() == {'ok': True, 'warning': ''}
    assert not login.preferences.public()['username']


def test_keyring_unavailable_never_falls_back_to_plaintext(login, monkeypatch):
    def unavailable(*args):
        raise CredentialError('keyring unavailable')
    monkeypatch.setattr(login.preferences.keyring, 'call', unavailable)
    with pytest.raises(CredentialError):
        login.login('collector', 'synthetic-password', auto_login=True)
    assert not login.preferences.path.exists()
    assert not login.store.token()
    assert login.login('collector', 'synthetic-password')['authenticated']


def test_failed_keyring_deletion_still_disables_auto_login_and_reports_warning(login, monkeypatch):
    login.login('collector', 'synthetic-password', auto_login=True)
    def unavailable(*args):
        raise CredentialError('keyring locked')
    monkeypatch.setattr(login.preferences.keyring, 'call', unavailable)
    result = login.logout()
    assert result['warning']
    assert not login.store.token()
    assert not login.preferences.public()['remember_password']


def test_different_platform_cannot_read_or_send_saved_password(login):
    login.login('collector', 'synthetic-password', auto_login=True)
    other = LoginPreferences(login.store.path, 'https://other.test', login.preferences.keyring)
    assert not other.public()['auto_login']
    with pytest.raises(CredentialError):
        other.password('collector')
    with pytest.raises(CredentialError):
        login.preferences.password('other-user')


def test_invalid_saved_password_disables_future_auto_login(login, monkeypatch):
    login.login('collector', 'synthetic-password', auto_login=True)
    monkeypatch.setattr(httpx, 'post', lambda url, **kw: httpx.Response(
        401, request=httpx.Request('POST', url), json={'detail': 'invalid'},
    ))
    with pytest.raises(httpx.HTTPStatusError):
        login.login('collector', '', use_saved_password=True, auto_login=True)
    assert not login.preferences.public()['auto_login']


def test_logout_wins_over_inflight_login(login, monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    original = httpx.post
    failures = []
    def delayed(url, **kwargs):
        if url.endswith('/auth/token'):
            entered.set()
            assert resume.wait(3)
        return original(url, **kwargs)
    monkeypatch.setattr(httpx, 'post', delayed)
    def run():
        try:
            login.login('collector', 'synthetic-password', auto_login=True)
        except CredentialError as exc:
            failures.append(str(exc))
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(3)
        login.logout()
    finally:
        resume.set()
        thread.join(3)
    assert not thread.is_alive()
    assert failures
    assert not login.store.token()
    assert not login.preferences.public()['auto_login']
    assert not login.preferences.keyring.secrets


def test_preferences_api_never_returns_password_and_blocks_foreign_origins(login):
    login.login('collector', 'synthetic-password', auto_login=True)
    app = create_app(MockController(), cors_origins=['http://127.0.0.1:4173'], platform_login=login)
    with TestClient(app) as client:
        response = client.get('/platform/auth/preferences', headers={'Origin': 'http://127.0.0.1:4173'})
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert 'synthetic-password' not in response.text
        assert response.json()['auto_login']
        assert response.json()['password_length'] == len('synthetic-password')
        assert client.get('/platform/auth/preferences', headers={'Origin': 'https://foreign.test'}).status_code == 403
        denied = client.post('/platform/auth/login', headers={'Origin': 'https://foreign.test'}, json={
            'username': 'collector', 'password': '', 'use_saved_password': True,
        })
        assert denied.status_code == 403
        assert client.post('/platform/auth/logout').json()['ok']
        assert not client.get('/platform/auth/preferences').json()['auto_login']
        accepted = client.post('/platform/auth/login', json={
            'username': 'collector', 'password': 'synthetic-password',
            'remember_password': True, 'auto_login': True,
        })
        assert accepted.status_code == 200
        assert accepted.json()['authenticated']
        assert client.get('/platform/auth/preferences').json()['auto_login']
        login.store.clear()
        resumed = client.post('/platform/auth/login', json={
            'username': 'collector', 'password': '',
            'use_saved_password': True, 'remember_password': True, 'auto_login': True,
        })
        assert resumed.status_code == 200 and resumed.json()['authenticated']
        assert 'synthetic-password' not in resumed.text
