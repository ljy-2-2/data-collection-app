"""Local preview reconnection only; no hardware, SSH or service commands."""
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'teleop_control/src'))
from teleop_control import tianji


@pytest.fixture
def manager(monkeypatch, tmp_path):
    m = tianji.TianjiManager(SimpleNamespace(hardware_enabled=True))
    monkeypatch.setattr(m, 'snapshot', lambda **kw: dict(state='idle', collection=m.collection.snapshot()))
    monkeypatch.setattr(m, 'require_other_idle', Mock())
    monkeypatch.setattr(m.camera_recovery_cancel, 'wait', lambda seconds: m.camera_recovery_cancel.is_set())
    monkeypatch.setattr(tianji, 'RUNTIME', tmp_path)
    monkeypatch.setattr(tianji.subprocess, 'run', Mock(side_effect=AssertionError('No system commands allowed')))
    monkeypatch.setattr(tianji.subprocess, 'Popen', Mock(side_effect=AssertionError('No processes allowed')))
    client = MagicMock()
    client.__enter__.return_value = client
    client.status.return_value = dict(idle=True, fault=None, max_stream_seconds=60)
    client.list_cameras.return_value = [dict(id=c, available=True) for c in ('usb-1','usb-2','usb-4')]
    monkeypatch.setattr(m, 'new_camera_client', Mock(return_value=client))
    yield m, client
    # The tests leave no live receiver; avoid atexit acting on mocks.
    m.camera_client = m.camera_thread = None
    assert not list(tmp_path.iterdir())


def test_healthy_three_camera_preview_is_kept(manager):
    m, client = manager
    m.camera_thread = Mock(is_alive=lambda: True)
    m.camera_phase = 'streaming'
    m.frames = {c:(b'jpeg',time.monotonic()) for c in tianji.CAMERAS}
    m.check_camera_recovery()
    m.camera_thread.join.assert_not_called()
    m.new_camera_client.assert_not_called()
    client.recover_camera.assert_not_called()


@pytest.mark.parametrize('blocked', ['armed', 'busy', 'recording', 'moving', 'simulation', 'other_task'])
def test_active_tasks_block_local_reconnection(manager, monkeypatch, blocked):
    m, client = manager
    state = dict(state='idle', collection={})
    if blocked in ('armed', 'busy', 'recording'): state['collection'][blocked] = True
    if blocked == 'moving': state['state'] = 'moving'
    if blocked == 'simulation': m.controller.hardware_enabled = False
    if blocked == 'other_task': m.require_other_idle.side_effect = ValueError('其他任务正在运行')
    monkeypatch.setattr(m, 'snapshot', lambda **kw: state)
    m.camera_client = client
    with pytest.raises(RuntimeError): m.check_camera_recovery()
    client.close.assert_not_called()
    client.request_stop.assert_not_called()
    assert m.camera_phase == 'fault'


def test_failed_receiver_clears_local_state_without_remote_calls(manager):
    m, client = manager
    m.camera_client = client
    m.camera_thread = Mock(is_alive=lambda: False)
    m.camera_error = 'old connection error'
    m.camera_session = object()
    m.frames = {'usb_1':(b'old',0)}
    m.check_camera_recovery()
    assert m.camera_client is m.camera_thread is m.camera_session is None
    assert m.frames == {} and m.camera_error == ''
    assert m.camera_phase == 'connecting'  # Not proof of restored images.
    assert not m.camera_preview_restarting
    client.close.assert_called_once()
    client.status.assert_not_called()
    client.recover_camera.assert_not_called()
    assert not m.diagnostic_events


