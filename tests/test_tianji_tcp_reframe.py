"""TCP frame relabelling must not rotate hardware or reverse VR hand motion."""
from copy import copy
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'teleop_control/vendor/tianji/teleop'))
from marvin_teleop.configuration import configuration
from marvin_teleop.geometry import inverse, pose, relative_target, rotation, transform
from marvin_teleop.mapping import same_facing_map
from marvin_teleop.mirror import LeftFromRight
from marvin_teleop.model import Models

FLIP = np.diag([1., -1., -1., 1.])


@pytest.fixture(scope='module')
def frames():
    current = Models(configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'))
    previous = {side: copy(arm) for side, arm in current.arms.items()}
    for arm in previous.values():
        arm.tool = arm.tool @ FLIP
        arm._pose_tracking = None
    previous['left'].tracking_mirror = LeftFromRight(previous['left'], previous['right'])
    return current.arms, previous


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('heading', [0., .8])
@pytest.mark.parametrize('axis', range(3))
@pytest.mark.parametrize('direction', [-1., 1.])
@pytest.mark.parametrize('motion', ['translation', 'rotation'])
def test_same_pico_motion_produces_same_physical_joint_commands(frames, side, heading, axis, direction, motion):
    current, previous = (arms[side] for arms in frames)
    for arm in (current, previous):
        arm.reset_pose_tracking()
    q0 = np.array([10., -35., 15., -65., 5., 15., -10.])
    hand0 = transform([.2, 1., -.4], [.2, -.3, .1])
    basis = same_facing_map(transform(rpy=[0., heading, 0.]))
    anchors = [arm.fk(q0) for arm in (current, previous)]
    np.testing.assert_allclose(anchors[0], anchors[1] @ FLIP, atol=1e-12)
    joints = [q0.copy(), q0.copy()]
    velocities = [np.zeros(7), np.zeros(7)]
    for tick in range(30):
        hand = hand0.copy()
        amount = direction * (tick+1)/30
        if motion == 'translation':
            hand[axis, 3] += .008 * amount
        else:
            hand[:3, :3] = rotation(np.eye(3)[axis], .03 * amount) @ hand0[:3, :3]
        targets = [relative_target(hand, hand0, anchor, basis, 1.) for anchor in anchors]
        np.testing.assert_allclose(targets[0], targets[1] @ FLIP, atol=1e-12)
        np.testing.assert_allclose(targets[0] @ inverse(current.tool),
                                   targets[1] @ inverse(previous.tool), atol=1e-12)
        for index, arm in enumerate((current, previous)):
            following = arm.pose_tracking_ik(targets[index], joints[index], .01, 180.,
                                             velocity=velocities[index], max_accel=450.)
            velocities[index] = (following-joints[index])/.01
            joints[index] = following
        np.testing.assert_allclose(joints[0], joints[1], atol=1e-6, rtol=0)


@pytest.mark.parametrize('axis,expected', [(0, [0., -1., 0.]), (1, [0., 0., 1.]), (2, [-1., 0., 0.])])
@pytest.mark.parametrize('direction', [-1., 1.])
def test_native_pico_positive_axes_map_to_robot_world(axis, expected, direction):
    # Native wire order is position xyz, quaternion xyzw, without Unity Z flip.
    hand0 = pose([0., 0., 0., 0., 0., 0., 1.])
    values = [0., 0., 0., 0., 0., 0., 1.]
    values[axis] = direction * .01
    basis = same_facing_map(hand0)
    target = relative_target(pose(values), hand0, np.eye(4), basis, 1.)
    np.testing.assert_allclose(target[:3, 3], direction * .01 * np.array(expected), atol=1e-12)
    values[:3] = [0., 0., 0.]
    values[3+axis] = np.sin(direction*.02/2)
    values[6] = np.cos(direction*.02/2)
    target = relative_target(pose(values), hand0, np.eye(4), basis, 1.)
    np.testing.assert_allclose(target[:3, :3], rotation(expected, direction*.02), atol=1e-12)
