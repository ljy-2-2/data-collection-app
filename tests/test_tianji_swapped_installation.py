"""Anatomical side routing regression: local kinematics and fake SDK calls only."""
import json
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'teleop_control/src'), str(ROOT/'scripts')]
from teleop_control.tianji_worker import Models, configuration, manual_plan
from teleop_control.tianji_config import DEFAULT_CONFIG
from teleop_control.tianji_models import TianjiMove, TianjiParameters
from marvin_teleop.geometry import inverse, relative_target, rotation, transform
from marvin_teleop.mapping import same_facing_map, arm_mount_orientations
from marvin_teleop.robot import Robot
from gripper.marvin_can import MarvinCAN
from sync_tianji_installation import build_installation


@pytest.fixture(scope='module')
def config():
    return configuration(DEFAULT_CONFIG)


@pytest.fixture(scope='module')
def models(config):
    return Models(config)


def test_physical_arm_model_tool_and_channel_follow_whole_arm(config, models):
    assert config['arm_channels'] == {'left': 'A', 'right': 'B'}
    assert config['arm_models'] == {'left': 'left', 'right': 'right'}
    assert config['arm_base_sides'] == {'left': 'left', 'right': 'right'}
    for side, physical in config['arm_models'].items():
        arm = models.arms[side]
        letter = 'L' if physical == 'left' else 'R'
        assert arm.side == side and arm.model_side == physical
        assert arm.path.name == f'marvin_{physical}_with_gripper.urdf'
        assert arm.control_point_name == 'grasp_center_'+letter
        assert arm.base[1, 3] * (1 if physical == 'left' else -1) > 0.
        assert np.linalg.det(arm.base[:3, :3]) == pytest.approx(1.)
    report = models.validate()
    assert all(result['max_position_error_m'] < .0001 for result in report.values())


def test_relabel_preserves_each_hardware_channel_geometry_and_teleop(config, models):
    from marvin_teleop.control import Controller
    from marvin_teleop.robot import SimRobot
    previous = deepcopy(config)
    previous.update(arm_channels={'left': 'B', 'right': 'A'},
                    arm_models={'left': 'right', 'right': 'left'},
                    arm_base_sides={'left': 'right', 'right': 'left'},
                    teleop_translation_z_sign={side: config['teleop_translation_z_sign'][peer]
                                              for side, peer in (('left', 'right'), ('right', 'left'))},
                    teleop_translation_xy_sign={side: config['teleop_translation_xy_sign'][peer]
                                               for side, peer in (('left', 'right'), ('right', 'left'))},
                    tool_tcp={side: config['tool_tcp'][peer] for side, peer in (('left', 'right'), ('right', 'left'))})
    old_models = Models(previous)
    np.testing.assert_array_equal(old_models.robot_from_operator, models.robot_from_operator)
    for side, peer in (('left', 'right'), ('right', 'left')):
        old_arm, new_arm = old_models.arms[peer], models.arms[side]
        assert previous['arm_channels'][peer] == config['arm_channels'][side]
        for attribute in ('base', 'nominal_base', 'tool', 'lower', 'upper', 'velocity_limits', 'acceleration_limits'):
            np.testing.assert_array_equal(getattr(old_arm, attribute), getattr(new_arm, attribute))
        for joints in (np.zeros(7), np.array([10., -25., 15., -80., 20., 10., -15.])):
            for method in ('fk', 'flange_fk'):
                np.testing.assert_allclose(getattr(old_arm, method)(joints), getattr(new_arm, method)(joints), atol=1e-12)
            np.testing.assert_allclose(old_arm.world_jacobian(joints, old_arm.fk(joints)),
                                       new_arm.world_jacobian(joints, new_arm.fk(joints)), atol=1e-12)
            results = []
            for arm_models, setup, selected in ((old_models, previous, peer), (models, config, side)):
                controller = Controller(arm_models, SimRobot(), setup)
                state = controller.arms[selected]
                state.anchor = (np.eye(4), arm_models.arms[selected].fk(joints))
                state.previous_hand = np.eye(4)
                state.basis = same_facing_map(transform(rpy=(0., .7, 0.)), arm_models.robot_from_operator)
                state.commanded = joints.copy()
                state.joint_anchor = joints.copy()
                hand = transform([.002, .003, -.004], [.01, -.02, .03])
                desired = controller._target(selected, SimpleNamespace(hands={selected: hand}, sequence=1),
                                             {name: joints.copy() for name in arm_models.arms}, .01)
                results.append((state.target_pose.copy(), desired))
            for old_value, new_value in zip(*results):
                np.testing.assert_allclose(old_value, new_value, atol=1e-12)


