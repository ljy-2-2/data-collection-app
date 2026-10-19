"""Independent manual table 4-2 audit; local kinematics only, no robot connection."""
import json
from pathlib import Path
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from teleop_control.tianji_config import DEFAULT_CONFIG
from teleop_control.tianji_models import TianjiMove
from teleop_control.tianji_worker import Models, configuration, manual_plan


ROOT = Path(__file__).resolve().parents[1]
MANUAL_DH = np.array([
    [0., 0., 174.5, 0.],
    [0., 90., 0., 0.],
    [0., -90., 287., 0.],
    [18., 90., 0., 180.],
    [18., 90., 314., 180.],
    [0., 90., 0., 90.],
    [0., 90., 0., 90.],
    [0., 90., 95., 90.],
])


def manual_chain(joints):
    frame = np.eye(4)
    for (distance_x, alpha_deg, distance_z, offset_deg), joint_deg in zip(MANUAL_DH, [*joints, 0.]):
        alpha, theta = np.radians([alpha_deg, offset_deg+joint_deg])
        cos_alpha, sin_alpha = np.cos(alpha), np.sin(alpha)
        cos_theta, sin_theta = np.cos(theta), np.sin(theta)
        frame = frame @ np.array([
            [cos_theta, -sin_theta, 0., distance_x/1000],
            [sin_theta*cos_alpha, cos_theta*cos_alpha, -sin_alpha, -distance_z*sin_alpha/1000],
            [sin_theta*sin_alpha, cos_theta*sin_alpha, cos_alpha, distance_z*cos_alpha/1000],
            [0., 0., 0., 1.],
        ])
    return frame


@pytest.fixture(scope='module')
def config():
    return configuration(DEFAULT_CONFIG)


@pytest.fixture(scope='module')
def models(config):
    return Models(config)


def test_bundled_dh_matches_ccs_696_manual_table_4_2():
    path = ROOT/'teleop_control/vendor/tianji/CommonConfig/ccs_m6_40.MvKDCfg'
    rows = [[float(value) for value in line.rstrip(',').split(',')] for line in path.read_text().splitlines() if line]
    for start in (1, 14):
        actual = np.array([row[:4] for row in rows[start:start+8]])
        np.testing.assert_allclose(actual, MANUAL_DH[:, [1, 0, 2, 3]], atol=1e-12)


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('joint_index', range(7))
@pytest.mark.parametrize('increment', [-10., 10.])
@pytest.mark.parametrize('bent', [False, True])
def test_all_joint_signs_match_manual_sdk_urdf_and_display(models, side, joint_index, increment, bent):
    arm = models.arms[side]
    joints = np.array([10., -25., 15., -80., 20., 10., -15.]) if bent else np.zeros(7)
    joints[joint_index] += increment
    expected = manual_chain(joints)
    vendor = np.linalg.inv(arm.base) @ arm.flange_fk(joints)
    urdf = np.linalg.inv(arm.base) @ arm.chain(joints)[-1] @ arm.flange
    np.testing.assert_allclose(vendor, expected, atol=1e-9, rtol=0)
    np.testing.assert_allclose(urdf[:3, 3], expected[:3, 3], atol=1e-5)
    np.testing.assert_allclose(urdf[:3, :3], expected[:3, :3], atol=2e-5)
    display = json.loads((ROOT/'public/tianji-model.json').read_text())
    letter = 'L' if arm.model_side == 'left' else 'R'
    entry = next(joint for joint in display['joints'] if joint['name'] == f'Joint{joint_index+1}_{letter}')
    np.testing.assert_array_equal(entry['axis'], [0., 0., 1.])
    np.testing.assert_array_equal(arm.axes[joint_index], entry['axis'])
    epsilon = 1e-4
    next_joints = joints.copy()
    next_joints[joint_index] += np.degrees(epsilon)
    next_pose = manual_chain(next_joints)
    delta_rotation = next_pose[:3, :3] @ expected[:3, :3].T
    angular = np.array([delta_rotation[2, 1]-delta_rotation[1, 2],
                        delta_rotation[0, 2]-delta_rotation[2, 0],
                        delta_rotation[1, 0]-delta_rotation[0, 1]])/(2*epsilon)
    jacobian = arm.world_jacobian(joints, arm.flange_fk(joints))
    np.testing.assert_allclose(arm.base[:3, :3].T@jacobian[3:, joint_index], angular, atol=1e-7)


def test_j3_positive_rotates_about_positive_base_z_at_zero():
    zero = manual_chain(np.zeros(7))
    changed = manual_chain([0., 0., 10., 0., 0., 0., 0.])
    relative = changed[:3, :3] @ zero[:3, :3].T
    angle = np.radians(10.)
    expected = np.array([[np.cos(angle), -np.sin(angle), 0.],
                         [np.sin(angle), np.cos(angle), 0.], [0., 0., 1.]])
    np.testing.assert_allclose(relative, expected, atol=1e-12)


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('increment', [-10., 10.])
@pytest.mark.parametrize('joint_index', range(7))
def test_joint_jog_keeps_sdk_sign_and_other_joints(models, config, monkeypatch, side, increment, joint_index):
    planner = Mock(return_value='checked-plan')
    monkeypatch.setattr('marvin_teleop.point_to_point.pose_plan', planner)
    measured = {arm_side: np.array([10., -25., 15., -80., 20., 10., -15.]) for arm_side in models.arms}
    original = {arm_side: values.copy() for arm_side, values in measured.items()}
    request = TianjiMove(kind='jog', mode='dual', side=side, joint=joint_index+1, delta=increment)
    assert manual_plan(models, measured, request, config) == 'checked-plan'
    goals = planner.call_args.args[2]
    assert planner.call_args.args[3] == (side,)
    for arm_side in measured:
        expected = original[arm_side].copy()
        if arm_side == side:
            expected[joint_index] += increment
        np.testing.assert_array_equal(goals[arm_side], expected)
        np.testing.assert_array_equal(measured[arm_side], original[arm_side])


@pytest.mark.parametrize('joint_number', range(1, 8))
@pytest.mark.parametrize('side', ['left', 'right'])
def test_joint_frames_match_unmodified_manufacturer_urdf(joint_number, side, models):
    arm = models.arms[side]
    letter = 'L' if arm.model_side == 'left' else 'R'
    source = arm.path.parent/f'Marvin M6-S-{letter}-CCS-696-V4.0 urdf.urdf'
    source_joint = ET.parse(source).getroot().find(f"joint[@name='Joint{joint_number}_{letter}']")
    configured_joint = ET.parse(arm.path).getroot().find(f"joint[@name='Joint{joint_number}_{letter}']")
    for element, attribute in (('origin', 'xyz'), ('origin', 'rpy'), ('axis', 'xyz')):
        expected = np.fromstring(source_joint.find(element).get(attribute), sep=' ')
        actual = np.fromstring(configured_joint.find(element).get(attribute), sep=' ')
        np.testing.assert_array_equal(actual, expected)
