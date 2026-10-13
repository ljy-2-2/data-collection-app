"""Exercise the desktop helper boundary using synthetic credentials only."""
import json
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'teleop_control/src'))
from teleop_control import keyring_helper
from teleop_control.login_credentials import CredentialError, DesktopKeyring


def test_keyring_keeps_secret_out_of_argv_and_waits_for_desktop_unlock(monkeypatch):
    def run(args, **kwargs):
        assert 'synthetic-password' not in ' '.join(args)
        assert json.loads(kwargs['input']) == {'password': 'synthetic-password'}
        assert kwargs['timeout'] >= 90
        return SimpleNamespace(returncode=0, stdout='{}')
    monkeypatch.setattr(subprocess, 'run', run)
    assert DesktopKeyring().call('store', 'scope', 'key', 'synthetic-password') == ''


@pytest.mark.parametrize('code, expected', [
    ('missing_dependency', '缺少密码保存组件'),
    ('missing_provider', '缺少钥匙环服务'),
    ('no_session_bus', '普通用户从桌面'),
    ('service_unavailable', '钥匙环服务未启动'),
    ('locked', '尚未解锁'),
    ('cancelled', '授权已取消'),
    ('verification_failed', '未能可靠保存'),
    ('unknown', '系统钥匙环不可用'),
])
def test_keyring_errors_are_actionable_and_never_repeat_private_output(monkeypatch, code, expected):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout=json.dumps({'error': code, 'debug': 'synthetic-password'}),
        stderr='synthetic-password'))
    with pytest.raises(CredentialError, match=expected) as error:
        DesktopKeyring().call('store', 'scope', 'key', 'synthetic-password')
    assert 'synthetic-password' not in str(error.value)


def test_keyring_timeout_does_not_expose_input_or_output(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 90, output='synthetic-password')
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(CredentialError, match='授权超时') as error:
        DesktopKeyring().call('store', 'scope', 'key', 'synthetic-password')
    assert 'synthetic-password' not in str(error.value)


def test_existing_desktop_bus_is_preserved(monkeypatch):
    address = 'unix:path=/tmp/synthetic-desktop-bus'
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', address)
    assert keyring_helper.desktop_bus_address()
    assert keyring_helper.os.environ['DBUS_SESSION_BUS_ADDRESS'] == address


@pytest.mark.parametrize('owner, mode, expected', [
    (1000, stat.S_IFSOCK, True),
    (2000, stat.S_IFSOCK, False),
    (1000, stat.S_IFREG, False),
])
def test_missing_bus_uses_only_same_user_desktop_socket(monkeypatch, owner, mode, expected):
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
    monkeypatch.setattr(keyring_helper.os, 'getuid', lambda: 1000)
    monkeypatch.setattr(Path, 'stat', lambda self: SimpleNamespace(st_uid=owner, st_mode=mode))
    assert keyring_helper.desktop_bus_address() is expected
    assert keyring_helper.os.environ.get('DBUS_SESSION_BUS_ADDRESS') == (
        'unix:path=/run/user/1000/bus' if expected else None)


def test_missing_bus_does_not_start_transient_session(monkeypatch):
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
    def absent(self):
        raise FileNotFoundError()
    monkeypatch.setattr(Path, 'stat', absent)
    assert not keyring_helper.desktop_bus_address()
    assert not keyring_helper.os.environ.get('DBUS_SESSION_BUS_ADDRESS')


@pytest.mark.parametrize('owned, activatable, expected', [
    (True, [], True),
    (False, ['org.freedesktop.secrets'], True),
    (False, ['unrelated.desktop.service'], False),
])
def test_provider_detection_accepts_running_or_dbus_activatable_provider(owned, activatable, expected):
    methods = []
    def call_sync(destination, path, interface, method, *args):
        methods.append(method)
        assert destination == 'org.freedesktop.DBus'
        value = owned if method == 'NameHasOwner' else activatable
        return SimpleNamespace(unpack=lambda: (value,))
    gio = SimpleNamespace(BusType=SimpleNamespace(SESSION=1),
                          DBusCallFlags=SimpleNamespace(NONE=0),
                          bus_get_sync=lambda *args: SimpleNamespace(call_sync=call_sync))
    glib = SimpleNamespace(Variant=lambda *args: args)
    assert keyring_helper.provider_available(gio, glib) is expected
    assert methods == (['NameHasOwner'] if owned else ['NameHasOwner', 'ListActivatableNames'])


def test_missing_provider_is_reported_before_attempting_secret_operation(monkeypatch, capsys):
    # A missing package must not become the misleading "log out and retry".
    monkeypatch.setitem(sys.modules, 'gi', SimpleNamespace(require_version=lambda *args: None))
    monkeypatch.setitem(sys.modules, 'gi.repository',
                        SimpleNamespace(Gio=object(), GLib=object(), Secret=object()))
    monkeypatch.setattr(keyring_helper, 'desktop_bus_address', lambda: True)
    monkeypatch.setattr(keyring_helper, 'provider_available', lambda *args: False)
    assert keyring_helper.main() == 1
    assert json.loads(capsys.readouterr().out) == {'error': 'missing_provider'}