@pytest.mark.parametrize('mapping', [{'left': 'left', 'right': 'left'}, {'left': 'right'}, None])
def test_invalid_or_partial_base_mapping_is_rejected(config, mapping):
    with pytest.raises(ValueError, match='arm_base_sides'):
        configuration(dict(config, arm_base_sides=mapping))


@pytest.mark.parametrize('mapping', [{'left': 'left', 'right': 'left'}, {'left': 'right'}, None])
def test_invalid_or_partial_model_mapping_is_rejected(config, mapping):
    with pytest.raises(ValueError, match='arm_models'):
        configuration(dict(config, arm_models=mapping))


def test_mount_orientation_overrides_are_per_side_and_legacy_compatible(config, models):
    assert arm_mount_orientations({}) == {'left': 'cad', 'right': 'cad'}
    assert arm_mount_orientations({'mount_orientation': 'same_facing_y_half_turn'}) == {
        'left': 'same_facing_y_half_turn', 'right': 'same_facing_y_half_turn'}
    assert arm_mount_orientations(config) == {'left': 'cad', 'right': 'cad'}
    assert config['mount_orientation'] == 'cad'
    assert not config.get('mount_orientation_overrides')
    left = models.arms['left']
    right = models.arms['right']
    np.testing.assert_allclose(left.base[:3, 3], left.nominal_base[:3, 3])
    np.testing.assert_allclose(left.base[:3, :3], left.nominal_base[:3, :3])
    np.testing.assert_allclose(right.base[:3, 3], right.nominal_base[:3, 3])
    np.testing.assert_allclose(right.base[:3, :3], right.nominal_base[:3, :3])
    assert models.mapping_report['mount_orientation_by_side'] == arm_mount_orientations(config)


@pytest.mark.parametrize('overrides', [None, [], {'unknown': 'cad'}, {'right': 'mirror'}, {'left': None}])
def test_invalid_mount_overrides_rejected(config, overrides):
    with pytest.raises(ValueError, match='mount_orientation_overrides'):
        configuration(dict(config, mount_orientation_overrides=overrides))


@pytest.mark.parametrize('overrides,changed_sides', [
    ({}, ()),
    ({'right': 'cad'}, ('right',)),
    ({'left': 'cad', 'right': 'cad'}, ('left', 'right')),
])
def test_base_correction_reverses_only_selected_x_z_for_fk_and_jacobian(config, overrides, changed_sides):
    previous = Models(dict(config, mount_orientation='same_facing_y_half_turn', mount_orientation_overrides={}))
    models = Models(dict(config, mount_orientation='same_facing_y_half_turn', mount_orientation_overrides=overrides))
    half_turn = np.diag([-1., 1., -1.])
    np.testing.assert_array_equal(previous.robot_from_operator, models.robot_from_operator)
    for side in ('left', 'right'):
        old_arm, new_arm = previous.arms[side], models.arms[side]
        correction = half_turn if side in changed_sides else np.eye(3)
        np.testing.assert_allclose(old_arm.base[:3, 3], new_arm.base[:3, 3])
        for joints in (np.zeros(7), np.array([10., -25., 15., -80., 20., 10., -15.])):
            old_pose, new_pose = old_arm.fk(joints), new_arm.fk(joints)
            centre = old_arm.base[:3, 3]
            np.testing.assert_allclose(new_pose[:3, 3]-centre, correction@(old_pose[:3, 3]-centre), atol=1e-12)
            np.testing.assert_allclose(new_pose[:3, :3], correction@old_pose[:3, :3], atol=1e-12)
            old_jacobian = old_arm.world_jacobian(joints, old_pose)
            new_jacobian = new_arm.world_jacobian(joints, new_pose)
            np.testing.assert_allclose(new_jacobian[:3], correction@old_jacobian[:3], atol=1e-12)
            np.testing.assert_allclose(new_jacobian[3:], correction@old_jacobian[3:], atol=1e-12)
            assert np.linalg.det(new_pose[:3, :3]) == pytest.approx(1.)


