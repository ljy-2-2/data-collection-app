"""Offline model, freshness and options; hardware discovery is never used."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
import pytest
from teleop_control.kuavo import KuavoManager, Options
from teleop_control.kuavo_preview import KuavoModel, ARM_NAMES, ASSETS, measured_feedback
from teleop_control.kuavo_input import Observation, ObservedChannel


def packet(stamp=100, values=None):
    return dict(event='control_trace',joint_order=list(ARM_NAMES),feedback_rad=values or [0.]*14,
                command_rad=[2.]*14,feedback_timestamp_ns=stamp)


def test_model_provenance_and_fk_independent_arms():
    model=KuavoModel()
    assert hashlib.sha256((ASSETS/'full_robot.urdf').read_bytes()).hexdigest()==model.reference['urdf_sha256']=='fecef6f980806af7d2c4b1abe56d3fe1d0dbfbd69b3475303eb158bcc06c85dc'
    original=model.segments();moved=model.segments({ARM_NAMES[0]:.5})
    assert all(a==b for a,b in zip(original,moved) if a['group']!='left')
    assert any(a!=b for a,b in zip(original,moved) if a['group']=='left')
    assert all(np.isfinite(s['a']+s['b']).all() for s in moved)
    assert model.reference['grippers']['left']['open_rad']==-2.67
    assert model.reference['grippers']['right']['closed_rad']==-.75


def test_only_complete_finite_measured_feedback_is_used():
    assert measured_feedback(packet())['joints'][ARM_NAMES[0]]==0
    for changed in [{'feedback_rad':None},{'feedback_rad':[0]*13},{'feedback_rad':[float('nan')]*14},
                    {'event':'command'},{'joint_order':[{}]*14},{'feedback_timestamp_ns':True},
                    {'joint_order':[ARM_NAMES[0]]*14}]:
        assert measured_feedback({**packet(),**changed}) is None
    assert measured_feedback({**packet(),'event':'ik_trace'}) is not None


def test_observation_does_not_refresh_duplicate_samples(monkeypatch):
    clock=[10.];monkeypatch.setattr('teleop_control.kuavo_input.time.monotonic',lambda:clock[0])
    observation=Observation();observation.packet(packet())
    frame=SimpleNamespace(stale=False,device_timestamp_ns=10)
    observation.input(frame)
    clock[0]+=2;observation.packet(packet());observation.input(frame)
    assert observation.snapshot()['telemetry']['age_s']==2
    assert observation.snapshot()['pico_live'] is False
    observation.packet(packet(101));observation.input(SimpleNamespace(stale=False,device_timestamp_ns=11))
    assert observation.snapshot()['pico_live'] is True
    channel=Mock();channel.receive.return_value=packet(102)
    observed=ObservedChannel(channel,observation)
    assert observed.receive() is channel.receive.return_value
    assert channel.mock_calls==[__import__('unittest.mock',fromlist=['call']).call.receive()]


def test_manager_stale_and_stopped_feedback_are_not_live(monkeypatch):
    clock=[10.];monkeypatch.setattr('teleop_control.kuavo.time.monotonic',lambda:clock[0])
    manager=KuavoManager(SimpleNamespace(hardware_enabled=False))
    manager.process=SimpleNamespace(poll=lambda:None)
    manager._merge({'telemetry':{**measured_feedback(packet()),'age_s':.1},'pico_live':True})
    assert manager.snapshot()['pose_live']
    clock[0]+=1
    assert not manager.snapshot()['pose_live'] and not manager.snapshot()['pico_live']
    manager.process=None
    manager._merge({'telemetry':{**measured_feedback(packet(101)),'age_s':0.}})
    assert not manager.snapshot()['pose_live']


def test_preparation_notice_waits_for_validated_running_and_preserves_refusal(monkeypatch, tmp_path):
    clock=[10.]
    monkeypatch.setattr('teleop_control.kuavo.time.monotonic',lambda:clock[0])
    manager=KuavoManager(SimpleNamespace(hardware_enabled=False),root=tmp_path)
    manager.session_started_at=clock[0]
    observation=Observation()
    observation.packet({'event':'compute_preparing'})
    clock[0]+=12
    manager._merge({'session':{'phase':'starting'},**observation.snapshot()})
    notice=manager.snapshot()['execution_notice']
    assert '准备计算模块' in notice and '12 秒' in notice and '手柄暂不生效' in notice
    observation.packet({'event':'ready'})
    manager._merge({'session':{'phase':'starting'},**observation.snapshot()})
    assert manager.snapshot()['state']=='starting'
    assert not manager.session_reached_running
    observation.packet({'event':'refused','reason':'ValueError: input connection closed or operator exit'})
    manager._merge({'session':{'phase':'failed','report':{'errors':['gripper shutdown unconfirmed']}},**observation.snapshot()})
    result=manager.snapshot()
    assert '尚未就绪' in result['error'] and 'gripper shutdown unconfirmed' in result['error']
    assert '未进入遥操作运行阶段' in result['execution_notice']
    assert result['robot_refusal']=='ValueError: input connection closed or operator exit'


def test_running_notice_requires_fresh_input_and_keeps_later_failure_reason(tmp_path):
    manager=KuavoManager(SimpleNamespace(hardware_enabled=False),root=tmp_path)
    manager.process=SimpleNamespace(poll=lambda:None)
    manager._merge({'session':{'phase':'running'},'pico_live':True})
    assert manager.session_reached_running and 'Grip' in manager.snapshot()['execution_notice']
    manager._merge({'session':{'phase':'running'},'pico_live':False})
    assert '未收到新鲜手柄输入' in manager.snapshot()['execution_notice']
    manager._merge({'session':{'phase':'failed','error':'cleanup unconfirmed'},
                    'robot_refusal':'input connection closed or operator exit'})
    assert manager.snapshot()['error']=='input connection closed or operator exit；cleanup unconfirmed'
    assert not manager.snapshot()['execution_notice']


@pytest.mark.parametrize('values',[{'arm_mode':'both'},{'position_scale':0},{'position_scale':1.1},{'position_scale':float('nan')},{'position_scale':True},{'max_velocity_rad_s':1}])
def test_invalid_or_unsupported_options_rejected(values):
    with pytest.raises(ValueError):Options(**values)


def test_idle_monitor_tracks_measured_pose_and_expires_without_new_frames(monkeypatch):
    clock=[10.];monkeypatch.setattr('teleop_control.kuavo.time.monotonic',lambda:clock[0])
    manager=KuavoManager(SimpleNamespace(hardware_enabled=False))
    manager.monitor_process=SimpleNamespace(poll=lambda:None)
    manager.monitor_received=10.
    manager.monitor_value=dict(pose_status='fresh',pose_detail='online',telemetry={
        'joints':{ARM_NAMES[0]:.5},'timestamp_ns':100,'age_s':.01})
    result=manager.snapshot()
    assert result['pose_live'] and not result['active']
    assert result['telemetry']['joints'][ARM_NAMES[0]]==.5
    assert result['segments']!=manager.model.segments()
    clock[0]+=.6
    assert not manager.snapshot()['pose_live'] and manager.snapshot()['pose_available']
    manager.monitor_received=clock[0];manager.monitor_value['pose_status']='stale'
    assert not manager.snapshot()['pose_live'], 'heartbeat is not joint feedback'
    manager.monitor_value['pose_status']='fresh';manager.monitor_process=None
    assert not manager.snapshot()['pose_live'], 'dead reader must not claim live pose'


def test_main_parameter_projection_preserves_other_side_and_reports_mixed_values():
    from pathlib import Path
    from teleop_control.kuavo_motion_controls import motion_controls
    folder=Path(__file__).parent/'fixtures/kuavo_settings'
    form={'values':json.loads((folder/'snapshot.json').read_text())['values'],
          'schema':json.loads((folder/'schema.json').read_text())}
    form['values']['right_arm_scale']=.4
    controls=motion_controls(form)
    assert controls['dual']==controls['dual_base']
    assert controls['dual']['mixed']==['position_scale']
    assert controls['right']['values']['position_scale']==.4
    assert controls['left']['targets']['max_velocity_rad_s']==['left_arm_speed_rad_s']
    assert controls['dual']['targets']['position_scale']==['left_arm_scale','right_arm_scale']
    assert controls['dual']['schema']['position_scale']['max']==2.


def gripper_packet(stamp=100, **changes):
    motor=dict(feedback=dict(received_ns=stamp,position_rad=-2.29),open_rad=-2.67,
               closed_rad=-1.15,feedback_age_ms=10.,desired_closure_fraction=1.,
               target_closure_fraction=1.,measured_closure_fraction=.25)
    motor.update(changes)
    return dict(event='status',grippers=dict(mode='injected_driver',motors={'left':motor}))


def test_gripper_preview_uses_canonical_measurement_not_target():
    from teleop_control.kuavo_preview import measured_grippers
    sample=measured_grippers(gripper_packet())['left']
    assert sample['closure_fraction']==pytest.approx(.25)
    assert sample['age_s']==.01
    for changes in ({'feedback':None}, {'feedback':{'received_ns':100,'position_rad':float('nan')}},
                    {'feedback':{'received_ns':True,'position_rad':-2.29}},
                    {'open_rad':-1.15}, {'closed_rad':None}, {'feedback_age_ms':1000},
                    {'feedback_age_ms':-1}, {'feedback_age_ms':True}):
        assert measured_grippers(gripper_packet(**changes))=={}
    for value in (None, {}, {'event':'gripper_trace','kind':'feedback_batch'},
                  {**gripper_packet(),'event':'command'}):
        assert measured_grippers(value)=={}
    sample=gripper_packet();sample['grippers']['mode']='preview'
    assert measured_grippers(sample)=={}


def test_gripper_feedback_survives_body_monitor_and_expires_without_samples(monkeypatch):
    clock=[10.];monkeypatch.setattr('teleop_control.kuavo_input.time.monotonic',lambda:clock[0])
    observer=Observation();observer.packet(gripper_packet())
    manager=KuavoManager(SimpleNamespace(hardware_enabled=False))
    manager.process=SimpleNamespace(poll=lambda:None)
    manager.monitor_process=SimpleNamespace(poll=lambda:None)
    manager.monitor_received=10.
    manager.monitor_value=dict(pose_status='fresh',telemetry=dict(joints={ARM_NAMES[0]:.5},age_s=.01))
    manager._merge(observer.snapshot())
    shown=manager.snapshot()['telemetry']
    assert shown['joints'][ARM_NAMES[0]]==.5
    assert shown['grippers']['left']['closure_fraction']==pytest.approx(.25)
    assert shown['grippers']['left']['live']
    clock[0]+=2
    observer.packet(gripper_packet())  # Repeated status is not new motor feedback.
    manager._merge(observer.snapshot())
    shown=manager.snapshot()['telemetry']['grippers']['left']
    assert shown['age_s']==pytest.approx(2.01) and not shown['live']
    observer.packet(gripper_packet(101));manager._merge(observer.snapshot())
    assert manager.snapshot()['telemetry']['grippers']['left']['live']
    clock[0]+=2
    assert not manager.snapshot()['telemetry']['grippers']['left']['live']
    assert manager.value['telemetry']['grippers']['left']['age_s']==pytest.approx(.01)
    manager.process=None
    observer.packet(gripper_packet(102));manager._merge(observer.snapshot())
    assert not manager.snapshot()['telemetry']['grippers']['left']['live']
