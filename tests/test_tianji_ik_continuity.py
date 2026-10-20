"""Small controller input must not select a remote redundant configuration."""
import json
from pathlib import Path
import numpy as np
import pytest
from teleop_control import tianji_worker
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT/'tests/fixtures/tianji_ik_branch_jumps.json').read_text())


@pytest.fixture(scope='module')
def models():
    return Models(configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'))


def advance(arm, target, q, v):
    result = arm.pose_tracking_ik(target, q, .01, 180, velocity=v, max_accel=450,
        singular_rotation_weight_m=.05, joint_limit_soft_margin_deg=30,
        joint_limit_reserve_deg=3, joint_limit_escape_speed_deg_s=20)
    next_v = (result-q)/.01
    assert arm.valid_joints(result)
    assert np.max(np.abs(next_v)) <= 180+1e-8
    assert np.max(np.abs(next_v-v)) <= 4.5+1e-8
    return result, next_v


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('case', CASES)
def test_half_millimetre_input_keeps_nearby_configuration(models, side, case, monkeypatch):
    arm = models.arms[side]
    q = np.array(case['q']); initial = q.copy(); v = np.zeros(7)
    target = arm.fk(q)
    target[:3, 3] += np.array(case['target'])[:3, 3]-models.arms['left'].fk(q)[:3, 3]
    assert case['max_joint_change_deg'] > 30
    def forbidden(*args, **kwargs):
        raise AssertionError('Teleoperation must not call global pose IK')
    monkeypatch.setattr(arm, 'ik', forbidden)
    for _ in range(250):
        q, v = advance(arm, target, q, v)
        assert np.max(np.abs(q-initial)) < 1.0
    assert np.linalg.norm(arm.fk(q)[:3, 3]-target[:3, 3]) < .0001


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('offset', [-.2, -.01, 0, .01, .2])
def test_small_noise_near_extension_does_not_spin_redundant_joints(models, side, offset):
    arm = models.arms[side]
    q = np.zeros(7); q[3] = arm.elbow_singularity_deg+offset
    initial = q.copy(); pose = arm.fk(q); v = np.zeros(7)
    for tick in range(200):
        target = pose.copy()
        target[:3, 3] += .00002*np.sin(tick*.17+np.arange(3))
        q, v = advance(arm, target, q, v)
        assert np.max(np.abs(q-initial)) < .2


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('direction', [-1, 1])
@pytest.mark.parametrize('dt', [.005, .01, .02])
def test_moving_hand_is_not_capped_at_ten_centimetres_per_second(models, side, direction, dt):
    arm = models.arms[side]
    q = np.array([10., -35, 15, -65, 5, 15, -10]); v = np.zeros(7)
    start = arm.fk(q); positions = []
    for tick in range(round(1/dt)):
        target = start.copy()
        target[2, 3] += direction * .2 * min((tick+1)*dt, .5)
        next_q = arm.pose_tracking_ik(target, q, dt, 180, velocity=v, max_accel=450,
            singular_rotation_weight_m=.05, joint_limit_soft_margin_deg=30,
            joint_limit_reserve_deg=3, joint_limit_escape_speed_deg_s=20)
        next_v = (next_q-q)/dt
        assert arm.valid_joints(next_q)
        assert np.max(abs(next_v)) <= 180+1e-7
        assert np.max(abs(next_v-v)) <= 450*dt+1e-7
        q, v = next_q, next_v
        positions.append(arm.fk(q)[2, 3])
        if tick == round(.5/dt)-1:
            assert abs(positions[-1]-target[2, 3]) < .015
    speeds = np.diff(positions)/dt*direction
    assert np.mean(speeds[round(.2/dt):round(.5/dt)-1]) > .18
    assert np.linalg.norm(arm.fk(q)[:3, 3]-target[:3, 3]) < .0001