def test_confirmed_shoulder_half_turn_keeps_stand_origins_and_joint_definitions(config, models):
    previous = Models(dict(config, mount_orientation='same_facing_y_half_turn', mount_orientation_overrides={}))
    np.testing.assert_array_equal(previous.world_mount, models.world_mount)
    np.testing.assert_array_equal(previous.robot_from_operator, models.robot_from_operator)
    for old_obstacle, new_obstacle in zip(previous.obstacles, models.obstacles, strict=True):
        np.testing.assert_array_equal(old_obstacle.triangles, new_obstacle.triangles)
    for side, arm in models.arms.items():
        old_arm = previous.arms[side]
        np.testing.assert_allclose(arm.base[:3, 3], old_arm.base[:3, 3], atol=1e-12)
        np.testing.assert_allclose(old_arm.base[:3, :3].T @ arm.base[:3, :3],
                                   np.diag([-1., -1., 1.]), atol=1e-12)
        for attribute in ('tool', 'lower', 'upper', 'velocity_limits', 'acceleration_limits', 'manufacturer_base'):
            np.testing.assert_array_equal(getattr(old_arm, attribute), getattr(arm, attribute))
        correction = arm.base @ inverse(old_arm.base)
        for joints in (np.zeros(7), np.array([10., -25., 15., -80., 20., 10., -15.])):
            for method in ('fk', 'flange_fk'):
                np.testing.assert_allclose(getattr(arm, method)(joints),
                                           correction @ getattr(old_arm, method)(joints), atol=1e-12)
        np.testing.assert_array_equal(arm.base_bound[0], arm.base)


@pytest.mark.parametrize('axis', [0, 1, 2])
@pytest.mark.parametrize('direction', [-1., 1.])
@pytest.mark.parametrize('side', ['left', 'right'])
def test_ik_moves_tcp_in_requested_world_direction(models, side, axis, direction):
    arm = models.arms[side]
    joints = np.array([10., -25., 15., -80., 20., 10., -15.])
    start = arm.fk(joints)
    target = start.copy()
    target[axis, 3] += direction*.001
    result = arm.differential_ik(target, joints, .01, 10.)
    actual = arm.fk(result)
    assert direction*(actual[axis, 3]-start[axis, 3]) > 0
    assert np.linalg.norm(actual[:3, 3]-target[:3, 3]) < .001
    assert arm.valid_joints(result)


@pytest.mark.parametrize('side', ['left', 'right'])
def test_same_facing_translation_and_rotation_are_not_reflected(models, side):
    anchor = models.arms[side].fk(np.array([0., -25., 0., -80., 0., 0., 0.]))
    hand_start = np.eye(4)
    for yaw in (0., .7, -1.2):
        head = transform(rpy=(0., yaw, 0.))
        basis = same_facing_map(head, models.robot_from_operator)
        for axis in np.eye(3):
            hand = hand_start.copy()
            hand[:3, 3] = head[:3, :3] @ (np.array([[0., -1., 0.], [0., 0., 1.], [-1., 0., 0.]]) @ axis)*.01
            target = relative_target(hand, hand_start, anchor, basis, 1.)
            np.testing.assert_allclose(target[:3, 3]-anchor[:3, 3], axis*.01, atol=1e-12)
            hand[:3, :3] = basis.T @ rotation(axis, .1) @ basis
            target = relative_target(hand, hand_start, anchor, basis, 1.)
            np.testing.assert_allclose(target[:3, :3], rotation(axis, .1) @ anchor[:3, :3], atol=1e-12)


def test_display_installation_is_generated_from_same_bases(models):
    current = json.loads((ROOT/'public/tianji-installation.json').read_text())
    assert current == build_installation(ROOT)
    original = json.loads((ROOT/'public/tianji-model.json').read_text())
    original_joints = {joint['name']: joint for joint in original['joints']}
    display_frames = []
    for side, arm in models.arms.items():
        letter = 'L' if arm.model_side == 'left' else 'R'
        mount = current['joints']['stand_to_'+letter]
        displayed_base = transform(mount['xyz'], mount['rpy'])
        slot = original_joints['stand_to_'+letter]
        original_base = transform(slot['xyz'], slot['rpy'])
        np.testing.assert_allclose(displayed_base[:3, 3], original_base[:3, 3], atol=1e-10)
        relative_rotation = original_base[:3, :3].T @ displayed_base[:3, :3]
        np.testing.assert_allclose(relative_rotation, np.diag([-1., -1., 1.]), atol=1e-10)
        assert np.linalg.det(relative_rotation) == pytest.approx(1.)
        display_frames.append(displayed_base @ inverse(arm.base))
    np.testing.assert_allclose(display_frames[0], display_frames[1], atol=1e-10)


