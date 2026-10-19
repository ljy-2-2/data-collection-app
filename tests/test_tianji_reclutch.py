"""Per-arm heading capture with real kinematics and a simulated robot only."""
from pathlib import Path
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]/'teleop_control/vendor/tianji'
os.environ['MARVIN_SDK_ROOT'] = str(ROOT)
sys.path.insert(0, str(ROOT/'teleop'))

from marvin_teleop.configuration import configuration
from marvin_teleop.control import Controller
from marvin_teleop.geometry import rotation
from marvin_teleop.mapping import same_facing_map
from marvin_teleop.model import Models
from marvin_teleop.protocol import Frame
from marvin_teleop.robot import SimRobot


@pytest.fixture(scope='module')
def models():
    return Models(configuration(ROOT/'teleop/marvin_teleop/config.json'))


@pytest.fixture
def session(models, monkeypatch):
    config = configuration(ROOT/'teleop/marvin_teleop/config.json')
    robot = SimRobot()
    controller = Controller(models, robot, config)
    clock = [100.]
    monkeypatch.setattr('marvin_teleop.control.time.monotonic', lambda: clock[0])
    for arm in models.arms.values():
        monkeypatch.setattr(arm, 'differential_ik', lambda target, reference, *args, **kwargs: reference.copy())

    def tick(grips, yaw=0., hands=None, head=None, connected=True, elapsed=.01):
        clock[0] += elapsed
        if head is None:
            head = np.eye(4)
            head[:3, :3] = rotation([0, 1, 0], yaw)
        frame = Frame(
            received=clock[0], session=1, sequence=round(clock[0]*1000),
            source_time=clock[0], focused=True, tracked=True,
            hands=hands or {side: np.eye(4) for side in ('left', 'right')},
            head=head, grips=grips, stop=False,
        )
        controller.tick(frame, connected, False, clock[0])
        return frame

    tick({'left': 0., 'right': 0.})
    return SimpleNamespace(controller=controller, robot=robot, models=models, tick=tick)


def test_each_arm_captures_heading_when_it_engages(session):
    first = session.tick({'left': 1., 'right': 0.})
    left = session.controller.arms['left']
    original_basis = left.basis.copy()
    second = session.tick({'left': 1., 'right': 1.}, yaw=np.pi/2)
    right = session.controller.arms['right']
    np.testing.assert_allclose(left.basis, same_facing_map(first.head, session.models.robot_from_operator))
    np.testing.assert_allclose(right.basis, same_facing_map(second.head, session.models.robot_from_operator))
    assert not np.allclose(left.basis, right.basis)
    session.tick({'left': 1., 'right': 1.}, yaw=-np.pi/2)
    np.testing.assert_allclose(left.basis, original_basis)
    np.testing.assert_allclose(right.calibration_head, second.head)
    for side, arm in session.controller.arms.items():
        np.testing.assert_allclose(arm.target_pose, arm.anchor[1], atol=1e-12)


@pytest.mark.parametrize('side', ['left', 'right'])
def test_reclutch_refreshes_only_selected_arm_without_pose_jump(session, side):
    grips = {'left': 1., 'right': 1.}
    session.tick(grips)
    peer = 'right' if side == 'left' else 'left'
    peer_basis = session.controller.arms[peer].basis.copy()
    peer_anchor = session.controller.arms[peer].anchor[1].copy()
    session.tick({**grips, side: 0.}, yaw=np.pi/2)
    assert session.controller.arms[side].basis is None
    head_frame = session.tick(grips, yaw=np.pi/2)
    selected = session.controller.arms[side]
    expected_basis = same_facing_map(head_frame.head, session.models.robot_from_operator)
    np.testing.assert_allclose(selected.basis, expected_basis)
    session.tick(grips, yaw=-np.pi/3)
    np.testing.assert_allclose(selected.target_pose, session.models.arms[side].fk(session.robot.q[side]), atol=1e-12)
    hands = {arm_side: np.eye(4) for arm_side in grips}
    hands[side][:3, 3] = [.005, .002, -.003]
    hands[side][:3, :3] = rotation([1, 0, 0], .02)
    session.tick(grips, yaw=-np.pi/3, hands=hands)
    expected = selected.anchor[1].copy()
    expected[:3, 3] += expected_basis@hands[side][:3, 3]
    expected[:3, :3] = expected_basis@hands[side][:3, :3]@expected_basis.T@selected.anchor[1][:3, :3]
    np.testing.assert_allclose(selected.target_pose, expected, atol=1e-12)
    np.testing.assert_allclose(session.controller.arms[peer].basis, peer_basis)
    np.testing.assert_allclose(session.controller.arms[peer].target_pose, peer_anchor, atol=1e-12)
    assert session.controller.arms[peer].mode == 'ACTIVE'


def test_invalid_reclutch_heading_pauses_only_selected_arm(session):
    session.tick({'left': 1., 'right': 1.})
    peer_basis = session.controller.arms['right'].basis.copy()
    session.tick({'left': 0., 'right': 1.})
    head = np.eye(4)
    head[:3, :3] = rotation([1, 0, 0], np.pi/2)
    session.tick({'left': 1., 'right': 1.}, head=head)
    assert session.controller.arms['left'].mode == 'WAIT_RELEASE'
    assert session.controller.arms['left'].basis is None
    assert session.controller.arms['right'].mode == 'ACTIVE'
    np.testing.assert_allclose(session.controller.arms['right'].basis, peer_basis)
    assert not session.controller.fault


def test_short_input_gap_reanchors_with_fresh_heading(session):
    grips = {'left': 1., 'right': 1.}
    session.tick(grips)
    session.tick(grips, connected=False)
    assert all(arm.basis is None for arm in session.controller.arms.values())
    frame = session.tick(grips, yaw=np.pi/2)
    session.tick(grips, yaw=-np.pi/3)
    for arm in session.controller.arms.values():
        assert arm.mode == 'ACTIVE'
        np.testing.assert_allclose(arm.basis, same_facing_map(frame.head, session.models.robot_from_operator))
        np.testing.assert_allclose(arm.target_pose, arm.anchor[1], atol=1e-12)


def test_heading_capture_ignores_head_pitch_and_roll(session):
    head = np.eye(4)
    head[:3, :3] = rotation([0, 1, 0], .7)@rotation([1, 0, 0], .2)@rotation([0, 0, 1], -.3)
    session.tick({'left': 1., 'right': 0.}, head=head)
    level = np.eye(4)
    level[:3, :3] = rotation([0, 1, 0], .7)
    np.testing.assert_allclose(session.controller.arms['left'].basis,
                               same_facing_map(level, session.models.robot_from_operator), atol=1e-12)
