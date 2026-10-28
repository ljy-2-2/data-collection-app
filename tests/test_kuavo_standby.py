"""Prepared-session lifecycle against owned fake processes; no robot access."""
import io
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control.kuavo import KuavoManager, Options


def ready_packet(**changes):
    return dict(session=dict(phase='running',arm_state='waiting_release',left_gripper='READY',
        right_gripper='READY',robot_report_monotonic_ns=time.monotonic_ns()),
        input_enabled=False,input_neutral=True,pico_live=True,**changes)


def prepared(tmp_path):
    manager=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    manager.process=SimpleNamespace(stdin=io.StringIO(),poll=lambda:None)
    manager.standby=True;manager.prepared_mode='left';manager.session_id='owned'
    manager._merge(ready_packet())
    return manager


def test_start_activates_existing_prepared_process_without_relaunch(tmp_path,monkeypatch):
    manager=prepared(tmp_path)
    launch=Mock(side_effect=AssertionError('prepared process must be reused'))
    monkeypatch.setattr('subprocess.Popen',launch)
    assert manager.snapshot()['control_ready']
    with pytest.raises(ValueError,match='双臂＋底盘'):manager.start(dict(confirm_motion=True,arm_mode='dual_base'))
    assert not manager.process.stdin.getvalue()
    manager.start(dict(confirm_motion=True,arm_mode='right',require_prepared=True))
    assert json.loads(manager.process.stdin.getvalue())=={'action':'activate','arm_mode':'right'}
    assert not manager.snapshot()['control_ready']
    packet=ready_packet();packet['input_enabled']=True
    manager._merge(packet)
    assert manager.snapshot()['state']=='running' and not manager.standby
    launch.assert_not_called()


@pytest.mark.parametrize('change',[
    {'input_neutral':False}, {'pico_live':False},
    {'session':{'phase':'running','left_gripper':'WAIT_FEEDBACK','right_gripper':'READY'}},
    {'session':{'phase':'running','left_gripper':'READY','right_gripper':'READY','arm_state':'paused',
                'robot_report_monotonic_ns':0}},
])
def test_unready_or_stale_preparation_never_activates(tmp_path,change):
    manager=prepared(tmp_path)
    packet=ready_packet();packet.update(change);manager._merge(packet)
    assert not manager.snapshot()['control_ready']
    with pytest.raises(ValueError):manager.start(dict(confirm_motion=True,arm_mode='left'))
    assert not manager.process.stdin.getvalue()


def test_plain_idle_and_cancelled_preparation_cannot_start(tmp_path):
    manager=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    with pytest.raises(ValueError,match='先检查设备'):manager.start(dict(confirm_motion=True,require_prepared=True))
    manager=prepared(tmp_path)
    manager.stop('owned')
    manager._merge(ready_packet())  # Late heartbeat cannot undo local cancellation.
    assert manager.snapshot()['state']=='stopping' and not manager.snapshot()['control_ready']
    with pytest.raises(ValueError):manager.start(dict(confirm_motion=True,arm_mode='left'))
    assert manager.process.stdin.getvalue()=='stop\n'


def ended_session(tmp_path,code=0):
    manager=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    manager.process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO(),poll=lambda:code,wait=lambda:code)
    manager.session_reached_running=True
    manager.activation_requested=True
    manager.value['service']={'service':{'ready_to_start':True,'owner_present':False}}
    manager._read(manager.process)
    return manager


def restart_options(**changes):
    return Options(confirm_motion=True,motor_ids_confirmed=True,prepare_controller=True,
        prepare_grippers=True,prepare_session=True,require_prepared=True,
        restart_after_stop=True,**changes).model_dump()


def test_explicit_restart_prepares_then_activates_selected_mode(tmp_path,monkeypatch):
    manager=ended_session(tmp_path)
    assert manager.snapshot()['can_restart'] and not manager.active()
    seen=[]
    def inspect(options,**kwargs):
        seen.append((options,kwargs))
        assert not manager.restart_available
        assert kwargs['_generation']==manager.preparation_generation
        assert kwargs['connect_cameras'] is False
        manager.standby=True
        manager.process=SimpleNamespace(stdin=io.StringIO(),poll=lambda:None)
        manager._merge(ready_packet())
    monkeypatch.setattr(manager,'inspect',inspect)
    monkeypatch.setattr(manager.collection,'arm',Mock())
    result=manager.start(restart_options(arm_mode='right',task='next episode'))
    assert len(seen)==1 and seen[0][0]['prepare_controller']
    assert json.loads(manager.process.stdin.getvalue())=={'action':'activate','arm_mode':'right'}
    assert manager.collection.arm.call_args.args[0]['task']=='next episode'
    assert result['state']=='starting' and not result['can_restart']
    with pytest.raises(ValueError):manager.start(restart_options())
    assert len(seen)==1


