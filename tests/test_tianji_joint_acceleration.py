"""Manufacturer per-axis limits, tested with kinematics and SimRobot only."""
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from teleop_control import tianji_worker
from teleop_control.tianji_models import TianjiParameters
from marvin_teleop.configuration import configuration
from marvin_teleop.control import Controller
from marvin_teleop.geometry import angle, rotation
from marvin_teleop.model import Models
from marvin_teleop.motion import advance
from marvin_teleop.pose_tracking import TELEOP_ELBOW_MAX_DEG as J4_MAX
from marvin_teleop.robot import SimRobot

CONFIG = Path(__file__).resolve().parents[1] / 'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'
QUALITY = dict(singular_rotation_weight_m=.05, joint_limit_soft_margin_deg=30.,
               joint_limit_reserve_deg=3.)
# Read-only feedback captured while the user's application was idle. No live
# robot is instantiated or commanded by this regression.
STARTS = {
    'left': [-139.9494, 25.0673, 163.2499, -90.5763, 38.0796, -29.035, 15.0171],
    'right': [93.0229, 17.5576, -118.9635, -55.2295, -49.7284, -44.7444, -56.5021],
}


@pytest.fixture(scope='module')
def models():
    return Models(configuration(CONFIG))


@pytest.mark.parametrize('requested', [2., 450., 600., 900.])
def test_api_config_desktop_and_controller_agree_on_per_joint_limits(models, requested):
    from qt_ui.bridge import FIELDS

    params = TianjiParameters(max_joint_accel_deg_s2=requested)
    cfg = configuration(CONFIG)
    cfg['max_joint_accel_deg_s2'] = params.max_joint_accel_deg_s2
    cfg = configuration(cfg)
    ctrl = Controller(models, SimRobot(), cfg)
    field = next(f for f in FIELDS if f['key'] == 'max_joint_accel_deg_s2')
    assert field['max'] == 900
    for side, status in ctrl.status().items():
        np.testing.assert_equal(models.arms[side].acceleration_limits,
                                [450., 450., 900., 900., 900., 900., 900.])
        np.testing.assert_equal(status['joint_acceleration_limits_deg_s2'],
                                np.minimum(requested, models.arms[side].acceleration_limits))


@pytest.mark.parametrize('requested', [901., float('nan'), float('inf'), 0.])
def test_invalid_acceleration_is_rejected_at_both_boundaries(requested):
    with pytest.raises(ValidationError):
        TianjiParameters(max_joint_accel_deg_s2=requested)
    cfg = configuration(CONFIG)
    cfg['max_joint_accel_deg_s2'] = requested
    with pytest.raises(ValueError):
        configuration(cfg)


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('joint', range(7))
@pytest.mark.parametrize('requested', [450., 900.])
def test_each_motor_accelerates_and_stops_with_its_own_limit(models, side, joint, requested):
    arm = models.arms[side]
    q = np.array([10., -35., 15., -65., 5., 15., -10.])
    goal = q.copy()
    goal[joint] += 8.
    velocity = np.zeros(7)
    limits = np.minimum(requested, arm.acceleration_limits)
    peak = 0.
    for _ in range(160):
        following, _ = advance(arm, q, goal, velocity, .01, 180., requested)
        new_velocity = (following-q)/.01
        acceleration = abs(new_velocity-velocity)/.01
        peak = max(peak, acceleration[joint])
        assert np.all(acceleration <= limits+1e-6)
        assert np.max(abs(new_velocity)) <= 180.+1e-8
        stop = following+new_velocity*np.max(abs(new_velocity)/limits)/2
        assert arm.valid_joints(following) and arm.valid_joints(stop)
        q, velocity = following, new_velocity
    assert peak == pytest.approx(limits[joint], abs=1e-6)
    np.testing.assert_allclose(q, goal, atol=1e-6)
    np.testing.assert_allclose(velocity, 0., atol=1e-6)