@pytest.mark.parametrize('stuck,broken', [(False,False),(True,False),(False,True)])
def test_stale_preview_waits_for_old_receiver_before_reconnecting(manager, stuck, broken):
    m, client = manager
    thread = Mock()
    thread.is_alive.side_effect = [True,True,True,stuck]
    m.camera_thread, m.camera_client = thread, client
    m.camera_phase = 'fault'
    if broken: client.request_stop.side_effect = OSError('disconnected')
    if stuck:
        with pytest.raises(RuntimeError, match='线程尚未退出'): m.check_camera_recovery()
        assert m.camera_thread is thread
    else:
        m.check_camera_recovery()
        assert m.camera_thread is None
        assert m.camera_phase == 'connecting'
    assert [c.kwargs for c in thread.join.call_args_list] == [dict(timeout=6),dict(timeout=3)]
    client.recover_camera.assert_not_called()


def test_cancel_before_cleanup_does_not_touch_connection(manager):
    m, client = manager
    m.camera_client = client
    m.camera_recovery_cancel.set()
    with pytest.raises(RuntimeError, match='已取消'): m.check_camera_recovery()
    client.close.assert_not_called()


def test_cancel_while_joining_does_not_reopen_preview(manager):
    m, client = manager
    thread = Mock()
    thread.is_alive.side_effect = [True,True,False,False]
    thread.join.side_effect = lambda **kw: m.camera_recovery_cancel.set()
    m.camera_thread, m.camera_client = thread, client
    with pytest.raises(RuntimeError, match='已取消'): m.check_camera_recovery()
    assert m.camera_stop.is_set()
    assert m.camera_phase == 'fault'


def test_button_cleans_local_receiver_before_starting_new_one(manager, monkeypatch):
    m, client = manager
    order = []
    m.camera_client = client
    client.close.side_effect = lambda: order.append('close')
    def reply(message):
        event, box = m.pending[message['id']]
        box['ok'] = True; event.set()
    monkeypatch.setattr(m, '_write', reply)
    monkeypatch.setattr(m, 'active', lambda: True)
    monkeypatch.setattr(m, 'start_cameras', lambda: order.append('start'))
    m.last_access = 0
    assert m.command('check')['ok']
    assert time.monotonic() - m.last_access < 1
    assert order == ['close', 'start']
    monkeypatch.setattr(m, 'active', lambda: False)


def test_stop_cancels_pending_reconnect(manager, monkeypatch):
    m, _ = manager
    def reply(message):
        event, box = m.pending[message['id']]
        box['ok'] = True; event.set()
    monkeypatch.setattr(m, '_write', reply)
    m.command('stop')
    assert m.camera_recovery_cancel.is_set()


@pytest.mark.parametrize('failure', ['controller_fault', 'unreachable'])
def test_local_restart_does_not_claim_controller_is_repaired(manager, failure):
    m, client = manager
    if failure == 'controller_fault':
        client.status.return_value.update(fault='Audit changed: dmesg_sha256', recoverable_camera_fault=True)
    else:
        client.status.side_effect = ConnectionRefusedError('connection refused')
    m.check_camera_recovery()
    m.camera_stop.clear()
    m._cameras()
    assert m.camera_phase == 'fault'
    assert m.camera_error
    if failure == 'controller_fault': assert '本地重连无法解除' in m.camera_error
    client.stream.assert_not_called()
    client.recover_camera.assert_not_called()


def test_fresh_preview_receives_three_images_after_local_reconnect(manager):
    m, client = manager
    m.check_camera_recovery()
    m.camera_stop.clear()
    def stream(**kw):
        for key in ('usb-1', 'usb-2', 'usb-4'):
            yield SimpleNamespace(metadata=dict(type='image',camera_id=key,format='mjpeg'),data=b'jpeg')
        assert set(m.frames) == set(tianji.CAMERAS)
        assert not m.camera_error
        m.camera_stop.set()
        yield SimpleNamespace(metadata=dict(type='end',ok=True),data=b'')
    client.stream.side_effect = stream
    m._cameras()
    client.stream.assert_called_once()
    client.recover_camera.assert_not_called()
    assert m.camera_phase == 'idle' and not m.camera_error
