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


@pytest.mark.parametrize('values',[{'arm_mode':'both'},{'position_scale':0},{'position_scale':1.1},{'position_scale':float('nan')},{'position_scale':True},{'max_velocity_rad_s':1}])
def test_invalid_or_unsupported_options_rejected(values):
    with pytest.raises(ValueError):Options(**values)