def test_saved_old_pose_is_rejected_before_motion_planning(models, config):
    measured = {side: np.zeros(7) for side in models.arms}
    for identifier in (None, 'nominal', 'arms_swapped_cables_unchanged_20261019', 'anatomical_left_a_right_b_20261019', 'anatomical_left_a_right_b_right_mount_v2_20261019', 'anatomical_left_a_right_b_both_mount_v3_20261019', 'anatomical_left_a_right_b_right_mount_rollback_v4_20261019', 'anatomical_left_a_right_b_pre_swap_restore_v5_20261019', 'physical_left_b_right_a_relabel_v6_20261020', 'physical_left_a_right_b_verified_jog_v7_20261020', 'physical_left_a_right_b_shoulder_half_turn_v8_20261020'):
        request = TianjiMove(kind='pose', targets={side: [0.]*7 for side in models.arms}, installation_id=identifier)
        with pytest.raises(ValueError, match='旧安装'):
            manual_plan(models, measured, request, config)


def test_current_pose_reaches_existing_collision_checked_planner(models, config, monkeypatch):
    planner = Mock(return_value='checked-plan')
    monkeypatch.setattr('marvin_teleop.point_to_point.pose_plan', planner)
    measured = {side: np.zeros(7) for side in models.arms}
    request = TianjiMove(kind='pose', targets={side: [0.]*7 for side in models.arms}, installation_id=config['installation_id'])
    assert manual_plan(models, measured, request, config) == 'checked-plan'
    planner.assert_called_once()


def test_gripper_calibration_and_can_follow_original_hardware(config):
    parameters = TianjiParameters()
    assert (parameters.left_gripper_open_rad, parameters.left_gripper_closed_rad) == (-.62, .9)
    assert (parameters.right_gripper_open_rad, parameters.right_gripper_closed_rad) == (.18, 1.7)
    grippers = json.loads((ROOT/'teleop_control/vendor/tianji/teleop/gripper/config.json').read_text())
    robot = SimpleNamespace(config=config, api=Mock())
    for side, channel in config['arm_channels'].items():
        transport = MarvinCAN(robot, side, grippers['motors'][side], grippers)
        assert transport.channel == channel
        assert transport.set_data is getattr(robot.api.robot, 'OnSetChData'+channel)
        assert transport.get_data is getattr(robot.api.robot, 'OnGetChData'+channel)
    assert robot.api.mock_calls == [call.robot.OnClearChDataA(), call.robot.OnClearChDataB()]


@pytest.mark.parametrize('side,channel,index', [('left','A',0), ('right','B',1)])
def test_fake_sdk_read_enable_command_and_stop_use_anatomical_channels(config, monkeypatch, side, channel, index):
    from SDK_PYTHON import fx_robot
    api = Mock()
    for method in ('connect', 'clear_set', 'send_cmd', 'set_joint_cmd_pose', 'set_vel_acc', 'set_state'):
        getattr(api, method).return_value = True
    feedback = dict(serial=1, states=[0, 0])
    def subscribe(_):
        feedback['serial'] += 1
        return dict(states=[dict(cur_state=state, err_code=0) for state in feedback['states']],
                    outputs=[dict(frame_serial=feedback['serial'], fb_joint_pos=[value]*7,
                                  fb_joint_vel=[0.]*7) for value in (11., 22.)])
    api.subscribe.side_effect = subscribe
    monkeypatch.setattr(fx_robot, 'Marvin_Robot', lambda: api)
    monkeypatch.setattr(fx_robot, 'DCSS', lambda: None)
    robot = Robot(config)
    np.testing.assert_allclose(robot.read()['left'], [11.]*7)
    np.testing.assert_allclose(robot.read()['right'], [22.]*7)
    robot.enable([side])
    api.set_state.assert_called_once_with(channel, 1)
    feedback['states'][index] = 1
    robot.read()
    robot.command({side: np.full(7, 23.)})
    api.set_joint_cmd_pose.assert_called_with(channel, [23.]*7)
    feedback['states'] = [0, 0]
    robot.stop()
    assert call(channel, 0) in api.set_state.call_args_list
    assert call('B' if channel == 'A' else 'A', 1) not in api.set_state.call_args_list
    api.robot.OnSetIntPara.assert_not_called()


def test_recording_and_camera_roles_follow_anatomical_sides():
    from teleop_control.tianji_collection import CAMERA_MAPPING
    from qt_ui.bridge import TIANJI_CAMERAS
    assert CAMERA_MAPPING == {'usb-1': 'head_color', 'usb-2': 'right_color', 'usb-4': 'left_color'}
    assert {item['id']: item['label'] for item in TIANJI_CAMERAS} == {
        'usb_1': '主相机', 'usb_2': '右腕部相机', 'usb_4': '左腕部相机'}