@pytest.mark.parametrize('side', ['left', 'right'])
def test_local_xoz_wrist_rotation_from_user_feedback_has_bounded_compensation(models, side):
    arm = models.arms[side]
    arm.reset_pose_tracking()
    q, velocity = np.array(STARTS[side]), np.zeros(7)
    anchor = arm.fk(q)
    errors, positions, trace, speeds = [], [], [q.copy()], []
    limits = np.minimum(900., arm.acceleration_limits)
    for tick in range(500):
        target = anchor.copy()
        theta = .15*np.sin(2*np.pi*tick*.01) if tick < 400 else 0.
        target[:3, :3] = anchor[:3, :3] @ rotation([0, 1, 0], theta)
        following = arm.pose_tracking_ik(
            target, q, .01, 180., velocity=velocity, max_accel=900.,
            input_time_s=100.+tick*.01, elbow_max_deg=J4_MAX, **QUALITY)
        new_velocity = (following-q)/.01
        assert np.all(abs(new_velocity-velocity) <= limits*.01+1e-7)
        assert np.max(abs(new_velocity)) <= 180.+1e-7
        assert arm.valid_joints(following)
        stop = following+new_velocity*np.max(abs(new_velocity)/limits)/2
        assert arm.valid_joints(stop)
        q, velocity = following, new_velocity
        actual = arm.fk(q)
        errors.append(np.degrees(angle(actual[:3, :3] @ target[:3, :3].T)))
        positions.append(np.linalg.norm(actual[:3, 3]-anchor[:3, 3]))
        trace.append(q.copy())
        speeds.append(velocity.copy())
    assert np.mean(errors[50:400]) < (.08 if side == 'left' else 1.15)
    assert max(positions) < (.001 if side == 'left' else .005)
    assert errors[-1] < .06 and positions[-1] < .0001
    if side == 'right':
        # The old right-wrist boundary reallocation drove J3 at 58 deg/s,
        # with about 695 degrees of summed joint travel in four small turns.
        assert np.max(abs(np.array(speeds)[:, 2])) < 20.
        assert np.sum(abs(np.diff(trace, axis=0))) < 650.


def test_small_turn_does_not_inherit_a_remote_unsent_singular_goal(models, monkeypatch):
    from marvin_teleop import pose_tracking

    arm = models.arms['right']
    arm.reset_pose_tracking()
    q = np.zeros(7)
    first = arm.fk(q)
    arm.pose_tracking_ik(first, q, .01, 180., velocity=q, max_accel=900., **QUALITY)
    arm._pose_tracking['goal'] = q+np.array([50., 0., -50., 0., 0., 0., 0.])
    target = first.copy()
    target[:3, :3] = rotation([0, 0, 1], .002) @ first[:3, :3]
    seeds = []
    original = pose_tracking._new_target

    def observed(arm, target, seed, *args, **kwargs):
        seeds.append(seed.copy())
        return original(arm, target, seed, *args, **kwargs)

    monkeypatch.setattr(pose_tracking, '_new_target', observed)
    arm.pose_tracking_ik(target, q, .01, 180., velocity=q, max_accel=900., **QUALITY)
    assert seeds and np.max(abs(seeds[0]-q)) <= 2.+1e-9


