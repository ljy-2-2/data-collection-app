import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control.kuavo import KuavoManager

ROOT=Path(__file__).resolve().parents[1]

def test_manual_policy_and_owner_isolation_offline():
    result=subprocess.run([sys.executable,str(ROOT/'tests/fixtures/kuavo_manual_fake.py')],
        cwd=ROOT,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'manual offline checks:' in result.stdout


def manager(tmp_path):return KuavoManager(SimpleNamespace(hardware_enabled=False),root=tmp_path)

def test_old_service_cannot_start_or_fake_readiness(tmp_path,monkeypatch):
    m=manager(tmp_path);spawn=Mock();monkeypatch.setattr(subprocess,'Popen',spawn)
    assert len(m.snapshot()['manual']['joints'])==20
    m.value['service']={'service':{'backend':'robot'}}
    with pytest.raises(ValueError,match='更新'):m.manual.start()
    spawn.assert_not_called()


def test_manual_excludes_teleop_and_settings(tmp_path):
    m=manager(tmp_path)
    m.manual.process=SimpleNamespace(poll=lambda:None,stdin=io.StringIO())
    m.manual.session_id='owned';m.manual.state='ready'
    assert m.active() and not m.snapshot()['prestart_editable']
    with pytest.raises(RuntimeError):m.start({'confirm_motion':True})
    with pytest.raises(RuntimeError):m.inspect({})
    assert not m.snapshot()['control_ready']


@pytest.mark.parametrize('body',[
    dict(session_id='old',sequence=1,command={'action':'heartbeat'}),
    dict(session_id='owned',sequence=True,command={'action':'heartbeat'}),
    dict(session_id='owned',sequence=0,command={'action':'heartbeat'}),
    dict(session_id='owned',sequence=1,command={'action':'joint','joint':'waist_yaw_joint','delta_deg':11}),
    dict(session_id='owned',sequence=1,command={'action':'base','direction':'faster'}),
])
def test_invalid_or_stale_manual_request_never_reaches_worker(tmp_path,body):
    m=manager(tmp_path);sink=io.StringIO()
    m.manual.process=SimpleNamespace(poll=lambda:None,stdin=sink)
    m.manual.session_id='owned';m.manual.sequence=0;m.manual.state='ready'
    with pytest.raises(ValueError):m.manual.input(body)
    assert not sink.getvalue()


def test_button_sequence_and_stale_release_isolation(tmp_path):
    m=manager(tmp_path);sink=io.StringIO()
    m.manual.process=SimpleNamespace(poll=lambda:None,stdin=sink)
    m.manual.session_id='owned';m.manual.state='ready'
    m.manual.input(dict(session_id='owned',sequence=2,command=dict(action='base',direction='stop')))
    with pytest.raises(ValueError):m.manual.input(dict(session_id='owned',sequence=1,command=dict(action='base',direction='forward')))
    assert json.loads(sink.getvalue())==dict(action='base',direction='stop')


def ready_manual(tmp_path):
    import math, time
    m=manager(tmp_path);sink=io.StringIO();manual=m.manual
    manual.process=SimpleNamespace(poll=lambda:None,stdin=sink)
    manual.session_id='owned';manual.state='ready';manual.received=time.monotonic()
    manual.telemetry=dict(pose_protocol='kuavo_manual_pose_v1',
        joints={r['name']:math.radians((r['min_deg']+r['max_deg'])/2) for r in manual.rows},
        grippers={'left':{'closure_fraction':.3},'right':{'closure_fraction':.8}})
    manual.initial_pose=manual._measured_pose()
    return m,sink


def test_saved_pose_is_measured_persistent_and_restorable(tmp_path):
    m,sink=ready_manual(tmp_path)
    expected=m.manual._measured_pose()
    reply=m.manual.pose_operation(dict(action='save',name='工作位置',session_id='owned'))
    pid=reply['manual']['selected_pose']
    assert reply['manual']['poses']==[dict(id=pid,name='工作位置')]
    assert sink.getvalue()=='' # Saving never issues a motion request.
    assert manager(tmp_path).manual.poses[0]['pose']==expected
    m.manual.input(dict(session_id='owned',sequence=1,command=dict(action='restore',pose_id=pid)))
    assert json.loads(sink.getvalue())==dict(action='restore',pose=expected)
    m.manual.pose_operation(dict(action='delete',id=pid))
    assert manager(tmp_path).manual.poses==[]
    with pytest.raises(ValueError,match='不存在'):
        m.manual.input(dict(session_id='owned',sequence=2,command=dict(action='restore',pose_id=pid)))


@pytest.mark.parametrize('problem',['stale','busy','missing_joint','missing_gripper','invalid_gripper','nonfinite','out_of_range'])
def test_bad_pose_not_saved_or_restored(tmp_path,problem):
    m,sink=ready_manual(tmp_path);manual=m.manual
    if problem=='stale':manual.received=0
    elif problem=='busy':manual.busy=True
    elif problem=='missing_joint':manual.telemetry['joints'].pop(manual.rows[0]['name'])
    elif problem=='missing_gripper':manual.telemetry['grippers'].pop('left')
    elif problem=='invalid_gripper':manual.telemetry['grippers']['left']['closure_fraction']=1.5
    elif problem=='nonfinite':manual.telemetry['joints'][manual.rows[0]['name']]=float('nan')
    elif problem=='out_of_range':manual.telemetry['joints'][manual.rows[0]['name']]=100
    with pytest.raises(ValueError):manual.pose_operation(dict(action='save',name='bad',session_id='owned'))
    with pytest.raises(ValueError):manual.input(dict(session_id='owned',sequence=1,command=dict(action='restore',pose_id='initial')))
    assert sink.getvalue()=='' and not manual.poses_path.exists()


def test_initial_pose_old_service_and_session_isolation(tmp_path):
    m,sink=ready_manual(tmp_path)
    with pytest.raises(ValueError):m.manual.pose_operation(dict(action='save',name='old',session_id='old'))
    m.manual.telemetry.pop('pose_protocol')
    with pytest.raises(ValueError,match='更新'):m.manual.input(dict(session_id='owned',sequence=1,command=dict(action='restore',pose_id='initial')))
    assert sink.getvalue()==''
    m.manual.telemetry['pose_protocol']='kuavo_manual_pose_v1'
    m.manual.input(dict(session_id='owned',sequence=2,command=dict(action='restore',pose_id='initial')))
    assert json.loads(sink.getvalue())['pose']==m.manual.initial_pose
    assert manager(tmp_path).manual.initial_pose is None


def test_corrupt_pose_file_never_overwritten(tmp_path):
    m,sink=ready_manual(tmp_path);path=m.manual.poses_path
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text('broken')
    other=manager(tmp_path)
    assert not other.manual.snapshot()['poses_available']
    with pytest.raises(ValueError,match='读取失败'):other.manual.pose_operation(dict(action='delete',id='x'))
    assert path.read_text()=='broken'


def test_default_pose_comes_only_from_manufacturer_initialization(tmp_path):
    import copy,math
    m,sink=ready_manual(tmp_path)
    initial=copy.deepcopy(m.manual._measured_pose())
    name=m.manual.rows[0]['name'];initial['joints'][name]+=math.radians(15)
    m.manual._receive_initial_pose(dict(initial_pose=initial,initial_pose_source='robot_init_state_param'))
    m.manual.input(dict(session_id='owned',sequence=1,command=dict(action='restore',pose_id='initial')))
    assert json.loads(sink.getvalue())['pose']==initial
    assert initial['joints']!=m.manual.telemetry['joints']
    for value in [{},dict(initial_pose=initial,initial_pose_source='session_feedback'),
                  dict(initial_pose=None,initial_pose_source='robot_init_state_param')]:
        m.manual._receive_initial_pose(value)
        assert m.manual.initial_pose is None
        with pytest.raises(ValueError,match='不存在'):
            m.manual.input(dict(session_id='owned',sequence=2,command=dict(action='restore',pose_id='initial')))


def test_check_automatically_prepares_manual_only_after_device_success(tmp_path):
    from teleop_control.kuavo import Options
    m=manager(tmp_path);calls=[]
    options=Options(prepare_manual=True,prepare_session=True,prepare_controller=True,prepare_grippers=True,
        confirm_motion=True,motor_ids_confirmed=True).model_dump()
    def devices(value,**kw):
        assert value['prepare_manual'] and not value['prepare_session']
        calls.append('devices');return dict(preparation_generation=12)
    m._inspect_devices=devices
    def start(**kw):
        assert kw==dict(checked_options=options,generation=12);calls.append('manual')
    m.manual.start=start
    m.manual.wait_ready=lambda generation:calls.append(('ready',generation))
    m.inspect(options,connect_cameras=True)
    assert calls==['devices','manual',('ready',12)]
    m._inspect_devices=Mock(side_effect=RuntimeError('device failed'));m.manual.start=Mock()
    with pytest.raises(RuntimeError,match='device failed'):m.inspect(options)
    m.manual.start.assert_not_called()


def test_manual_check_permission_cannot_be_omitted():
    from teleop_control.kuavo import Options
    with pytest.raises(ValueError):Options(prepare_manual=True)


def test_manual_to_xr_handoff_waits_for_confirmed_cleanup(tmp_path):
    m=manager(tmp_path);calls=[]
    m.manual.active=lambda:True
    m.manual.checked_options=dict(prepare_manual=True,prepare_controller=True,prepare_grippers=True,confirm_motion=True,motor_ids_confirmed=True)
    m.snapshot=lambda:dict(control_ready=True)
    m.manual.finish=lambda:calls.append('cleanup')
    m.inspect=lambda options,**kw:calls.append(('prepare',options))
    m.start=lambda options:calls.append(('activate',options))
    KuavoManager.start(m,dict(confirm_motion=True,arm_mode='left',require_prepared=True))
    assert calls[0]=='cleanup' and calls[1][0]=='prepare' and calls[2][0]=='activate'
    assert not calls[1][1]['prepare_manual'] and calls[1][1]['prepare_session']
    assert calls[2][1]['arm_mode']=='left'
    calls.clear();m.manual.finish=Mock(side_effect=RuntimeError('cleanup not confirmed'))
    with pytest.raises(RuntimeError,match='not confirmed'):KuavoManager.start(m,dict(confirm_motion=True))
    assert not calls


def test_handoff_cancelled_during_cleanup_never_starts_xr(tmp_path):
    m=manager(tmp_path);m.manual.active=lambda:True
    m.manual.checked_options=dict(prepare_manual=True)
    m.snapshot=lambda:dict(control_ready=True)
    m.manual.finish=m.check_cancel.set;m.inspect=Mock()
    with pytest.raises(RuntimeError,match='取消'):KuavoManager.start(m,dict(confirm_motion=True))
    m.inspect.assert_not_called()


def test_checked_manual_is_editable_and_cancel_releases_it(tmp_path):
    m,sink=ready_manual(tmp_path)
    m.manual.checked_options=dict(prepare_manual=True)
    m.preparation_options=dict(prepare_manual=True)
    assert m._prestart_editable() and m.snapshot()['control_ready']
    resume=m._request_preparation_cancel(preserve=True)
    assert resume[0]['prepare_manual'] and m.check_cancel.is_set()
    assert sink.getvalue()=='close\n'
    m.manual.busy=True
    assert not m._prestart_editable() and not m.snapshot()['control_ready']


def test_wait_ready_checks_cancellation_and_measured_readiness(tmp_path):
    m,sink=ready_manual(tmp_path)
    m.manual.checked_options=dict(prepare_manual=True)
    m.manual.wait_ready(m.preparation_generation,timeout=.05)
    assert m.value['state']=='ready'
    m.check_cancel.set()
    with pytest.raises(ValueError,match='取消'):m.manual.wait_ready(m.preparation_generation)
    assert sink.getvalue()=='close\n'
