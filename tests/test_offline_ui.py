"""Verify desktop login remains required without opening a window or moving hardware."""
import sys
from pathlib import Path
import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QCoreApplication
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from qt_ui.bridge import Bridge


@pytest.fixture
def bridge(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    value = Bridge({}, settings_path=tmp_path)
    value.poll_timer.stop()
    yield value
    value.save_timer.stop()
    value.deleteLater()


def test_service_ready_keeps_login_and_device_controls_locked(bridge):
    def request(path, **kwargs):
        assert path == '/platform/auth/preferences'
        kwargs['done']({})
    bridge.request = request
    bridge.serviceReady()
    assert not bridge.state['authenticated']
    calls = []
    bridge.action = lambda *args, **kwargs: calls.append(args)
    bridge.teleop('check')
    assert calls == []
    assert not bridge.can_move()


@pytest.mark.parametrize('response', [
    dict(offline=True, authenticated=False, user=None),
    dict(authenticated=False, user={'username': 'operator'}),
    dict(authenticated=True, user=None),
])
def test_invalid_or_old_offline_login_response_cannot_enter_ui(bridge, response):
    bridge.update(ready=True)
    def action(name, path, payload, done, **kwargs):
        done(response)
    bridge.action = action
    bridge.login('operator', 'wrong', False, False, False)
    assert not bridge.state['authenticated']
    assert bridge.state['error'] == '登录状态无效'


def test_only_verified_login_unlocks_device_controls(bridge):
    bridge.update(ready=True)
    def action(name, path, payload, done, **kwargs):
        assert payload['username'] == 'operator'
        assert payload['password'] == 'verified-password'
        done(dict(authenticated=True, user={'username': 'operator'}))
    bridge.action = action
    bridge.login('operator', 'verified-password', False, False, False)
    assert bridge.state['authenticated']
    assert bridge.state['username'] == 'operator'
    calls = []
    bridge.action = lambda *args, **kwargs: calls.append(args)
    bridge.teleop('check')
    assert calls[0][1] == '/tianji/check'


def test_service_failure_returns_to_startup_and_invalidates_pending_login(bridge):
    bridge.update(ready=True, authenticated=True, username='operator', busy='login')
    epoch, revision = bridge.epoch, bridge.revision
    bridge.serviceFailed('本机服务启动超时')
    assert not bridge.state['ready']
    assert not bridge.state['authenticated']
    assert bridge.state['startupError'] == '本机服务启动超时'
    assert bridge.state['error'] == bridge.state['busy'] == ''
    assert bridge.epoch > epoch and bridge.revision > revision
    bridge.request = lambda path, **kwargs: kwargs['done']({})
    bridge.serviceReady()
    assert bridge.state['ready']
    assert not bridge.state['authenticated']
    assert bridge.state['startupError'] == ''
