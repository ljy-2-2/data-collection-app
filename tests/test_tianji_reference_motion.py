"""Reference retarget outputs and accepted-command IK, with kinematics only."""
import json
from pathlib import Path
import numpy as np
import pytest
from teleop_control import tianji_worker
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models
from marvin_teleop.geometry import inverse, pose, relative_target, rotation
from marvin_teleop.mapping import same_facing_map

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = json.loads((ROOT / 'tests/fixtures/test_teleop_retarget_reference.json').read_text())
QUALITY = dict(rotation_weight_m=.35, singular_rotation_weight_m=.05,
               joint_limit_soft_margin_deg=30., joint_limit_reserve_deg=3.)


@pytest.fixture(scope='module')
def models():
    return Models(configuration(ROOT / 'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'))


@pytest.mark.parametrize('case', REFERENCE['cases'])
def test_coordinates_match_independent_test_teleop_outputs(case):
    basis = same_facing_map(pose(case['head']))
    target = relative_target(pose(case['hand']), pose(case['hand0']), np.array(case['tcp0']), basis, case['scale'])
    np.testing.assert_allclose(target, case['expected'], atol=1e-12)
    assert np.linalg.det(target[:3, :3]) == pytest.approx(1.)


@pytest.mark.parametrize('side', ['left', 'right'])
def test_new_target_does_not_chase_a_previous_unsent_optimizer_goal(models, side):
    arm = models.arms[side]
    arm.reset_pose_tracking()
    q = np.array([10., -35., 15., -65., 5., 15., -10.])
    original = arm.fk(q)
    future = original.copy()
    future[0, 3] += .15
    # A few rejected sends leave the accepted integrator at q. A controller
    # reversal to its original pose must discard the unsent future IK posture.
    for _ in range(8):
        arm.pose_tracking_ik(future, q, .01, 180., velocity=np.zeros(7), max_accel=450., **QUALITY)
    result = arm.pose_tracking_ik(original, q, .01, 180., velocity=np.zeros(7), max_accel=450., **QUALITY)
    np.testing.assert_allclose(result, q, atol=1e-12)


@pytest.mark.parametrize('turn, error_bound_mm', [(0., 47.), (.4, 49.)])
def test_fast_hand_reversal_tracks_locally_and_preserves_mirrored_continuity(models, turn, error_bound_mm):
    left, right = models.arms['left'], models.arms['right']
    mirror = left.tracking_mirror
    q = dict(right=np.array([10., -35., 15., -65., 5., 15., -10.]))
    q['left'] = mirror.signs * q['right']
    velocity = {side: np.zeros(7) for side in q}
    for arm in (left, right):
        arm.reset_pose_tracking()
    anchor = right.fk(q['right'])
    errors, trace = [], []
    for tick in range(700):
        t = np.clip((tick - 50) / 30., 0., 2.)
        amount = t if t <= 1 else 2 - t
        target = anchor.copy()
        target[:3, 3] += np.array([.05, .08, .12]) * amount
        target[:3, :3] = rotation([1, 0, 0], turn * amount) @ anchor[:3, :3]
        for side, arm in (('right', right), ('left', left)):
            mapped = target if side == 'right' else inverse(mirror.world) @ target @ mirror.tcp
            n = arm.pose_tracking_ik(mapped, q[side], .01, 180., velocity=velocity[side], max_accel=450., **QUALITY)
            nv = (n - q[side]) / .01
            assert arm.valid_joints(n)
            assert np.max(abs(nv)) <= 180. + 1e-7
            assert np.max(abs(nv - velocity[side])) <= 4.5 + 1e-7
            q[side], velocity[side] = n, nv
        np.testing.assert_allclose(q['left'], mirror.signs * q['right'], atol=1e-6)
        errors.append(np.linalg.norm(right.fk(q['right'])[:3, 3] - target[:3, 3]) * 1000)
        trace.append(q['right'].copy())
    # The former look-ahead seed caused ~53-55 mm mean lag and 217-231 degrees
    # total joint travel on this identical out-and-back hand trajectory.
    assert np.mean(errors[50:110]) < error_bound_mm
    assert np.sum(abs(np.diff(trace, axis=0))) < 190.
    assert np.max(np.ptp(np.array(trace)[500:], axis=0)) < .01
