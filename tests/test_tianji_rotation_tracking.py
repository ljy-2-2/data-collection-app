"""Reachable hand turns: elbow preference, small joint travel and angular lag.

Only the installed manufacturer FK/Jacobian libraries are used; no hardware
connection, servo enable or robot command is created by these tests.
"""
from pathlib import Path
import json

import numpy as np
import pytest

from teleop_control import tianji_worker  # Registers the bundled model path.
from marvin_teleop.configuration import configuration
from marvin_teleop.geometry import angle, inverse, rotation
from marvin_teleop.model import Models

ROOT = Path(__file__).resolve().parents[1]
QUALITY = dict(singular_rotation_weight_m=.05, joint_limit_soft_margin_deg=30.,
               joint_limit_reserve_deg=3., joint_limit_escape_speed_deg_s=20.)


@pytest.fixture(scope='module')
def models():
    return Models(configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'))


def checked_step(arm, target, q, velocity):
    result = arm.pose_tracking_ik(target, q, .01, 180., velocity=velocity,
                                 max_accel=450., **QUALITY)
    following = (result-q)/.01
    assert arm.valid_joints(result)
    assert np.max(abs(following)) <= 180.+1e-7
    assert np.max(abs(following-velocity)) <= 4.5+1e-7
    return result, following


@pytest.mark.parametrize('side', ['left', 'right'])
def test_inward_translation_prefers_negative_elbow_without_sign_limit(models, side):
    arm = models.arms[side]
    arm.reset_pose_tracking()
    q = np.zeros(7)
    velocity = q.copy()
    anchor = arm.fk(q)
    for tick in range(350):
        target = anchor.copy()
        target[1, 3] += (-1 if side == 'left' else 1)*.05*min(1., (tick+1)/100)
        q, velocity = checked_step(arm, target, q, velocity)
    # The old solver ended at +40.7 degrees on this same reachable path.
    assert q[3] < -35.
    assert np.linalg.norm(arm.fk(q)[:3, 3]-target[:3, 3]) < .0001
    assert angle(arm.fk(q)[:3, :3] @ target[:3, :3].T) < .001
    assert arm.hardware_lower[3] == -145. and arm.hardware_upper[3] == 60.


@pytest.mark.parametrize('side', ['left', 'right'])
def test_positive_elbow_remains_reachable_and_does_not_jump_branch(models, side):
    arm = models.arms[side]
    arm.reset_pose_tracking()
    initial = np.array([-5., -18., 4., 45., 3., -25., 2.])
    q, velocity = initial.copy(), np.zeros(7)
    witness = initial.copy()
    witness[3] += 2.
    target = arm.fk(witness)
    for _ in range(250):
        q, velocity = checked_step(arm, target, q, velocity)
        assert np.max(abs(q-initial)) < 5.
    assert q[3] > 45.
    assert np.linalg.norm(arm.fk(q)[:3, 3]-target[:3, 3]) < .0001
    assert angle(arm.fk(q)[:3, :3] @ target[:3, :3].T) < .001


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('direction', [-1., 1.])
def test_small_turn_does_not_pull_the_arm_toward_its_joint_centers(models, side, direction):
    arm = models.arms[side]
    arm.reset_pose_tracking()
    q, velocity = np.zeros(7), np.zeros(7)
    anchor = arm.fk(q)
    trace = [q.copy()]
    for tick in range(250):
        target = anchor.copy()
        target[:3, :3] = anchor[:3, :3] @ rotation([1, 0, 0], direction*.08*min(1., (tick+1)/100))
        q, velocity = checked_step(arm, target, q, velocity)
        trace.append(q.copy())
    # About 4.6 degrees of hand rotation used to create 32 degrees of joint
    # travel and a 13.8-degree unrelated reconfiguration, even with fixed TCP.
    trace = np.asarray(trace)
    assert np.sum(abs(np.diff(trace, axis=0))) < 6.
    assert np.max(abs(trace)) < 2.
    assert np.linalg.norm(arm.fk(q)[:3, 3]-target[:3, 3]) < .0001
    assert angle(arm.fk(q)[:3, :3] @ target[:3, :3].T) < .001


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('posture', ['horizontal', 'vertical'])
def test_continuous_clockwise_and_counterclockwise_roll_tracks_hand_speed(models, side, posture):
    right = models.arms['right']
    mirror = models.arms['left'].tracking_mirror
    initial = np.array([0. if posture == 'horizontal' else 90., -45., 0., -90., 0., 45., 0.])
    anchor = right.fk(initial)
    # The gripper's forward (local X) axis is horizontal/vertical respectively.
    assert abs(anchor[2, 0]) == pytest.approx(0. if posture == 'horizontal' else 1., abs=1e-8)
    arm = models.arms[side]
    arm.reset_pose_tracking()
    q = initial.copy() if side == 'right' else mirror.signs*initial
    velocity = np.zeros(7)
    errors, positions = [], []
    for tick in range(400):
        target = anchor.copy()
        # Three complete alternating turns, peak hand speed 54 degrees/s.
        amount = .15*np.sin(2*np.pi*tick*.01) if tick < 300 else 0.
        target[:3, :3] = anchor[:3, :3] @ rotation([1, 0, 0], amount)
        if side == 'left':
            target = inverse(mirror.world) @ target @ mirror.tcp
        q, velocity = checked_step(arm, target, q, velocity)
        actual = arm.fk(q)
        errors.append(np.degrees(angle(actual[:3, :3] @ target[:3, :3].T)))
        positions.append(np.linalg.norm(actual[:3, 3]-target[:3, 3]))
    # Previously ~1.09 degrees mean / 2.04 degrees p95 angular lag. These
    # thresholds also catch silently restoring the stationary-goal brake.
    assert np.mean(errors[20:300]) < .08
    assert np.percentile(errors[20:300], 95) < .15
    assert max(positions) < .0005
    assert errors[-1] < .06 and positions[-1] < .0001


@pytest.mark.parametrize('side', ['left', 'right'])
def test_reachable_nearby_negative_elbow_is_preferred_during_a_wrist_turn(models, side):
    right = models.arms['right']
    mirror = models.arms['left'].tracking_mirror
    fixture = json.loads((ROOT/'tests/fixtures/tianji_held_target_oscillation.json').read_text())[1]
    initial = np.asarray(fixture['q'])
    anchor = right.fk(initial)
    final = anchor.copy()
    final[:3, :3] = anchor[:3, :3] @ rotation([0, 0, 1], .08)
    witness = right.local_negative_ik(final, initial)
    assert witness is not None and witness[3] < -10.
    # This verified pose has both branches. v8 selected J4 ~= +2.24 degrees.
    assert np.linalg.norm(right.fk(witness)[:3, 3]-final[:3, 3]) < .0001
    arm = models.arms[side]
    arm.reset_pose_tracking()
    q = initial.copy() if side == 'right' else mirror.signs*initial
    velocity = np.zeros(7)
    available = False
    for tick in range(350):
        target = anchor.copy()
        target[:3, :3] = anchor[:3, :3] @ rotation([0, 0, 1], .08*min(1., (tick+1)/100))
        if side == 'left':
            target = inverse(mirror.world) @ target @ mirror.tcp
        q, velocity = checked_step(arm, target, q, velocity)
        available |= arm.last_negative_candidate_available
        assert q[3] < 0.
    assert available and q[3] < -10.
    actual = arm.fk(q)
    assert np.linalg.norm(actual[:3, 3]-target[:3, 3]) < .0001
    assert angle(actual[:3, :3] @ target[:3, :3].T) < .001


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('posture', ['horizontal', 'vertical'])
@pytest.mark.parametrize('axis', range(3))
def test_wrist_rotation_velocity_survives_submillimetre_position_jitter(models, side, posture, axis):
    right = models.arms['right']
    mirror = models.arms['left'].tracking_mirror
    initial = np.array([0. if posture == 'horizontal' else 90., -45., 0., -90., 0., 45., 0.])
    anchor = right.fk(initial)
    arm = models.arms[side]
    arm.reset_pose_tracking()
    q = initial.copy() if side == 'right' else mirror.signs*initial
    velocity = np.zeros(7)
    errors, positions = [], []
    for tick in range(400):
        target = anchor.copy()
        if tick < 300:
            target[:3, :3] = anchor[:3, :3] @ rotation(np.eye(3)[axis], .15*np.sin(2*np.pi*tick*.01))
            target[:3, 3] += .0003*np.sin(tick*.53+np.arange(3)*1.7)
        if side == 'left':
            target = inverse(mirror.world) @ target @ mirror.tcp
        q, velocity = checked_step(arm, target, q, velocity)
        errors.append(np.degrees(angle(arm.fk(q)[:3, :3] @ target[:3, :3].T)))
        positions.append(np.linalg.norm(arm.fk(q)[:3, 3]-target[:3, 3]))
    # v8 disabled hand-velocity feedforward at only 0.09 mm/frame positional
    # displacement and had ~1.06 degrees mean / 2.00 degrees p95 angular lag.
    assert np.mean(errors[20:300]) < (.15, 1., 2.)[axis]
    assert np.percentile(errors[20:300], 95) < (.4, 3., 4.5)[axis]
    assert max(positions) < (.0015, .003, .0055)[axis]
    assert errors[-1] < .06
    assert np.linalg.norm(arm.fk(q)[:3, 3]-target[:3, 3]) < .0001


def test_failed_optional_negative_candidate_does_not_block_reachable_pose(models, monkeypatch):
    arm = models.arms['right']
    arm.reset_pose_tracking()
    def unavailable(*args):
        raise ValueError('native branch enumeration unavailable at this configuration')
    monkeypatch.setattr(arm, 'local_negative_ik', unavailable)
    q = np.array([10., -35., 15., -10., 5., 15., -10.])
    velocity = np.zeros(7)
    target = arm.fk(q)
    target[:3, :3] = target[:3, :3] @ rotation([1, 0, 0], .03)
    for _ in range(250):
        q, velocity = checked_step(arm, target, q, velocity)
    assert np.linalg.norm(arm.fk(q)[:3, 3]-target[:3, 3]) < .0001
    assert angle(arm.fk(q)[:3, :3] @ target[:3, :3].T) < .001