@pytest.mark.parametrize('side', ['left', 'right'])
def test_teleop_j4_limit_includes_stopping_distance_without_changing_hardware(models, side):
    arm = models.arms[side]
    arm.reset_pose_tracking()
    q = np.array([10., -35., 15., J4_MAX-.2, 5., 15., -10.])
    witness = q.copy()
    witness[3] = 20.
    target = arm.fk(witness)
    assert arm.valid_joints(witness)
    physical_faces, physical_bounds = arm.joint_halfspaces()
    velocity = np.zeros(7)
    constrained = False
    limits = np.minimum(900., arm.acceleration_limits)
    for _ in range(250):
        following = arm.pose_tracking_ik(target, q, .01, 180., velocity=velocity,
                                         max_accel=900., elbow_max_deg=J4_MAX, **QUALITY)
        new_velocity = (following-q)/.01
        stop = following+new_velocity*np.max(abs(new_velocity)/limits)/2
        assert following[3] <= J4_MAX and stop[3] <= J4_MAX
        assert arm.valid_joints(following) and arm.valid_joints(stop)
        assert np.all(abs(new_velocity-velocity) <= limits*.01+1e-7)
        constrained |= arm.last_ik_constrained
        q, velocity = following, new_velocity
    assert constrained
    assert arm.hardware_upper[3] == 60.
    assert arm.valid_joints(witness)
    faces, bounds = arm.joint_halfspaces()
    np.testing.assert_equal(faces, physical_faces)
    np.testing.assert_equal(bounds, physical_bounds)




@pytest.mark.parametrize('side', ['left', 'right'])
def test_manual_and_preparation_can_use_positive_j4_above_five(models, side):
    from teleop_control.tianji_models import TianjiMove
    from teleop_control.tianji_worker import manual_plan
    from marvin_teleop.preparation import PreparationPlan

    cfg = configuration(CONFIG)
    measured = {s: np.zeros(7) for s in models.arms}
    measured[side][3] = 20.
    # Only the ordinary manufacturer and collision checks apply to manual
    # jogs and explicit preparation. Teleop's -5-degree face must not leak.
    plan = manual_plan(models, measured, TianjiMove(kind='jog', mode=side,
                       side=side, joint=4, delta=5.), cfg)
    assert plan['goal'][side][3] == 25.
    prep = PreparationPlan(models, measured, cfg, validate=False, sides={side})
    assert prep.start[side][3] == 20.