@pytest.mark.parametrize('reason',['failure','cancel','generation'])
def test_restart_never_activates_after_failure_or_cancellation(tmp_path,monkeypatch,reason):
    manager=ended_session(tmp_path);stream=io.StringIO()
    arm=Mock();monkeypatch.setattr(manager.collection,'arm',arm)
    def inspect(options,**kwargs):
        if reason=='failure':raise RuntimeError('robot preparation refused')
        manager.standby=True;manager.process=SimpleNamespace(stdin=stream,poll=lambda:None)
        manager._merge(ready_packet())
        if reason=='cancel':manager.check_cancel.set()
        else:manager.preparation_generation+=1
    monkeypatch.setattr(manager,'inspect',inspect)
    with pytest.raises(RuntimeError):manager.start(restart_options())
    assert not stream.getvalue() and not manager.snapshot()['can_restart']
    arm.assert_not_called()


@pytest.mark.parametrize('case',['failed_exit','blocked_service','owner_present','cold_start','saving'])
def test_restart_requires_clean_end_and_no_conflicting_work(tmp_path,monkeypatch,case):
    manager=ended_session(tmp_path,1 if case=='failed_exit' else 0)
    if case=='blocked_service':manager.value['service']['service']['blocked_reason']='worker_exit_without_confirmed_cleanup'
    if case=='owner_present':manager.value['service']['service']['owner_present']=True
    if case=='cold_start':manager.restart_available=False
    if case=='saving':manager.collection.phase='finalizing'
    inspect=Mock();monkeypatch.setattr(manager,'inspect',inspect)
    assert not manager.snapshot()['can_restart']
    with pytest.raises(ValueError):manager.start(restart_options())
    inspect.assert_not_called()


def test_restart_request_requires_preparation_permissions():
    with pytest.raises(ValueError):Options(restart_after_stop=True,confirm_motion=True)


def test_check_prepares_and_waits_before_returning(tmp_path,monkeypatch):
    manager=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    options=Options(prepare_controller=True,prepare_grippers=True,prepare_session=True,
                    confirm_motion=True,motor_ids_confirmed=True).model_dump()
    monkeypatch.setattr(manager,'_launch_options',lambda options:(['fake-python'],{},dict(options)))
    monkeypatch.setattr(manager,'_start_monitor',Mock())
    monkeypatch.setattr(manager,'_inspect_preparing',lambda *a:manager.value.update(gripper_preparation_ready=True))
    process=SimpleNamespace(stdin=io.StringIO(),poll=lambda:None)
    monkeypatch.setattr('subprocess.Popen',Mock(return_value=process))
    monkeypatch.setattr('threading.Thread.start',lambda _:None)
    def wait():
        assert manager.operation.locked() and manager.pending_operation
        payload=json.loads(process.stdin.getvalue())
        assert payload['standby'] and payload['arm_mode']=='dual'
        assert not manager.snapshot()['control_ready']
        manager._merge(ready_packet())
    monkeypatch.setattr(manager,'_wait_prepared',wait)
    result=manager.inspect(options)
    assert result['state']=='ready' and result['control_ready'] and result['active']
    assert not manager.pending_operation
    assert result['prestart_editable']


def test_preparation_timeout_stops_only_owned_session(tmp_path):
    manager=prepared(tmp_path)
    with pytest.raises(RuntimeError,match='超时'):manager._wait_prepared(0)
    assert manager.process.stdin.getvalue()=='stop\n' and not manager.snapshot()['control_ready']


def finishable_prepared(tmp_path):
    manager=prepared(tmp_path)
    code=[None]
    manager.process.poll=lambda:code[0]
    def wait(**kwargs):code[0]=0;return 0
    manager.process.wait=wait
    manager.preparation_options=Options(prepare_controller=True,prepare_grippers=True,prepare_session=True,
        confirm_motion=True,motor_ids_confirmed=True,arm_mode='left').model_dump()
    return manager


def test_edit_prepared_session_stops_saves_readback_then_reprepares(tmp_path,monkeypatch):
    from teleop_control.kuavo import SessionSettingsRequest
    manager=finishable_prepared(tmp_path);process=manager.process
    monkeypatch.setattr(manager,'_launch_options',lambda body:(['fake-python'],{},body))
    resume=Mock();monkeypatch.setattr(manager,'_resume_preparation',resume)
    form={'revision':3,'values':{'left_arm_speed_rad_s':.5},'editable':True}
    def save(*args,**kwargs):
        assert process.poll()==0 and process.stdin.getvalue()=='stop\n'
        payload=json.loads(kwargs['input'])['settings_request']
        assert payload['changes']=={'left_arm_speed_rad_s':.5}
        assert payload['expected_revision']==2
        return SimpleNamespace(returncode=0,stdout='KUAVO:'+json.dumps({'session_settings':form})+'\n',stderr='')
    monkeypatch.setattr('subprocess.run',save)
    result=manager.session_settings(SessionSettingsRequest(action='apply',confirm=True,
        changes={'left_arm_speed_rad_s':.5},expected_revision=2,expected_service_instance_id='a'*32,
        expected_installation_sha256='b'*64))
    assert result['session_settings']==form and not result['control_ready']
    assert result['prestart_editable'] and manager.process is None
    assert resume.call_args.args[0][0]['arm_mode']=='left'


