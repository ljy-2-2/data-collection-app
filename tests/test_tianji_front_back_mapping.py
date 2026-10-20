"""Direction regressions against unmodified vendor kinematics; no hardware access.

Passing these tests proves the software transform chain, not physical installation.
"""
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'teleop_control/vendor/tianji/teleop'))
from marvin_teleop.configuration import configuration
from marvin_teleop.geometry import relative_target, rotation, transform
from marvin_teleop.mapping import same_facing_map, installed_base_from_nominal
from marvin_teleop.model import Models
from marvin_teleop.preview import validate_preview
from marvin_teleop.installation import new_measurements

CONFIG = ROOT / 'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'
# Independent expected mount axes from the manufacturer's GRV/fixture definition:
# native X is forward; native Z points outwards on each horizontal straight arm.
BASE_ROTATIONS = {
    'left': np.array([[1., 0., 0.], [0., 0., 1.], [0., -1., 0.]]),
    'right': np.array([[1., 0., 0.], [0., 0., -1.], [0., 1., 0.]]),
}
VR_AXES = np.array([[0., 0., -1.], [-1., 0., 0.], [0., 1., 0.]])


@pytest.fixture(scope='module')
def models():
    return Models(configuration(CONFIG))


def vendor_pose(arm, q):
    local = np.asarray(arm.kine.fk(list(map(float, q)))).copy()
    local[:3, 3] /= 1000
    base = np.eye(4)
    base[:3, :3] = BASE_ROTATIONS[arm.side]
    base[:3, 3] = arm.nominal_base[:3, 3]
    return base @ local @ arm.tool


@pytest.mark.parametrize('side', ['left', 'right'])
def test_mount_gravity_and_horizontal_zero_agree_with_vendor(models, side):
    arm = models.arms[side]
    np.testing.assert_allclose(arm.base[:3, :3], BASE_ROTATIONS[side], atol=1e-12)
    np.testing.assert_allclose(arm.base[:3, :3] @ arm.gravity, [0, 0, -9.81], atol=1e-12)
    delta = arm.flange_fk(np.zeros(7))[:3, 3] - arm.base[:3, 3]
    np.testing.assert_allclose(delta, [0, .8705 if side == 'left' else -.8705, 0], atol=1e-10)
    for q in ([0]*7, [10, -35, 15, -65, 5, 15, -10]):
        np.testing.assert_allclose(arm.fk(q), vendor_pose(arm, q), atol=1e-12)


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('axis', range(3))
@pytest.mark.parametrize('direction', [-1, 1])
@pytest.mark.parametrize('yaw', [0., .8, -1.2])
def test_six_hand_directions_against_vendor_and_differential_ik(models, side, axis, direction, yaw):
    arm = models.arms[side]
    heading = rotation([0, 1, 0], yaw)
    head = np.eye(4)
    head[:3, :3] = heading @ rotation([1, 0, 0], .2) @ rotation([0, 0, 1], -.15)
    hand0 = transform([.4, 1.2, -.3], [.2, -.3, .1])
    hand = hand0.copy()
    hand[:3, 3] += heading @ VR_AXES[axis] * .003 * direction
    q = np.array([10., -35., 15., -65., 5., 15., -10.])
    anchor = vendor_pose(arm, q)
    basis = same_facing_map(head, models.robot_from_operator)
    target = relative_target(hand, hand0, anchor, basis, 1)
    delta = np.eye(3)[axis] * .003 * direction
    np.testing.assert_allclose(target[:3, 3] - anchor[:3, 3], delta, atol=1e-12)
    solved = arm.ik(target, q)
    np.testing.assert_allclose(vendor_pose(arm, solved), target, atol=1e-6)
    # Production uses bounded differential IK; check that path as well.
    for _ in range(12):
        q = arm.differential_ik(target, q, .01, 180)
    np.testing.assert_allclose(vendor_pose(arm, q), target, atol=2e-5)


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('axis', range(3))
@pytest.mark.parametrize('direction', [-1, 1])
def test_three_hand_rotation_axes_against_unmodified_vendor(models, side, axis, direction):
    arm = models.arms[side]
    head = transform(rpy=[0, .7, 0])
    heading = head[:3, :3]
    hand0 = transform([.4, 1.2, -.3], [.2, -.3, .1])
    hand = hand0.copy()
    hand[:3, :3] = rotation(heading @ VR_AXES[axis], .015 * direction) @ hand0[:3, :3]
    q = np.array([10., -35., 15., -65., 5., 15., -10.])
    anchor = vendor_pose(arm, q)
    target = relative_target(hand, hand0, anchor, same_facing_map(head), 1)
    expected = anchor.copy()
    expected[:3, :3] = rotation(np.eye(3)[axis], .015 * direction) @ anchor[:3, :3]
    np.testing.assert_allclose(target, expected, atol=1e-12)
    solved = arm.ik(target, q)
    np.testing.assert_allclose(vendor_pose(arm, solved), expected, atol=1e-6)
    for _ in range(12):
        q = arm.differential_ik(target, q, .01, 180)
    np.testing.assert_allclose(vendor_pose(arm, q), expected, atol=2e-5)


