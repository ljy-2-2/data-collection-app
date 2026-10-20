"""Recovery state machine and native SDK sequencing with synthetic feedback only."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from teleop_control import tianji_worker
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models
from marvin_teleop.recovery import ZeroRecovery
from marvin_teleop.robot import Robot, CommandBusy

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'


@pytest.fixture(scope='module')
def models():
    return Models(configuration(CONFIG))


@pytest.fixture
def rig(monkeypatch, models):
    from SDK_PYTHON import fx_robot
    clock = [100.]
    monkeypatch.setattr('marvin_teleop.robot.time.monotonic', lambda: clock[0])
    states = [100, 100]; errors = [13, 13]
    q = [np.array([0., 0., 0., -12., 0., 0., 0.]), np.array([0., 0., 0., -8., 0., 0., 0.])]
    events = []; frame = [1]; advancing = [True]; persistent = [False]
    pending = {}; goals = {}
    api = Mock()
    api.robot = SimpleNamespace(OnClearErr_A=Mock(), OnClearErr_B=Mock())
    api.connect.return_value = True; api.clear_set.return_value = True
    def clear(channel):
        events.append(('clear', channel))
        if not persistent[0]:errors['AB'.index(channel)] = 0
        return None  # The actual C ABI is void, never a boolean success flag.
    api.clear_error.side_effect = clear
    def set_state(channel, state):
        events.append(('state', channel, state));pending[channel] = state;return True
    api.set_state.side_effect = set_state
    def set_joint(channel, joints):
        events.append(('pose', channel, list(joints)));goals[channel] = np.asarray(joints);return True
    api.set_joint_cmd_pose.side_effect = set_joint
    api.set_vel_acc.return_value = True
    def send():
        events.append(('send',))
        for ch, st in pending.items():states['AB'.index(ch)] = 100 if errors['AB'.index(ch)] else st
        for ch, goal in goals.items():
            if states['AB'.index(ch)] == 1:q['AB'.index(ch)] = goal.copy()
        pending.clear();goals.clear()
        assert sum(s == 1 for s in states) <= 1, 'Zeroing must be sequential'
        return True
    api.send_cmd.side_effect = send
    def subscribe(_):
        if advancing[0]:frame[0] += 1
        return dict(states=[dict(cur_state=st, err_code=err) for st, err in zip(states, errors)],
                    outputs=[dict(frame_serial=frame[0],fb_joint_pos=x.tolist(),fb_joint_vel=[0.]*7) for x in q])
    api.subscribe.side_effect = subscribe
    monkeypatch.setattr(fx_robot, 'Marvin_Robot', lambda: api)
    monkeypatch.setattr(fx_robot, 'DCSS', lambda: None)
    config = configuration(CONFIG)
    robot = Robot(config)
    robot.read_servo_errors = lambda: dict(left=[0]*7,right=[0]*7)
    monkeypatch.setattr(models, 'collision_reason', lambda *a, **kw: None)
    def run(recovery, steps=15000):
        for _ in range(steps):
            clock[0] += .01
            recovery.tick(clock[0])
            if recovery.done:return
        pytest.fail('Recovery did not finish')
    return SimpleNamespace(robot=robot,config=config,models=models,clock=clock,api=api,states=states,
                           errors=errors,q=q,events=events,advancing=advancing,persistent=persistent,run=run)


def test_latched_emergency_clears_then_holds_then_enables_one_arm_at_a_time(rig):
    with pytest.raises(ValueError, match='controller state'):rig.robot.read()
    recovery=ZeroRecovery(rig.robot,rig.models,rig.config,('left','right'),rig.clock[0])
    rig.run(recovery)
    assert recovery.done and rig.states == [0,0]
    assert not rig.robot.enabled and all(np.max(np.abs(q)) < 1e-10 for q in rig.q)
    assert [x for x in rig.events if x[0]=='clear'] == [('clear','A'),('clear','B')]
    enables=[i for i,x in enumerate(rig.events) if x[0]=='state' and x[2]==1]
    assert [rig.events[i][1] for i in enables] == ['A','B']
    for i in enables:
        channel=rig.events[i][1]
        assert sum(x[0]=='pose' and x[1]==channel for x in rig.events[:i]) >= 2
        assert any(x==('send',) for x in rig.events[:i])
    assert rig.api.robot.OnClearErr_A.restype is None


@pytest.mark.parametrize('position', [-145., 59.9])
def test_recovery_accepts_exact_vendor_limits_but_normal_teleop_reserve_rejects(rig,position):
    rig.q[0][3]=position
    assert not rig.models.arms['left'].valid_joints(rig.q[0])
    assert rig.models.arms['left'].valid_hardware_joints(rig.q[0])
    rig.run(ZeroRecovery(rig.robot,rig.models,rig.config,('left',),rig.clock[0]))
    assert rig.q[0][3] == 0
    assert rig.q[1][3] == -8


@pytest.mark.parametrize('fault', ['hard_limit','collision','stale','servo','persistent_estop'])
def test_real_faults_never_enable_or_report_success(rig,fault,monkeypatch):
    if fault=='hard_limit':rig.q[0][3] = -145.1
    if fault=='collision':monkeypatch.setattr(rig.models,'collision_reason',lambda *a, **kw:'obstacle')
    if fault=='stale':rig.advancing[0]=False
    if fault=='servo':rig.robot.read_servo_errors=lambda:dict(left=[1]*7,right=[0]*7)
    if fault=='persistent_estop':rig.persistent[0]=True
    recovery=ZeroRecovery(rig.robot,rig.models,rig.config,('left','right'),rig.clock[0])
    with pytest.raises((ValueError,RuntimeError)):
        rig.run(recovery)
    assert not recovery.done
    assert not any(x[0]=='state' and x[2]==1 for x in rig.events)


def test_recovery_retries_busy_transaction_without_duplicating_movement(rig,monkeypatch):
    original=rig.robot.stage_recovery_hold
    calls=[0]
    def hold(side):
        calls[0]+=1
        if calls[0]==1:raise CommandBusy('transient')
        original(side)
    monkeypatch.setattr(rig.robot,'stage_recovery_hold',hold)
    rig.run(ZeroRecovery(rig.robot,rig.models,rig.config,('left',),rig.clock[0]))
    assert calls[0]==2
    assert [e for e in rig.events if e==('state','A',1)]==[('state','A',1)]


REPORTED_POSE = {
    'left': np.array([10.8288, -45.657, .6119, -57.9142, 29.2839, -46.1642, 27.7179]),
    'right': np.array([-120.333, -59.9347, 147.4432, -130.0947, -60.3889, -24.0946, 20.2804]),
}


def test_reported_clearance_warning_recovers_right_first_with_real_geometry(rig, monkeypatch):
    # Unlike the basic SDK fixtures, reproduce the actual CAD checks that
    # blocked recovery: right link 2/5 is separated by ~14.215 mm, under 15 mm.
    monkeypatch.setattr(rig.models, 'collision_reason', Models.collision_reason.__get__(rig.models))
    for i, side in enumerate(('left', 'right')):
        rig.q[i][:] = REPORTED_POSE[side]
    assert rig.models.collision_reason(REPORTED_POSE) == 'right: nonadjacent links 2/5 too close'
    assert rig.models.collision_reason(REPORTED_POSE, 0.) is None
    original_command = rig.robot.command
    command_calls = [0]
    def busy_once(joints, deadline=None):
        command_calls[0] += 1
        if command_calls[0] == 1:
            raise CommandBusy('synthetic send-buffer contention during clearance recovery')
        return original_command(joints, deadline=deadline)
    monkeypatch.setattr(rig.robot, 'command', busy_once)
    recovery = ZeroRecovery(rig.robot, rig.models, rig.config, ('left', 'right'), rig.clock[0])
    rig.run(recovery, steps=30000)
    assert [e[1] for e in rig.events if e[0] == 'state' and e[2] == 1] == ['B', 'A']
    assert rig.states == [0, 0] and recovery.done
    assert all(np.max(np.abs(q)) < 1e-9 for q in rig.q)
    assert recovery.clearance.floors[('right', 2, 5)] == rig.config['collision_margin_m']
    for channel in ('A', 'B'):
        poses = np.array([e[2] for e in rig.events if e[0] == 'pose' and e[1] == channel])
        velocity = np.diff(poses, axis=0) / .01
        assert np.max(np.abs(velocity)) <= 5.4 + 1e-6
        assert np.max(np.abs(np.diff(velocity, axis=0) / .01)) <= 5.0 + 1e-6


def test_clearance_recovery_rejects_deeper_folding_and_never_relaxes_new_pairs(models):
    from marvin_teleop.recovery_clearance import RecoveryClearance
    guard = RecoveryClearance(models, REPORTED_POSE, .015)
    assert guard.reason(REPORTED_POSE) is None
    assert set(guard.floors) == {('right', 2, 5)}
    deeper = {s: q.copy() for s, q in REPORTED_POSE.items()}
    deeper['right'][3] -= 1.
    assert guard.reason(deeper) is not None
    improved = {s: q.copy() for s, q in REPORTED_POSE.items()}
    improved['right'] *= .99
    assert guard.reason(improved) is None
    guard.advance(improved)
    assert guard.reason(REPORTED_POSE) is not None, 'Recovered clearance cannot be surrendered'
    assert guard.fork().floors is not guard.floors
    assert models.collision_reason(REPORTED_POSE) is not None, 'Normal teleop keeps the full reserve'


def test_overlapping_housings_are_not_recovery_exemptions(rig, monkeypatch):
    monkeypatch.setattr(rig.models, 'collision_reason', Models.collision_reason.__get__(rig.models))
    for i, side in enumerate(('left', 'right')):
        rig.q[i][:] = REPORTED_POSE[side]
    rig.q[1][3] = -145.
    joints = dict(left=rig.q[0], right=rig.q[1])
    assert rig.models.collision_reason(joints, 0.) is not None
    recovery = ZeroRecovery(rig.robot, rig.models, rig.config, ('left', 'right'), rig.clock[0])
    with pytest.raises(ValueError, match='路径存在干涉'):
        rig.run(recovery)
    assert not any(e[0] == 'state' and e[2] == 1 for e in rig.events)


def test_stop_during_zero_recovery_disables_and_does_not_start_peer(rig):
    recovery=ZeroRecovery(rig.robot,rig.models,rig.config,('left','right'),rig.clock[0])
    for _ in range(200):
        rig.clock[0]+=.01;recovery.tick(rig.clock[0])
        if recovery.phase=='moving':break
    assert recovery.phase=='moving'
    rig.robot.stop()
    assert rig.states==[0,0] and not rig.robot.enabled
    assert not recovery.done
    assert not any(e==('state','B',1) for e in rig.events)


def test_worker_zero_recovers_without_check_or_vr(rig,monkeypatch,tmp_path):
    import io,json,queue,threading
    from teleop_control import tianji_worker as worker
    incoming=queue.Queue(); samples=[]; started=[False]
    close_queued=threading.Event()
    rig.q[0][3]=-.1;rig.q[1][3]=-.1
    incoming.put(json.dumps(dict(action='move',id='zero',payload=dict(kind='zero',mode='dual')))+'\n')
    class Input:
        def __iter__(self):
            while True:
                item=incoming.get(timeout=5)
                if item is None:return
                yield item
                if json.loads(item).get("action") == "close":close_queued.set()
    class Publisher:
        def __init__(self,**kw):pass
        def submit(self,row,*args):
            samples.append(row);rig.clock[0]+=.02
            assert len(samples)<500,'Worker recovery never completed'
            assert row['state']!='error'
            if row['state']=='moving':started[0]=True
            elif started[0] and row['state']=='idle':
                incoming.put(json.dumps(dict(action='close',id='close'))+'\n')
                assert close_queued.wait(1), 'Input thread did not enqueue close'
                started[0]=False
        def close(self):pass
    monkeypatch.setattr(worker,'__file__',str(tmp_path/'src/teleop_control/tianji_worker.py'))
    monkeypatch.setattr(worker.sys,'stdin',Input())
    monkeypatch.setattr(worker.sys,'stdout',io.StringIO())
    monkeypatch.setattr(worker.signal,'signal',lambda *args:None)
    monkeypatch.setattr(worker,'load_configuration',lambda:rig.config)
    monkeypatch.setattr(worker,'Models',lambda config:rig.models)
    monkeypatch.setattr(worker,'validate_preview',lambda *args:None)
    monkeypatch.setattr(worker,'RecordingRobot',lambda config:rig.robot)
    monkeypatch.setattr(worker,'Publisher',Publisher)
    monkeypatch.setattr(worker,'PicoLauncher',lambda *args:pytest.fail('Zero must not launch VR'))
    try:worker.main()
    finally:incoming.put(None)
    assert any(s['state']=='moving' for s in samples)
    assert rig.states==[0,0] and all(np.max(np.abs(q))<1e-8 for q in rig.q)


def test_manager_launches_robot_worker_for_explicit_zero_without_device_check():
    import threading
    from teleop_control.tianji import TianjiManager
    manager=SimpleNamespace(command_lock=threading.Lock(),collection=SimpleNamespace(armed=False,active=lambda:False),
                            active=lambda:False,_launch=Mock(),_command=Mock(return_value='ok'))
    assert TianjiManager.command(manager,'move',dict(kind='zero'))=='ok'
    manager._launch.assert_called_once_with()
    manager.collection.armed=True
    with pytest.raises(ValueError,match='停止遥操作和采集'):
        TianjiManager.command(manager,'move',dict(kind='zero'))