def test_return_during_parameter_write_prevents_background_restart(tmp_path,monkeypatch):
    from teleop_control.kuavo import SessionSettingsRequest
    manager=finishable_prepared(tmp_path)
    launch=Mock(return_value=(['fake-python'],{},{}));monkeypatch.setattr(manager,'_launch_options',launch)
    def save(*args,**kwargs):
        manager._request_preparation_cancel()  # Return wins over automatic resume.
        return SimpleNamespace(returncode=0,stdout='KUAVO:{"session_settings":{"revision":3}}\n',stderr='')
    monkeypatch.setattr('subprocess.run',save)
    monkeypatch.setattr('threading.Thread.start',lambda thread:thread.run())
    manager.session_settings(SessionSettingsRequest(action='apply',confirm=True,
        changes={'left_arm_speed_rad_s':.5},expected_revision=2,expected_service_instance_id='a'*32,
        expected_installation_sha256='b'*64))
    assert launch.call_count==1 and manager.preparation_options is None and manager.process is None
    assert manager.snapshot()['state']=='idle'


def test_cancel_device_check_before_session_exists_prevents_next_stage(tmp_path,monkeypatch):
    import threading
    from teleop_control.kuavo import PreparationCancelled
    manager=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    entered=threading.Event();errors=[]
    monkeypatch.setattr(manager,'_launch_options',lambda body:(['fake-python'],{},body))
    monkeypatch.setattr(manager,'_stop_monitor',Mock())
    start=Mock(side_effect=AssertionError('cancelled check must not launch session'));monkeypatch.setattr(manager,'start',start)
    def checking(*args):
        entered.set()
        assert manager.check_cancel.wait(2)
        raise PreparationCancelled('cancelled')
    monkeypatch.setattr(manager,'_inspect_preparing',checking)
    def run():
        try:manager.inspect(Options(prepare_controller=True,prepare_grippers=True,prepare_session=True,
            confirm_motion=True,motor_ids_confirmed=True).model_dump())
        except PreparationCancelled:errors.append('cancelled')
    thread=threading.Thread(target=run);thread.start()
    assert entered.wait(2)
    result=manager.cancel_preparation();thread.join(2)
    assert not thread.is_alive() and errors==['cancelled']
    assert result['state']=='idle' and not result['active'] and manager.preparation_options is None
    start.assert_not_called()


def test_running_session_rejects_return_and_parameter_edit(tmp_path):
    from teleop_control.kuavo import SessionSettingsRequest
    manager=prepared(tmp_path)
    manager.activation_requested=True
    with pytest.raises(RuntimeError,match='运行'):manager.cancel_preparation()
    with pytest.raises(RuntimeError,match='运行'):manager.session_settings(SessionSettingsRequest())
    assert not manager.process.stdin.getvalue()


def test_saved_revision_is_used_by_automatic_preparation(tmp_path,monkeypatch):
    from teleop_control.kuavo import SessionSettingsRequest
    manager=finishable_prepared(tmp_path);old=manager.process
    form=dict(values={'left_arm_speed_rad_s':.5},revision=3,settings_sha256='c'*64,
              service_instance_id='a'*32,installation_sha256='b'*64,editable=True)
    monkeypatch.setattr(manager,'_launch_options',lambda options:(['fake-python'],{},dict(options)))
    monkeypatch.setattr('subprocess.run',lambda *a,**kw:SimpleNamespace(returncode=0,
        stdout='KUAVO:'+json.dumps({'session_settings':form})+'\n',stderr=''))
    new=SimpleNamespace(stdin=io.StringIO(),poll=lambda:None)
    monkeypatch.setattr('subprocess.Popen',Mock(return_value=new))
    monkeypatch.setattr('threading.Thread.start',lambda thread:thread.run())
    monkeypatch.setattr(manager,'_read',lambda _:None)
    monkeypatch.setattr(manager,'_stderr',lambda _:None)
    monkeypatch.setattr(manager,'_start_monitor',Mock())
    def check(*args):
        assert old.poll()==0 and manager.value['session_settings']==form
        manager.value.update(service={'service':{'session_contract':{'protocol':'kuavo_robot_session_v3'}}},
                             gripper_preparation_ready=True)
    monkeypatch.setattr(manager,'_inspect_preparing',check)
    def wait():
        payload=json.loads(new.stdin.getvalue())
        assert payload['standby'] and payload['expected_settings']['revision']==3
        assert payload['expected_settings']['settings_sha256']=='c'*64
        manager._merge(ready_packet())
    monkeypatch.setattr(manager,'_wait_prepared',wait)
    manager.session_settings(SessionSettingsRequest(action='apply',confirm=True,
        changes={'left_arm_speed_rad_s':.5},expected_revision=2,expected_service_instance_id='a'*32,
        expected_installation_sha256='b'*64))
    assert manager.process is new and manager.snapshot()['control_ready']
    assert 'activate' not in new.stdin.getvalue()