def test_desktop_feedback_moves_correct_manufacturer_arm_and_gripper():
    from qt_ui.visuals import RobotScene
    scene = RobotScene(ROOT)
    status = dict(joints={'left': [11.]*7, 'right': [22.]*7}, grippers=dict(motors={
        side: dict(feedback=dict(position_rad=position, age_s=0))
        for side, position in (('left', -.62), ('right', 1.7))}))
    scene.update_pose(status)
    for joint in range(1, 8):
        assert scene._pose_angles[f'Joint{joint}_L'] == pytest.approx(np.radians(11))
        assert scene._pose_angles[f'Joint{joint}_R'] == pytest.approx(np.radians(22))
    assert scene._pose_angles['gripper_drive_L'] > 0
    assert scene._pose_angles['gripper_drive_R'] == pytest.approx(0)


def test_controller_joint_stream_reorders_every_per_arm_field_once():
    from FusionDataCollector.channel_mapping import anatomical_joint_sample
    sample = dict(joint_names=[side+'_joint_'+str(joint) for side in ('left', 'right') for joint in range(1, 8)],
                  position_deg=list(range(14)), velocity_deg_s=list(range(20, 34)), torque_nm=list(range(40, 54)),
                  frame_serial=[100, 200], arm_state=[1, 2, 3, 4, 5, 6], timestamp_ns=123456789)
    swapped = anatomical_joint_sample(sample, {'left': 'B', 'right': 'A'})
    for key in ('position_deg', 'velocity_deg_s', 'torque_nm'):
        assert swapped[key] == sample[key][7:]+sample[key][:7]
    assert swapped['frame_serial'] == [200, 100]
    assert swapped['arm_state'] == [4, 5, 6, 1, 2, 3]
    assert swapped['joint_names'] == sample['joint_names']
    assert swapped['timestamp_ns'] == sample['timestamp_ns']
    assert sample['position_deg'] == list(range(14))
    anatomical = anatomical_joint_sample(sample, {'left': 'A', 'right': 'B'})
    for key in sample:
        assert anatomical[key] == sample[key]
    with pytest.raises(ValueError, match='only once'):
        anatomical_joint_sample(swapped, {'left': 'B', 'right': 'A'})
    assert anatomical_joint_sample(sample, None) is sample


def test_desktop_old_pose_cannot_be_restored_or_silently_relabelled(config):
    from qt_ui.bridge import Bridge
    pose = dict(id='old-replay', name='replay', targets={'left': [1.]*7, 'right': [2.]*7})
    bridge = SimpleNamespace(_state=dict(robot='tianji', selectedPose=pose['id'], poses=[pose]),
                             can_move=lambda: True, fail=Mock(), action=Mock())
    Bridge.restorePose(bridge)
    bridge.fail.assert_called_once()
    bridge.action.assert_not_called()
    assert 'installation_id' not in pose
    pose['installation_id'] = config['installation_id']
    Bridge.restorePose(bridge)
    bridge.action.assert_called_once_with('move', '/tianji/move', dict(kind='pose', mode='dual',
        targets=pose['targets'], installation_id=config['installation_id']))


def test_collection_rejects_stale_worker_installation_before_opening_cameras(monkeypatch):
    from teleop_control import tianji_collection
    monkeypatch.setattr(tianji_collection, 'collection_available', lambda: True)
    factory = Mock()
    collection = tianji_collection.TianjiCollection(Mock(), episode_factory=factory)
    collection.armed = True
    collection.latest = dict(teleop_state='running')
    with pytest.raises(ValueError, match='安装版本'):
        collection.start()
    factory.assert_not_called()


@pytest.mark.parametrize('field', ['installation_id', 'arm_channels', 'arm_models', 'arm_base_sides', 'teleop_translation_z_sign', 'teleop_translation_xy_sign'])
def test_collection_rejects_mismatched_mapping_before_opening_cameras(config, monkeypatch, field):
    from teleop_control import tianji_collection
    monkeypatch.setattr(tianji_collection, 'collection_available', lambda: True)
    factory = Mock()
    collection = tianji_collection.TianjiCollection(Mock(), episode_factory=factory)
    collection.armed = True
    installation = deepcopy(config)
    installation[field] = None
    collection.latest = dict(teleop_state='running', installation=installation)
    with pytest.raises(ValueError, match='安装版本'):
        collection.start()
    factory.assert_not_called()


@pytest.mark.parametrize('action', ['start', 'move'])
@pytest.mark.parametrize('field', ['installation_id', 'teleop_translation_xy_sign'])
def test_manager_rejects_stale_worker_before_motion(config, action, field):
    import threading
    from teleop_control.tianji import TianjiManager
    manager = SimpleNamespace(lock=threading.RLock(), value={**config, field: None}, _write=Mock())
    with pytest.raises(ValueError, match='完全重启 App'):
        TianjiManager._command(manager, action, {})
    manager._write.assert_not_called()
