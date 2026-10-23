"""Position-mode regression tests using a fake SDK, without hardware access."""

from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from teleop_control import tianji_worker
from marvin_teleop.robot import Robot


@pytest.fixture
def session(monkeypatch):
    from SDK_PYTHON import fx_robot

    api = Mock()
    for method in ('connect', 'clear_set', 'send_cmd', 'set_joint_cmd_pose',
                   'set_vel_acc', 'set_PD_vel_est_step', 'set_state'):
        getattr(api, method).return_value = True
    feedback = SimpleNamespace(states=[0, 0], serial=1, advance=True)

    def subscribe(dcss):
        if feedback.advance:
            feedback.serial += 1
        return dict(
            states=[dict(cur_state=state, err_code=0) for state in feedback.states],
            outputs=[dict(frame_serial=feedback.serial, fb_joint_pos=[10.] * 7,
                          fb_joint_vel=[0.] * 7) for _ in range(2)],
        )

    api.subscribe.side_effect = subscribe
    monkeypatch.setattr(fx_robot, 'Marvin_Robot', lambda: api)
    monkeypatch.setattr(fx_robot, 'DCSS', lambda: None)
    monkeypatch.setattr('marvin_teleop.robot.time.monotonic', lambda: 100.)
    robot = Robot(dict(robot_ip='127.0.0.1', arm_channels=dict(left='A', right='B'),
                       rate_hz=100, robot_timeout_s=.15))
    return SimpleNamespace(api=api, robot=robot, feedback=feedback)


def test_enable_uses_original_position_mode_without_pd_or_parameter_calls(session):
    robot, api = session.robot, session.api
    robot.enable()
    api.set_state.assert_has_calls([call('A', 1), call('B', 1)])
    api.set_PD_vel_est_step.assert_not_called()
    api.set_joint_kd_params.assert_not_called()
    api.set_impedance_type.assert_not_called()
    api.robot.OnGetIntPara.assert_not_called()
    api.robot.OnSetIntPara.assert_not_called()
    api.robot.OnSavePara.assert_not_called()
    assert not robot.enabled_sides
    session.feedback.states = [1, 1]
    robot.read()
    assert robot.enabled_sides == {'left', 'right'}
    robot.__class__ = tianji_worker.RecordingRobot
    robot.command(robot.q)
    assert robot.last_command['kind'] == 'sdk_accepted_joint_target'
    assert robot.last_command['joints_deg'] == dict(left=[10.] * 7, right=[10.] * 7)
    api.set_PD_vel_est_step.assert_not_called()


def test_position_enable_requires_fresh_acknowledgement(session):
    session.robot.enable(['left'])
    session.feedback.states = [1, 0]
    session.feedback.advance = False
    session.robot.read()
    assert not session.robot.enabled_sides
    session.feedback.advance = True
    session.robot.read()
    assert session.robot.enabled_sides == {'left'}


def test_position_mode_rejects_unexpected_impedance_feedback(session):
    session.robot.enable()
    session.feedback.states = [1, 1]
    session.robot.read()
    session.feedback.states[0] = 3
    with pytest.raises(ValueError, match='controller state'):
        session.robot.read()


def test_position_mode_setter_failure_prevents_enable_packet(session):
    session.api.set_state.return_value = False
    with pytest.raises(RuntimeError, match='set_state position'):
        session.robot.enable()
    session.api.send_cmd.assert_not_called()
    session.api.set_PD_vel_est_step.assert_not_called()
    assert not session.robot.enabled


def test_feedback_counter_wrap_keeps_enabled_arms_running(session, monkeypatch):
    robot = session.robot
    session.feedback.advance = False
    session.feedback.states = [1, 1]
    robot.enabled_sides = {'left', 'right'}
    for tick, serial in enumerate((999980, 999990, 0, 10, 20)):
        now = 100. + tick * .01
        monkeypatch.setattr('marvin_teleop.robot.time.monotonic', lambda: now)
        session.feedback.serial = serial
        robot.read()
        assert robot.frames == dict(left=serial, right=serial)
        assert robot.changed == dict(left=now, right=now)
        assert robot.enabled_sides == {'left', 'right'}
    session.api.set_state.assert_not_called()
    session.api.send_cmd.assert_not_called()


