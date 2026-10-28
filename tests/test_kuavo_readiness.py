from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control.kuavo_readiness import hardware_readiness, require_hardware_ready
from teleop_control.kuavo import KuavoManager


def report(phase='ready', **parameters):
    values={'/robot_version':63, '/hardware/is_ready':1, '/use_external_mpc':True, **parameters}
    return {'event':'preparation_result','error':None,'result':{'ok':True,'error':None,'result':{
        'phase':phase,'controller':{'phase':phase,'parameters':values}}}}


@pytest.mark.parametrize('value', [None, {}, {'phase':'ready'}, {'result':{'error':'offline'}},
    report('not_started'),report('awaiting_confirmation'),report('preparing'),
    report(**{'/robot_version':None}),report(**{'/robot_version':62}),
    report(**{'/hardware/is_ready':True}),report(**{'/hardware/is_ready':0}),
    report(**{'/use_external_mpc':False})])
def test_admission_or_missing_preparation_does_not_prove_hardware_ready(value):
    assert hardware_readiness(value)[0] is False
    with pytest.raises(ValueError):require_hardware_ready(value)


def test_ready_requires_current_controller_metadata():
    assert hardware_readiness(report())[0] is True
    assert require_hardware_ready(report()) is None


def test_connected_service_without_controller_is_blocked_before_worker(tmp_path,monkeypatch):
    manager=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    manager.value.update(service={'management_ready':True,'service':{
        'ready_to_start':True,'readiness_scope':'session_admission_only_not_hardware'}},
        preparation=report('not_started'))
    launch=Mock();monkeypatch.setattr('subprocess.Popen',launch)
    snapshot=manager.snapshot()
    assert not snapshot['hardware_ready'] and '主控未启动' in snapshot['start_blocked_reason']
    with pytest.raises(ValueError,match='主控未启动'):manager.start({'confirm_motion':True})
    launch.assert_not_called()
    manager.value['preparation']=report()
    assert manager.snapshot()['hardware_ready'] and not manager.snapshot()['start_blocked_reason']


def test_cleanup_lock_explains_connected_but_not_admitted_without_starting_worker(tmp_path,monkeypatch):
    from teleop_control.kuavo_readiness import service_block_detail
    service=dict(blocked_reason='worker_exit_without_confirmed_cleanup',owner_present=True,
        cleanup_diagnostics=dict(failed_checks=['arm_hold_unconfirmed']))
    assert '上一会话未确认手臂保持状态' in service_block_detail(service)
    manager=KuavoManager(SimpleNamespace(hardware_enabled=False),root=tmp_path)
    manager.value['service']={'management_ready':True,'service':service}
    launch=Mock();monkeypatch.setattr('subprocess.Popen',launch)
    snapshot=manager.snapshot()
    assert snapshot['service_connected'] and '服务锁定了新会话' in snapshot['service_block_detail']
    assert snapshot['start_blocked_reason']==snapshot['service_block_detail']
    launch.assert_not_called()


def test_blocked_inspection_reads_fields_before_refusal_without_preparing(monkeypatch):
    from teleop_control.kuavo_worker import inspect_service
    service=dict(blocked_reason='worker_exit_without_confirmed_cleanup',owner_present=True,
        cleanup_diagnostics=dict(failed_checks=['arm_hold_unconfirmed']),
        session_contract=dict(protocol='kuavo_robot_session_v3'))
    client=Mock();client.get_status.return_value=dict(management_ready=True,service=service)
    form=dict(values={'left_arm_scale':1.},schema={},editable=False)
    read=Mock(return_value=form);monkeypatch.setattr('teleop_control.kuavo_session_settings.read_settings',read)
    events=[]
    with pytest.raises(ValueError,match='上一会话未确认手臂保持状态'):inspect_service(client,events.append)
    assert events[-1]['session_settings']==form
    client.preparation.assert_not_called()
    read.assert_called_once()
