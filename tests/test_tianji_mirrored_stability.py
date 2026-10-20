"""Production solver: mirrored hand input and stationary singular targets."""
import json
from copy import copy
from pathlib import Path
import numpy as np
import pytest
from teleop_control import tianji_worker
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models
from marvin_teleop.mirror import LeftFromRight
from marvin_teleop.geometry import inverse, relative_target, transform, rotation
from marvin_teleop.mapping import same_facing_map

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'
CASES = json.loads((ROOT/'tests/fixtures/tianji_held_target_oscillation.json').read_text())
SIGNS = np.array([-1.,1.,-1.,1.,-1.,1.,-1.])
QUALITY = dict(singular_rotation_weight_m=.05, joint_limit_soft_margin_deg=30.,
               joint_limit_reserve_deg=3.,joint_limit_escape_speed_deg_s=20.)


@pytest.fixture(scope='module')
def models():
    return Models(configuration(CONFIG))


def bounded_step(arm, target, q, v, dt=.01):
    n = arm.pose_tracking_ik(target,q,dt,180,velocity=v,max_accel=450,**QUALITY)
    nv = (n-q)/dt
    assert arm.valid_joints(n)
    assert np.max(abs(nv)) <= 180+1e-7
    assert np.max(abs(nv-v)) <= 450*dt+1e-7
    return n,nv


@pytest.mark.parametrize('case', CASES, ids=lambda c:str(c['case']))
@pytest.mark.parametrize('noise', [False,True])
def test_held_target_stops_instead_of_repeatedly_reversing(models,case,noise):
    left,right = models.arms['left'],models.arms['right']
    mirror = left.tracking_mirror
    qr = np.array(case['q']);ql = SIGNS*qr
    vr,vl = np.zeros(7),np.zeros(7)
    target = np.array(case['target']);traces = {s:[] for s in ('left','right')}
    left.reset_pose_tracking();right.reset_pose_tracking()
    for tick in range(1100):
        observed = target.copy()
        if noise:
            observed[:3,3] += .00002*np.sin(tick*.17+np.arange(3))
            observed[:3,:3] = rotation([0,0,1],.0001*np.sin(tick*.13))@target[:3,:3]
        qr,vr = bounded_step(right,observed,qr,vr)
        ql,vl = bounded_step(left,inverse(mirror.world)@observed@mirror.tcp,ql,vl)
        np.testing.assert_allclose(ql,SIGNS*qr,atol=1e-6)
        traces['left'].append(ql.copy());traces['right'].append(qr.copy())
    for trace in traces.values():
        tail = np.array(trace)[600:]
        assert np.max(np.ptp(tail,axis=0)) < .01
        assert np.max(abs(np.diff(tail,axis=0)/.01)) < .01
    assert case['hold_range_deg']>10 and case['max_speed_deg_s']>70


@pytest.mark.parametrize('start', [[0.]*7,[20.,-35,15,-65,5,15,-10]])
def test_same_anatomical_hand_motion_produces_mirrored_full_joint_trajectory(models,start):
    l,r = models.arms['left'],models.arms['right']
    qr = np.array(start);ql = SIGNS*qr;vl,vr = np.zeros(7),np.zeros(7)
    l.reset_pose_tracking();r.reset_pose_tracking()
    f = np.diag([-1.,1.,1.,1.])
    hand0r = transform([.3,1.2,-.4],[.1,.2,-.1]);hand0l = f@hand0r@f
    tcp0r,tcp0l = r.fk(qr),l.fk(ql)
    basis = same_facing_map(np.eye(4))
    for tick in range(250):
        h = hand0r.copy();amount=.05*min(1.,(tick+1)/150)
        h[:3,3] += [-amount,amount*.3,-amount]
        h[:3,:3] = rotation([0,1,0],amount*.3)@h[:3,:3]
        tr = relative_target(h,hand0r,tcp0r,basis,1.)
        tl = relative_target(f@h@f,hand0l,tcp0l,basis,1.)
        np.testing.assert_allclose(l.tracking_mirror.world@tl@l.tracking_mirror.tcp,tr,atol=1e-10)
        qr,vr = bounded_step(r,tr,qr,vr);ql,vl = bounded_step(l,tl,ql,vl)
        np.testing.assert_allclose(ql,SIGNS*qr,atol=1e-6)


def test_left_solver_reflects_actual_tool_and_independent_installation(models):
    l,r = copy(models.arms['left']),copy(models.arms['right'])
    l.base = transform([.01,.02,-.01],[.02,-.03,.01])@l.base
    r.base = transform([-.01,0,.02],[-.01,.01,-.02])@r.base
    l.tool = transform([.02,-.01,.14],[.1,-.2,.3])
    mirror = LeftFromRight(l,r)
    for q in (np.zeros(7),np.array([20.,-35,15,-65,5,15,-10])):
        expected = mirror.world@l.fk(q)@mirror.tcp
        np.testing.assert_allclose(mirror.arm.fk(SIGNS*q),expected,atol=1e-9)
        j = l.world_jacobian(q,l.fk(q));h = mirror.world[:3,:3]
        expected_j = np.vstack((h@j[:3],-h@j[3:]))*SIGNS
        np.testing.assert_allclose(mirror.arm.world_jacobian(SIGNS*q,expected),expected_j,atol=1e-9)


def test_reclutch_discards_previous_optimizer_goal(models):
    for arm in models.arms.values():
        q = np.array([10.,-35,15,-65,5,15,-10]);target = arm.fk(q);target[0,3]+=.05
        bounded_step(arm,target,q,np.zeros(7))
        arm.reset_pose_tracking()
        np.testing.assert_allclose(bounded_step(arm,arm.fk(q),q,np.zeros(7))[0],q,atol=1e-12)


@pytest.mark.parametrize('singularity', ['shoulder','elbow','wrist'])
def test_slow_reachable_pose_path_through_singularity_stays_local(models,singularity):
    arm = models.arms['right']
    starts = {'shoulder':np.array([30.,0.,50.,-65.,20.,10.,-5.]),
              'elbow':np.array([10.,-35.,15.,arm.elbow_singularity_deg,5.,15.,-10.]),
              'wrist':np.array([10.,-35.,15.,-65.,5.,0.,-10.])}
    axis = {'shoulder':1,'elbow':3,'wrist':5}[singularity]
    initial = starts[singularity];q = initial.copy();v = np.zeros(7)
    arm.reset_pose_tracking()
    for tick in range(700):
        source = initial.copy();source[axis] += 3.*np.sin(2*np.pi*tick/600)
        q,v = bounded_step(arm,arm.fk(source),q,v)
        assert np.max(abs(q-initial)) < 15.
        assert np.max(abs(v)) < 45.