@pytest.mark.parametrize('allow_faults', [False, True])
def test_initial_zero_cache_is_not_ready(session, allow_faults):
    session.feedback.advance = False
    session.feedback.serial = 0
    for _ in range(3):
        with pytest.raises(ValueError, match='robot UDP feedback not advancing'):
            session.robot.read(allow_faults=allow_faults)
        assert session.robot.frames == dict(left=None, right=None)
    session.feedback.serial = 10
    session.robot.read(allow_faults=allow_faults)


@pytest.mark.parametrize('serial', [0, 999990])
def test_frozen_feedback_still_times_out_after_wrap(session, monkeypatch, serial):
    session.feedback.advance = False
    session.feedback.serial = 999980
    session.robot.read()
    monkeypatch.setattr('marvin_teleop.robot.time.monotonic', lambda: 100.01)
    session.feedback.serial = serial
    session.robot.read()
    monkeypatch.setattr('marvin_teleop.robot.time.monotonic', lambda: 100.10)
    session.robot.read()
    assert session.robot.changed == dict(left=100.01, right=100.01)
    monkeypatch.setattr('marvin_teleop.robot.time.monotonic', lambda: 100.17)
    with pytest.raises(ValueError, match='robot UDP feedback not advancing'):
        session.robot.read()


def test_wrapped_frame_does_not_hide_controller_fault(session):
    session.feedback.advance = False
    session.feedback.serial = 999990
    session.robot.read()
    session.feedback.serial = 0
    session.feedback.states = [3, 0]
    with pytest.raises(ValueError, match='controller state'):
        session.robot.read()


@pytest.mark.parametrize('missing_side', ['left', 'right'])
def test_peer_feedback_cannot_validate_initial_zero_lane(session, missing_side):
    session.api.subscribe.side_effect = lambda _: dict(
        states=[dict(cur_state=0, err_code=0) for _ in range(2)],
        outputs=[dict(frame_serial=0 if side == missing_side else 999990,
                      fb_joint_pos=[10.] * 7, fb_joint_vel=[0.] * 7)
                 for side in ('left', 'right')],
    )
    with pytest.raises(ValueError, match=missing_side + ': robot UDP feedback not advancing'):
        session.robot.read()


@pytest.mark.parametrize('stale_side', ['left', 'right'])
def test_advancing_peer_cannot_hide_stalled_zero_lane(session, monkeypatch, stale_side):
    session.feedback.advance = False
    session.feedback.serial = 999990
    session.robot.read()
    session.feedback.serial = 0
    session.robot.read()
    monkeypatch.setattr('marvin_teleop.robot.time.monotonic', lambda: 100.16)
    session.api.subscribe.side_effect = lambda _: dict(
        states=[dict(cur_state=0, err_code=0) for _ in range(2)],
        outputs=[dict(frame_serial=0 if side == stale_side else 160,
                      fb_joint_pos=[10.] * 7, fb_joint_vel=[0.] * 7)
                 for side in ('left', 'right')],
    )
    with pytest.raises(ValueError, match=stale_side + ': robot UDP feedback not advancing'):
        session.robot.read()


@pytest.mark.parametrize('operation', ['enable', 'stop'])
def test_mode_acknowledgement_across_counter_wrap(session, operation):
    robot = session.robot
    session.feedback.advance = False
    session.feedback.serial = 999990
    robot.enable()
    if operation == 'stop':
        robot.stop(['left', 'right'])
    session.feedback.states = [1, 1] if operation == 'enable' else [0, 0]
    pending = robot.pending_sides if operation == 'enable' else robot.stopping_sides
    robot.read()
    assert set(pending) == {'left', 'right'}  # Same serial is never an ACK.
    session.feedback.serial = 0
    robot.read()  # Wrap does not fault while the existing ACK guard waits.
    assert set(pending) == {'left', 'right'}
    session.feedback.serial = 10
    robot.read()
    assert not pending
    assert robot.enabled_sides == ({'left', 'right'} if operation == 'enable' else set())
