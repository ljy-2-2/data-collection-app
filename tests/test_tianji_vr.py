"""Local VR recovery, with all service and SDK operations simulated."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'teleop_control/src'))
from teleop_control import tianji_vr
from teleop_control.tianji import TianjiManager
from teleop_control.main import MockController


def receiver(connected):
    return Mock(snapshot=Mock(return_value=(object(), False, 'timestamp stopped', connected)))


@pytest.fixture
def connection(monkeypatch):
    seconds = [0.0]
    monkeypatch.setattr(tianji_vr.time, 'monotonic', lambda: seconds[0])
    cancelled = threading.Event()
    monkeypatch.setattr(cancelled, 'wait', lambda dt: seconds.__setitem__(0,seconds[0]+dt))
    old, fresh = receiver(False), receiver(True)
    factory = Mock(side_effect=[old,fresh])
    service = Mock()
    ensure = Mock()
    vr = tianji_vr.LocalVRConnection(dict(service_dir='/unused', vr_timeout_s=.2),
        input_factory=factory, ensure_service=ensure, service=service, cancelled=cancelled)
    return vr,old,fresh,service,ensure,factory


def test_first_check_uses_real_data_without_restart_when_healthy(connection):
    vr,old,fresh,service,ensure,factory=connection
    factory.side_effect=[fresh]
    vr.check()
    ensure.assert_called_once()
    service.restart.assert_not_called()
    assert vr.snapshot()[3] and vr.snapshot()[2]==''


def test_stalled_service_is_restarted_once_and_sdk_rebuilt(connection):
    vr,old,fresh,service,ensure,factory=connection
    order=[]
    old.close.side_effect=lambda:order.append('close')
    service.restart.side_effect=lambda:order.append('restart')
    vr.check()
    assert order==['close','restart']
    assert factory.call_count==2
    assert vr.input is fresh and vr.snapshot()[3]
    service.restart.assert_called_once()


def test_repeated_check_reuses_healthy_connection(connection):
    vr,old,fresh,service,ensure,factory=connection
    vr.input=fresh
    vr.check()
    service.restart.assert_not_called()
    factory.assert_not_called()
    fresh.close.assert_not_called()


def test_repeated_check_recovers_disconnected_existing_receiver(connection):
    vr,old,fresh,service,ensure,factory=connection
    vr.input=old
    factory.side_effect=[fresh]
    vr.check()
    old.close.assert_called_once()
    service.restart.assert_called_once()
    assert vr.input is fresh


def test_listening_service_without_fresh_frames_remains_offline(connection):
    vr,old,fresh,service,ensure,factory=connection
    fresh.snapshot.return_value=(None,False,'no data',False)
    with pytest.raises(RuntimeError,match='Send Data'):vr.check()
    assert not vr.snapshot()[3]
    assert '时间戳' in vr.snapshot()[2]
    assert service.restart.call_count==1
    # A later real update restores status without needing another restart.
    fresh.snapshot.return_value=(object(),False,'',True)
    assert vr.snapshot()[3] and vr.snapshot()[2]==''


@pytest.mark.parametrize('failure',['start','close','restart','sdk'])
def test_failed_recovery_surfaces_error_and_stays_offline(connection,failure):
    vr,old,fresh,service,ensure,factory=connection
    if failure=='start':ensure.side_effect=RuntimeError('failed start')
    elif failure=='close':old.close.side_effect=RuntimeError('failed close')
    elif failure=='restart':service.restart.side_effect=RuntimeError('failed restart')
    else:factory.side_effect=RuntimeError('failed sdk')
    with pytest.raises(RuntimeError,match='failed'):vr.check()
    assert not vr.snapshot()[3]
    assert 'failed' in vr.snapshot()[2]
    assert service.restart.call_count == int(failure=='restart')


@pytest.mark.parametrize('during_close',[False,True])
def test_cancellation_prevents_service_restart(connection,during_close):
    vr,old,fresh,service,ensure,factory=connection
    if during_close:old.close.side_effect=vr.cancelled.set
    else:vr.cancelled.set()
    with pytest.raises(RuntimeError,match='已取消'):vr.check()
    service.restart.assert_not_called()


@pytest.mark.parametrize('busy',['umi','armed','saving','g2'])
def test_manager_rejects_check_before_worker_or_vr_mutation(monkeypatch,busy):
    controller=MockController()
    umi=SimpleNamespace(status=lambda:SimpleNamespace(state='running' if busy=='umi' else 'idle'))
    m=TianjiManager(controller,umi=umi)
    if busy=='armed':m.collection.armed=True
    if busy=='saving':monkeypatch.setattr(m.collection,'active',lambda:True)
    if busy=='g2':monkeypatch.setattr(m,'require_other_idle',Mock(side_effect=ValueError('G2 busy')))
    monkeypatch.setattr(m,'_launch',Mock())
    monkeypatch.setattr(m,'_command',Mock())
    with pytest.raises(ValueError):m.command('check')
    m._launch.assert_not_called()
    m._command.assert_not_called()
    m.collection.armed=False
    if busy=='saving':monkeypatch.setattr(m.collection,'active',lambda:False)


def test_disconnect_closes_sdk_and_desktop_bridge_even_if_sdk_close_fails(connection):
    vr,old,fresh,service,ensure,factory=connection
    vr.input=old
    old.close.side_effect=RuntimeError('SDK close failed')
    with pytest.raises(RuntimeError,match='SDK close'):vr.close()
    service.stop.assert_called_once()
