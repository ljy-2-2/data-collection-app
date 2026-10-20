"""Reproduce forward/inward folding from horizontal zero without changing limits."""
from pathlib import Path
import numpy as np
import pytest
from teleop_control import tianji_worker
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models
from marvin_teleop.geometry import angle

CONFIG=Path(__file__).resolve().parents[1]/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'

@pytest.fixture(scope='module')
def models():return Models(configuration(CONFIG))


def step(arm,target,q,v,anchor,forward):
    return arm.pose_tracking_ik(target,q,.01,180,velocity=v,max_accel=450,
        singular_rotation_weight_m=.05,
        joint_limit_soft_margin_deg=30,joint_limit_reserve_deg=3,joint_limit_escape_speed_deg_s=20)



@pytest.mark.parametrize('side',['left','right'])
@pytest.mark.parametrize('forward',[True,False])
def test_horizontal_zero_folds_on_requested_side_with_bounded_commands(models,side,forward):
    arm=models.arms[side];q=np.zeros(7);v=q.copy();anchor=q.copy()
    target=arm.fk(q);target[:3,3]+=[.08 if forward else -.08,-.08 if side=='left' else .08,0]
    trace=[]
    for tick in range(550):
        nextq=step(arm,target,q,v,anchor,forward)
        nextv=(nextq-q)/.01
        assert np.max(np.abs(nextv)) <= 180+1e-8
        assert np.max(np.abs(nextv-v)) <= 4.5+1e-8
        assert arm.valid_joints(nextq)
        trace.append(nextq.copy());q,v=nextq,nextv
        if forward and tick == 49:
            assert q[3] < -20, "No special slow start through negative angles"
    if forward:
        assert max(x[3] for x in trace) <= 1e-8
        assert q[3] < -20

    actual=arm.fk(q)
    assert np.linalg.norm(actual[:3,3]-target[:3,3]) < .0001
    assert angle(actual[:3,:3]@target[:3,:3].T) < .001
    assert arm.hardware_lower[3] == -145 and arm.hardware_upper[3] == 60


@pytest.mark.parametrize('side',['left','right'])
def test_no_spontaneous_fold_when_hand_target_is_stationary(models,side):
    arm=models.arms[side];q=np.zeros(7)
    for _ in range(20):np.testing.assert_allclose(step(arm,arm.fk(q),q,q,q,True),q,atol=1e-12)

@pytest.mark.parametrize('side',['left','right'])
@pytest.mark.parametrize('start',[[-5,-18,4,45,3,-25,2],[10,-35,15,-65,5,15,-10]])
@pytest.mark.parametrize('axis',range(3))
@pytest.mark.parametrize('direction',[-1,1])
def test_all_translation_axes_from_existing_positive_and_negative_poses(models,side,start,axis,direction):
    arm=models.arms[side];q=np.array(start,dtype=float);v=np.zeros(7)
    target=arm.fk(q);target[axis,3]+=.01*direction
    for _ in range(180):
        nextq=step(arm,target,q,v,q,None);nextv=(nextq-q)/.01
        assert arm.valid_joints(nextq)
        assert np.max(np.abs(nextv-v))<=4.5+1e-8
        q,v=nextq,nextv
    assert np.linalg.norm(arm.fk(q)[:3,3]-target[:3,3])<.0001
    assert angle(arm.fk(q)[:3,:3]@target[:3,:3].T)<.001
    if start[3]>0:assert q[3]>0, 'Positive configurations remain usable'

@pytest.mark.parametrize('side',['left','right'])
def test_small_continuous_hand_motion_has_no_zero_crossing_slow_phase(models,side):
    arm=models.arms[side];q=np.zeros(7);v=q.copy();initial=arm.fk(q)
    for tick in range(240):
        target=initial.copy();amount=.04*min(1,(tick+1)/100)
        target[:3,3]+=[amount,-amount if side=='left' else amount,0]
        nextq=step(arm,target,q,v,q,None);nextv=(nextq-q)/.01
        assert np.max(np.abs(nextv-v))<=4.5+1e-8
        assert np.max(np.abs(nextv))<=180+1e-8
        q,v=nextq,nextv
        if tick==49:assert q[3]<-15
    assert np.linalg.norm(arm.fk(q)[:3,3]-target[:3,3])<.0001

@pytest.mark.parametrize('side',['left','right'])
@pytest.mark.parametrize('axis',range(3))
@pytest.mark.parametrize('direction',[-1,1])
def test_whole_arm_solver_preserves_position_for_each_rotation_axis(models,side,axis,direction):
    from marvin_teleop.geometry import rotation
    arm=models.arms[side];q=np.array([10.,-35.,15.,-65.,5.,15.,-10.]);v=np.zeros(7)
    target=arm.fk(q);target[:3,:3]=rotation(np.eye(3)[axis],.03*direction)@target[:3,:3]
    for _ in range(180):
        nextq=step(arm,target,q,v,q,None);nextv=(nextq-q)/.01
        assert arm.valid_joints(nextq)
        assert np.max(np.abs(nextv-v))<=4.5+1e-8
        q,v=nextq,nextv
    assert np.linalg.norm(arm.fk(q)[:3,3]-target[:3,3])<.0001
    assert angle(arm.fk(q)[:3,:3]@target[:3,:3].T)<.001
