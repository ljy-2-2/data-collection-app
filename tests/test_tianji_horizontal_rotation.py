"""OpenXR horizontal turns with a 100 Hz controller and independent XR clock."""
from pathlib import Path

import numpy as np
import pytest

from teleop_control import tianji_worker
from marvin_teleop.configuration import configuration
from marvin_teleop.geometry import angle, relative_target, rotation, transform
from marvin_teleop.mapping import same_facing_map
from marvin_teleop.model import Models
from marvin_teleop.pose_tracking import _rotation_sample

ROOT = Path(__file__).resolve().parents[1]
QUALITY = dict(singular_rotation_weight_m=.05, joint_limit_soft_margin_deg=30.,
               joint_limit_reserve_deg=3.)


@pytest.fixture(scope='module')
def models():
    return Models(configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'))


def test_source_clock_controls_velocity_and_cached_reads_do_not_clear_it():
    first = np.eye(4)
    initial = _rotation_sample(None, first, .01, 100.)
    next_pose = first.copy()
    next_pose[:3, :3] = rotation([0, 0, 1], .02)
    sample = _rotation_sample({'rotation_sample': initial}, next_pose, .01, 100.02)
    # A 20 ms XR interval observed by a 10 ms servo loop is 1 rad/s, not 2.
    np.testing.assert_allclose(sample['velocity'], [0., 0., 1.], atol=1e-10)
    for loop_dt in (.005, .01, .02):
        repeated = _rotation_sample({'rotation_sample': sample}, next_pose, loop_dt, 100.02)
        np.testing.assert_allclose(repeated['velocity'], sample['velocity'], atol=0.)
    stopped = _rotation_sample({'rotation_sample': sample}, next_pose, .01, 100.04)
    np.testing.assert_allclose(stopped['velocity'], 0., atol=0.)


@pytest.mark.parametrize('timestamp', [99., 100., 100.3])
def test_initial_reset_or_long_gap_cannot_create_an_angular_velocity(timestamp):
    first = _rotation_sample(None, np.eye(4), .01, 100.)
    changed = transform(rpy=[0., 0., .1])
    sample = _rotation_sample({'rotation_sample': first}, changed, .01, timestamp)
    np.testing.assert_allclose(sample['velocity'], 0., atol=0.)


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('heading', [0., .7])
@pytest.mark.parametrize('fps', [30, 60, 90, 100])
def test_horizontal_clockwise_and_counterclockwise_turns_at_real_xr_cadence(models, side, heading, fps):
    mirror = models.arms['left'].tracking_mirror
    initial = np.array([10., -35., 15., -65., 5., 15., -10.])
    q = initial.copy() if side == 'right' else mirror.signs*initial
    arm = models.arms[side]
    arm.reset_pose_tracking()
    velocity = np.zeros(7)
    anchor = arm.fk(q)
    hand0 = transform([.3, 1.2, -.4], [.1, .2, -.1])
    basis = same_facing_map(transform(rpy=[0., heading, 0.]))
    errors, positions = [], []
    for tick in range(500):
        now = tick*.01
        source_time = np.floor(now*fps+1e-9)/fps
        theta = .15*np.sin(2*np.pi*source_time) if tick < 400 else 0.
        hand = hand0.copy()
        # OpenXR +Y is vertical: this is a top-view horizontal hand turn.
        hand[:3, :3] = rotation([0, 1, 0], theta) @ hand0[:3, :3]
        target = relative_target(hand, hand0, anchor, basis, 1.)
        np.testing.assert_allclose(target[:3, :3], rotation([0, 0, 1], theta) @ anchor[:3, :3], atol=1e-12)
        following = arm.pose_tracking_ik(
            target, q, .01, 180., velocity=velocity, max_accel=450.,
            input_time_s=100.+source_time, input_age_s=max(0., now-source_time), **QUALITY)
        new_velocity = (following-q)/.01
        assert arm.valid_joints(following)
        assert np.max(abs(new_velocity)) <= 180.+1e-7
        assert np.max(abs(new_velocity-velocity)) <= 4.5+1e-7
        q, velocity = following, new_velocity
        actual = arm.fk(q)
        true_angle = .15*np.sin(2*np.pi*now) if tick < 400 else 0.
        intended = rotation([0, 0, 1], true_angle) @ anchor[:3, :3]
        errors.append(np.degrees(angle(actual[:3, :3] @ intended.T)))
        positions.append(np.linalg.norm(actual[:3, 3]-anchor[:3, 3]))
    # At 60 Hz v9 had ~5.03 degrees mean / 8.67 degrees p95 angular lag
    # despite the same bent arm and 54 deg/s hand trajectory.
    assert np.mean(errors[50:400]) < (2.2 if fps == 30 else 1.4)
    assert np.percentile(errors[50:400], 95) < (4.8 if fps == 30 else 3.2)
    assert max(positions) < .002
    assert errors[-1] < .06 and positions[-1] < .0001
    assert np.max(abs(velocity)) < .01
    assert arm.last_input_rate_hz == pytest.approx(fps, rel=1e-7)
    assert arm.last_input_angular_speed_deg_s == 0.


def test_cached_rotation_feedforward_expires_without_extrapolating_the_pose(models):
    arm = models.arms['right']
    arm.reset_pose_tracking()
    q = np.array([10., -35., 15., -65., 5., 15., -10.])
    target = arm.fk(q)
    arm.pose_tracking_ik(target, q, .01, 180., velocity=np.zeros(7), max_accel=450.,
                         input_time_s=100., **QUALITY)
    target[:3, :3] = rotation([0, 0, 1], .01) @ target[:3, :3]
    first = arm.pose_tracking_ik(target, q, .01, 180., velocity=np.zeros(7), max_accel=450.,
                                input_time_s=100.02, **QUALITY)
    assert arm.last_angular_feedforward_active
    arm.pose_tracking_ik(target, first, .01, 180., velocity=(first-q)/.01, max_accel=450.,
                         input_time_s=100.02, input_age_s=.05, **QUALITY)
    assert not arm.last_angular_feedforward_active
    np.testing.assert_allclose(arm._pose_tracking['target'], target, atol=0.)
