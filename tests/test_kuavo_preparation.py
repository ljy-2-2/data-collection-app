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
from teleop_control.kuavo import KuavoManager, Options


def test_preparation_runs_with_relocated_app_and_no_reference_folder(tmp_path):
    root=Path(__file__).resolve().parents[1]
    app=tmp_path/'relocated-app';app.mkdir()
    for name in ('teleop_control/src','teleop_control/vendor/kuavo_runtime','tests/fixtures/kuavo_settings'):
        shutil.copytree(root/name,app/name,ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy(root/'tests/fixtures/kuavo_preparation_fake.py',app/'fake.py')
    result=subprocess.run([sys.executable,str(app/'fake.py')],cwd=app,
        env={**os.environ,'PYTHONPATH':str(app/'teleop_control/src'),'PYTHONDONTWRITEBYTECODE':'1'},
        capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.parametrize('body',[{'prepare_controller':True},
    {'prepare_controller':True,'confirm_motion':True},
    {'prepare_controller':True,'motor_ids_confirmed':True}])
def test_unapproved_check_cannot_prepare_controller(body,tmp_path,monkeypatch):
    with pytest.raises(ValueError):Options(**body)
    manager=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    launch=Mock();monkeypatch.setattr(subprocess,'Popen',launch)
    with pytest.raises(ValueError):manager.inspect(body)
    launch.assert_not_called()


def test_plain_check_keeps_observation_only_default():
    assert not Options().prepare_controller
    assert not Options().prepare_grippers


@pytest.mark.parametrize('body',[
    {'prepare_grippers':True},
    {'prepare_grippers':True,'prepare_controller':True,'confirm_motion':True,
     'motor_ids_confirmed':True,'accept_unverified_gripper_bus':False},
])
def test_gripper_preparation_requires_explicit_options(body,tmp_path,monkeypatch):
    with pytest.raises(ValueError):Options(**body)
    manager=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    launch=Mock();monkeypatch.setattr(subprocess,'Popen',launch)
    with pytest.raises(ValueError):manager.inspect(body)
    launch.assert_not_called()


def test_failed_gripper_preparation_blocks_start_even_when_controller_ready(tmp_path):
    manager=KuavoManager(SimpleNamespace(hardware_enabled=False),root=tmp_path)
    manager.value.update(gripper_preparation_required=True,gripper_preparation_ready=False)
    with pytest.raises(ValueError,match='夹爪准备尚未通过'):
        manager.start({'confirm_motion':True})
    manager.value['gripper_preparation_ready']=True
    assert not manager.snapshot()['start_blocked_reason']


def test_auto_check_streams_progress_while_holding_operation_lock(tmp_path,monkeypatch):
    manager=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    options=Options(prepare_controller=True,confirm_motion=True,motor_ids_confirmed=True).model_dump()
    monkeypatch.setattr(manager,'_launch_options',lambda body:(['fake-python'],{},body))
    monkeypatch.setattr(manager,'_start_monitor',Mock())
    events=[{'preparation_progress':'正在启动厂家主控'}, {'preparation_progress':'厂家主控与初始化已就绪'}]
    class Writer(io.StringIO):
        def close(self):
            if not self.closed:self.sent=self.getvalue()
            super().close()
    writer=Writer()
    process=SimpleNamespace(stdin=writer,stdout=io.StringIO(''.join('KUAVO:'+json.dumps(e)+'\n' for e in events)),
        stderr=io.StringIO('fake diagnostic\n'),wait=Mock(return_value=0),poll=lambda:0)
    monkeypatch.setattr(subprocess,'Popen',Mock(return_value=process))
    merge=manager._merge;observed=[]
    def merging(value):
        assert manager.operation.locked() and manager.active()
        observed.append(value);merge(value)
    monkeypatch.setattr(manager,'_merge',merging)
    result=manager.inspect(options)
    assert result['state']=='idle' and not result['active']
    assert result['preparation_progress']=='厂家主控与初始化已就绪'
    assert observed[:2]==events and json.loads(writer.sent)['prepare_controller']
    manager._start_monitor.assert_called_once()


def test_busy_inspect_does_not_release_another_operation(tmp_path):
    manager=KuavoManager(SimpleNamespace(hardware_enabled=True),root=tmp_path)
    manager.pending_operation=True
    with pytest.raises(RuntimeError):manager.inspect({})
    assert manager.pending_operation
