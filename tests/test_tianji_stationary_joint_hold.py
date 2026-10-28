"""A stopped hand cancels old task goals and brakes accepted momentum only."""
from pathlib import Path
from types import SimpleNamespace
import json
import numpy as np
import pytest
from teleop_control import tianji_worker
from marvin_teleop.configuration import configuration
from marvin_teleop.control import Controller
from marvin_teleop.model import Models
from marvin_teleop.geometry import rotation
from marvin_teleop.robot import CommandBusy

ROOT=Path(__file__).resolve().parents[1]
CASES=json.loads((ROOT/'tests/fixtures/tianji_held_target_oscillation.json').read_text())
@pytest.fixture(scope='module')
def models():return Models(configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'))


def setup(models,side,case=None):
    cfg=configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json')
    initial=np.array(case['q'] if case else [10.,-35.,15.,-65.,5.,15.,-10.])
    q={s:initial*(models.arms['left'].tracking_mirror.signs if s=='left' else 1) for s in ('left','right')}
    ctrl=Controller(models,SimpleNamespace(),cfg)
    f=SimpleNamespace(head=np.eye(4),hands={s:np.eye(4) for s in q},sequence=0,received=0.,source_time=0.)
    ctrl._activate(side,f,q)
    return ctrl,f,q


@pytest.mark.parametrize('side',['left','right'])
def test_stationary_hold_brakes_monotonically_and_never_chases_feedback_or_ik(models,side,monkeypatch):
    ctrl,f,q=setup(models,side);a=ctrl.arms[side];arm=models.arms[side]
    a.velocity=np.array([25.,-12.,9.,-15.,5.,-8.,10.])
    monkeypatch.setattr(arm,'pose_tracking_ik',lambda *a,**kw:pytest.fail('Held hands must not run IK'))
    for i in range(1,60):
        f.source_time=f.received=ctrl.last_tick=i*.01
        prev=a.velocity.copy();desired=ctrl._target(side,f,q,.01);v=(desired-a.commanded)/.01
        assert np.all(abs(v)<=abs(prev)+1e-8)
        assert np.all(v*prev>=-1e-8)
        assert np.all(abs(v-prev)<=arm.acceleration_limits*.01+1e-7)
        assert arm.valid_joints(desired)
        a.commanded=desired;a.velocity=v
        q[side]=desired+np.sin(i)*.005  # Encoder noise must not move a latched goal.
    np.testing.assert_allclose(a.velocity,0.,atol=1e-9)
    assert a.stationary_hold and a.mode=='ACTIVE'
    # Resuming uses the stopped command, not a stale 1 m Cartesian target.
    a.anchor=(np.eye(4),np.eye(4));a.anchor[1][0,3]=1.
    seen=[]
    monkeypatch.setattr(arm,'pose_tracking_ik',lambda target,reference,*a,**kw:seen.append(target.copy()) or reference.copy())
    f.source_time+=.01;f.hands[side][0,3]=.02
    ctrl._target(side,f,q,.01)
    assert not a.stationary_hold
    assert np.linalg.norm(seen[-1][:3,3]-arm.fk(a.commanded)[:3,3])<.01


@pytest.mark.parametrize('side',['left','right'])
@pytest.mark.parametrize('case',CASES,ids=lambda c:str(c['case']))
def test_move_then_noisy_stop_settles_at_singular_poses(models,side,case):
    ctrl,f,q=setup(models,side,case);a=ctrl.arms[side];rng=np.random.default_rng(37);trace=[]
    for i in range(1,501):
        f.sequence=i;f.source_time=f.received=ctrl.last_tick=i*.01
        h=np.eye(4);h[:3,3]=np.array([.02,-.01,.02])*min(1,i/30)+rng.normal(0,.0005,3)
        h[:3,:3]=rotation([0,0,1],rng.normal(0,.0025))@rotation([1,0,0],.12*min(1,i/30))
        f.hands[side]=h;desired=ctrl._target(side,f,q,.01);v=(desired-a.commanded)/.01
        assert np.all(abs(v-a.velocity)<=models.arms[side].acceleration_limits*.01+1e-7)
        a.commanded=desired;a.velocity=v;q[side]=desired;trace.append(desired.copy())
    assert np.max(np.ptp(np.array(trace)[200:],axis=0))<1e-8
    assert a.stationary_hold and a.mode=='ACTIVE'


def test_busy_sdk_does_not_commit_unaccepted_braking_velocity(models,monkeypatch):
    ctrl,f,q=setup(models,'left');a=ctrl.arms['left'];a.velocity=np.array([10.,0.,0.,-10.,0.,0.,0.])
    accepted=a.commanded.copy();speed=a.velocity.copy()
    ctrl.measured=q
    ctrl.holds={s:v.copy() for s,v in q.items()}
    monkeypatch.setattr(models,'collision_reason',lambda *a:None)
    ctrl.robot=SimpleNamespace(enabled=True,command=lambda *a,**kw:(_ for _ in ()).throw(CommandBusy('busy')))
    for i in range(2):
        f.source_time=.01*(i+1)
        desired=ctrl._target('left',f,q,.01)
        import time
        now=time.monotonic();f.received=now
        ctrl._send({'left':desired},now,now,f)
        np.testing.assert_array_equal(a.commanded,accepted)
        np.testing.assert_array_equal(a.velocity,speed)
