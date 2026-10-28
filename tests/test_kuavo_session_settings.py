"""Offline integration for the October 26 client and versioned parameters."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control.kuavo import KuavoManager, SessionSettingsRequest

ROOT=Path(__file__).resolve().parents[1]


def test_sdk_runs_from_relocated_app_without_reference_project(tmp_path):
    app=tmp_path/'standalone-app'
    for name in ('teleop_control/src','teleop_control/vendor/kuavo_runtime','tests/fixtures/kuavo_settings'):
        shutil.copytree(ROOT/name,app/name,ignore=shutil.ignore_patterns('__pycache__'))
    fixture='tests/fixtures/kuavo_session_settings_fake.py'
    shutil.copy2(ROOT/fixture,app/fixture)
    env={**os.environ,'PYTHONPATH':str(app/'teleop_control/src'),'PYTHONDONTWRITEBYTECODE':'1'}
    result=subprocess.run([sys.executable,str(app/fixture)],cwd=app,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.parametrize('body',[
    {'action':'execute'}, {'action':'apply'}, {'action':'reset'},
    {'action':'read','changes':{'left_arm_scale':.5}},
    {'action':'preview','changes':{'left_arm_scale':True}},
    {'action':'preview','changes':{'left_arm_scale':float('nan')}},
    {'action':'apply','confirm':True,'expected_revision':True},
])
def test_invalid_settings_requests_rejected(body):
    with pytest.raises(ValueError):SessionSettingsRequest(**body)


def manager(tmp_path):
    m=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    cfg=m.settings.connection_path;cfg.parent.mkdir(parents=True);cfg.write_text('{}')
    return m


def test_busy_manager_never_launches_parameter_worker(tmp_path,monkeypatch):
    m=manager(tmp_path);m.pending_operation=True
    run=Mock();monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(RuntimeError):m.session_settings(SessionSettingsRequest())
    run.assert_not_called()


def test_failed_parameter_write_invalidates_cached_settings_without_retry(tmp_path,monkeypatch):
    m=manager(tmp_path);m.value['session_settings']={'revision':2}
    monkeypatch.setattr(m,'_launch_options',lambda options:(['python'],{},options))
    run=Mock(side_effect=subprocess.TimeoutExpired('worker',30));monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(subprocess.TimeoutExpired):m.session_settings(SessionSettingsRequest())
    assert run.call_count==1 and not m.active() and m.value['session_settings'] is None


def test_current_parameters_are_pinned_and_not_double_scaled(tmp_path,monkeypatch):
    m=manager(tmp_path)
    from teleop_control.kuavo_settings import MotionSettings
    m.settings.save_motion(MotionSettings(position_scale=.5))
    form=dict(revision=2,settings_sha256='c'*64,service_instance_id='a'*32,installation_sha256='b'*64,editable=True)
    m.value.update(service={'service':{'session_contract':{'protocol':'kuavo_robot_session_v3'}}},session_settings=form)
    monkeypatch.setattr(m,'_launch_options',lambda options:(['python'],{},options))
    process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO(),stderr=io.StringIO(),poll=lambda:None)
    monkeypatch.setattr(subprocess,'Popen',Mock(return_value=process));monkeypatch.setattr('threading.Thread.start',lambda _:None)
    m.start({'confirm_motion':True})
    sent=json.loads(process.stdin.getvalue())
    assert sent['position_scale']==1 and sent['expected_settings']['revision']==2
    assert m.settings.motion.position_scale==.5


def test_arm_service_rejects_requested_base_scope(tmp_path,monkeypatch):
    m=manager(tmp_path)
    m.value['service']={'service':{'session_contract':{'protocol':'kuavo_robot_session_v3','capabilities':[]}}}
    launch=Mock();monkeypatch.setattr(subprocess,'Popen',launch)
    assert not m.snapshot()['base_supported']
    with pytest.raises(ValueError,match='不支持双臂＋底盘'):m.start({'confirm_motion':True,'arm_mode':'dual_base'})
    launch.assert_not_called()


def test_read_settings_after_failed_check_never_restarts_preparation(tmp_path,monkeypatch):
    m=manager(tmp_path);m.preparation_options={'prepare_controller':True,'prepare_grippers':True,'prepare_session':True}
    monkeypatch.setattr(m,'_launch_options',lambda options:(['fake-python'],{},options))
    form={'revision':2,'values':{},'schema':{},'editable':False,'locked_reason':'session_active_or_cleanup_pending'}
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a,0,'KUAVO:'+json.dumps({'session_settings':form})+'\n',''))
    cancel=Mock(side_effect=AssertionError('read cancelled control'));monkeypatch.setattr(m,'_request_preparation_cancel',cancel)
    inspect=Mock(side_effect=AssertionError('read restarted control'));monkeypatch.setattr(m,'inspect',inspect)
    result=m.session_settings(SessionSettingsRequest(action='read'))
    assert result['session_settings']['values']=={} and not result['session_settings']['editable']
    cancel.assert_not_called();inspect.assert_not_called()