@pytest.mark.parametrize('side', ['left', 'right'])
def test_reaching_j4_boundary_keeps_clutch_active_and_allows_other_motion_and_reverse(models, side, monkeypatch):
    from marvin_teleop.protocol import Frame

    cfg = configuration(CONFIG)
    robot = SimRobot()
    robot.q[side] = np.array([10., -35., 15., J4_MAX-.2, 5., 15., -10.])
    ctrl = Controller(models, robot, cfg)
    clock = [100.]
    monkeypatch.setattr('marvin_teleop.control.time.monotonic', lambda: clock[0])
    arm = models.arms[side]
    anchor = arm.fk(robot.q[side])

    def tick(target, grip=1.):
        clock[0] += .01
        hand = np.eye(4)
        a = ctrl.arms[side]
        if a.basis is not None:
            hand[:3, 3] = a.basis.T @ (target[:3, 3]-anchor[:3, 3])
            hand[:3, :3] = a.basis.T @ target[:3, :3] @ anchor[:3, :3].T @ a.basis
        frame = Frame(received=clock[0], session=1, sequence=round(clock[0]*1000),
                      source_time=clock[0], focused=True, tracked=True,
                      hands={s: hand if s == side else np.eye(4) for s in models.arms},
                      head=np.eye(4), grips={s: grip if s == side else 0. for s in models.arms}, stop=False)
        ctrl.tick(frame, True, False, clock[0])
        if grip:
            assert ctrl.arms[side].mode == 'ACTIVE', ctrl.arms[side].reason
            assert not ctrl.fault
            assert robot.q[side][3] <= J4_MAX

    tick(anchor, 0.)
    tick(anchor)
    outward = robot.q[side].copy()
    for index in range(200):
        outward[3] = J4_MAX-.2+15.2*min(1., (index+1)/100.)
        tick(arm.fk(outward))
    assert ctrl.arms[side].constrained_count > 0
    reached = robot.q[side].copy()
    tangent = reached.copy()
    tangent[6] += 2.
    for _ in range(200):
        tick(arm.fk(tangent))
    assert np.max(abs(robot.q[side]-reached)) > .5
    inward = robot.q[side].copy()
    inward[3] -= 4.
    for _ in range(250):
        tick(arm.fk(inward))
    assert robot.q[side][3] < reached[3]-1.


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('initial_j4', [-4., -2., 0., 15., 45., 60.])
def test_above_negative_limit_ratchets_inward_without_forced_return(models, side, initial_j4):
    arm = models.arms[side]
    arm.reset_pose_tracking()
    q = np.array([10., -35., 15., initial_j4, 5., 15., -10.])
    initial = q.copy()
    target = arm.fk(q)
    velocity = np.zeros(7)
    for _ in range(20):
        following = arm.pose_tracking_ik(target, q, .01, 180., velocity=velocity,
                                         max_accel=900., elbow_max_deg=J4_MAX, **QUALITY)
        np.testing.assert_allclose(following, initial, atol=1e-12)
    witness = initial.copy()
    witness[3] = -25.
    target = arm.fk(witness)
    for _ in range(500):
        following = arm.pose_tracking_ik(target, q, .01, 180., velocity=velocity,
                                         max_accel=900., elbow_max_deg=J4_MAX, **QUALITY)
        new_velocity = (following-q)/.01
        limit = max(J4_MAX, q[3])
        assert following[3] <= limit
        limits = np.minimum(900., arm.acceleration_limits)
        stop = following+new_velocity*np.max(abs(new_velocity)/limits)/2
        assert stop[3] <= limit
        assert np.all(abs(new_velocity-velocity) <= limits*.01+1e-7)
        q, velocity = following, new_velocity
    if initial_j4 <= 0.:
        assert q[3] < J4_MAX
    else:
        assert q[3] < initial_j4  # No automatic branch change just to recenter.
    if initial_j4 <= 0.:
        assert np.linalg.norm(arm.fk(q)[:3,3]-target[:3,3]) < .0001
    else:
        assert arm.valid_hardware_joints(q)


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('initial_j4', [0., 60.])
def test_controller_can_clutch_at_zero_or_hardware_max_and_retreat_without_snap(models, side, initial_j4, monkeypatch):
    from marvin_teleop.protocol import Frame

    cfg = configuration(CONFIG)
    robot = SimRobot()
    robot.q[side] = np.array([10., -35., 15., initial_j4, 5., 15., -10.])
    ctrl = Controller(models, robot, cfg)
    clock = [100.]
    monkeypatch.setattr('marvin_teleop.control.time.monotonic', lambda: clock[0])
    arm = models.arms[side]
    initial = robot.q[side].copy()
    anchor = arm.fk(initial)
    for index in range(120):
        clock[0] += .01
        hand = np.eye(4)
        a = ctrl.arms[side]
        if index > 20 and a.basis is not None:
            witness = initial.copy()
            witness[3] -= 3.*min(1., (index-20)/50.)
            target = arm.fk(witness)
            hand[:3, 3] = a.basis.T @ (target[:3, 3]-anchor[:3, 3])
            hand[:3, :3] = a.basis.T @ target[:3, :3] @ anchor[:3, :3].T @ a.basis
        frame = Frame(received=clock[0], session=1, sequence=index, source_time=clock[0],
                      focused=True, tracked=True, head=np.eye(4),
                      hands={s: hand if s == side else np.eye(4) for s in models.arms},
                      grips={s: 1. if s == side and index > 0 else 0. for s in models.arms}, stop=False)
        previous = robot.q[side][3]
        ctrl.tick(frame, True, False, clock[0])
        if index > 0:
            assert ctrl.arms[side].mode == 'ACTIVE', ctrl.arms[side].reason
            assert robot.q[side][3] <= max(J4_MAX, previous)
            if index <= 20:
                np.testing.assert_allclose(robot.q[side], initial, atol=1e-12)
    assert robot.q[side][3] < initial_j4-.05