def display_transforms(model, angles):
    matrices = {'stand': np.eye(4)}
    pending = model['joints'][:]
    while pending:
        remaining = []
        for joint in pending:
            if joint['parent'] not in matrices:
                remaining.append(joint)
                continue
            value = angles.get(joint['name'], 0.)
            if joint['mimic']:
                m = joint['mimic']
                value = angles.get(m['joint'], 0.) * float(m['multiplier']) + float(m['offset'])
            t = matrices[joint['parent']] @ transform(joint['xyz'], joint['rpy'])
            if joint['type'] in ('revolute', 'continuous'):
                r = np.eye(4)
                r[:3, :3] = rotation(joint['axis'], value)
                t = t @ r
            matrices[joint['child']] = t
        assert len(remaining) < len(pending)
        pending = remaining
    return matrices


def test_preview_and_collision_share_one_world_transform(models):
    cad = json.loads((ROOT / 'public/tianji-model.json').read_text())
    world_display = np.asarray(cad['world_from_display'])
    assert validate_preview(models, ROOT / 'public/tianji-model.json')['consistent']
    for q in ([0]*7, [10, -35, 15, -65, 5, 15, -10]):
        for opening in (0., .5, 1.):
            angles = {f'Joint{i + 1}_{letter}': np.radians(value)
                      for letter in ('L', 'R') for i, value in enumerate(q)}
            angles.update({f'gripper_drive_{letter}': opening for letter in ('L', 'R')})
            matrices = display_transforms(cad, angles)
            for side, letter in [('left', 'L'), ('right', 'R')]:
                arm = models.arms[side]
                np.testing.assert_allclose(world_display @ matrices[f'Base_{letter}'], arm.base, atol=1e-10)
                np.testing.assert_allclose(world_display @ matrices[f'grasp_center_{letter}'], vendor_pose(arm, q), atol=3e-5)
                for i, t in enumerate(arm.chain(q)):
                    np.testing.assert_allclose(world_display @ matrices[f'Link{i + 1}_{letter}'], t, atol=1e-10)
    assert models.collision_reason({s: np.zeros(7) for s in models.arms}) is None


@pytest.mark.parametrize('fault', ['mount', 'axis', 'tcp', 'geometry'])
def test_stale_preview_is_rejected(models, tmp_path, fault):
    cad = json.loads((ROOT / 'public/tianji-model.json').read_text())
    if fault == 'mount':
        next(j for j in cad['joints'] if j['name'] == 'stand_to_L')['rpy'][2] += np.pi
    elif fault == 'axis':
        j = next(j for j in cad['joints'] if j['name'] == 'Joint1_R')
        j['axis'] = (-np.array(j['axis'])).tolist()
    elif fault == 'tcp':
        cad['coordinate_mapping']['control_points']['left']['flange_to_tcp'][0][3] += .01
    else:
        cad['arm_geometry'] = 'front_back_mirror_v1'
    p = tmp_path / 'preview.json'; p.write_text(json.dumps(cad))
    with pytest.raises(ValueError): validate_preview(models, p)


def test_retired_compensations_rejected_and_calibration_labels_actual_tcp(models):
    cfg = configuration(CONFIG)
    for key, value in [('arm_geometry', 'front_back_mirror_v1'), ('mount_orientation', 'same_facing_y_half_turn')]:
        with pytest.raises(ValueError, match=key): configuration({**cfg, key: value})
    with pytest.raises(ValueError, match='mount_orientation'):
        installed_base_from_nominal(np.eye(4), 'same_facing_y_half_turn')
    report = models.validate()
    assert all(entry['max_jacobian_error'] < .0001 for entry in report.values())
    measurements = new_measurements(models)
    assert '裸法兰' not in measurements['instructions']
    for side, arm in models.arms.items():
        assert measurements['arms'][side]['control_point'] == arm.control_point_name
        np.testing.assert_allclose(measurements['arms'][side]['flange_to_tcp'], arm.tool)
