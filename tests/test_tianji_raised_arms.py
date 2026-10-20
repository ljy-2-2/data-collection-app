"""Continuous VR-like lifts through extension, including unreachable hand poses."""
import numpy as np
import pytest
from teleop_control import tianji_worker
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models
from marvin_teleop.geometry import inverse,rotation


@pytest.fixture(scope='module')
def models():
    return Models(configuration('teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'))


@pytest.mark.parametrize('start,translation,turn', [
    ([0.]*7,[0.,0.,.7],0.),
    ([15.,-30.,5.,-65.,5.,15.,0.],[0.,.25,.65],1.5)])
def test_continuous_lift_does_not_stop_and_release_in_bursts(models,start,translation,turn):
    l,r=models.arms['left'],models.arms['right'];mirror=l.tracking_mirror
    q={'right':np.array(start),'left':mirror.signs*np.array(start)}
    v={s:np.zeros(7) for s in q};start_pose=r.fk(q['right']);positions=[];trace=[];mirror_errors=[]
    for arm in (l,r):arm.reset_pose_tracking()
    for tick in range(1700):
        amount=np.clip((tick-100)/1000.,0.,1.)
        target=start_pose.copy();target[:3,3]+=np.array(translation)*amount
        target[:3,:3]=rotation([1,0,0],turn*amount)@start_pose[:3,:3]
        for side,arm in (('right',r),('left',l)):
            mapped=target if side=='right' else inverse(mirror.world)@target@mirror.tcp
            n=arm.pose_tracking_ik(mapped,q[side],.01,180.,velocity=v[side],max_accel=450.,
                singular_rotation_weight_m=.05,joint_limit_soft_margin_deg=30.,joint_limit_reserve_deg=3.)
            nv=(n-q[side])/.01
            assert arm.valid_joints(n)
            assert np.max(abs(nv))<=180.+1e-7
            assert np.max(abs(nv-v[side]))<=4.5+1e-7
            q[side],v[side]=n,nv
        mirror_errors.append(float(np.max(abs(q['left']-mirror.signs*q['right']))))
        positions.append(r.fk(q['right'])[:3,3]);trace.append(q['right'].copy())
    assert max(mirror_errors)<1e-6
    speed=np.linalg.norm(np.diff(positions,axis=0),axis=1)/.01
    # The old solver stopped for 357/467 ticks, then released at .80-.85 m/s
    # despite a steady .07 m/s hand input. A projected reachable component
    # must continue smoothly even when the full hand pose is out of reach.
    assert np.count_nonzero(speed[200:1000]<.001)<5
    assert np.max(speed[200:1100])<.25
    assert np.percentile(abs(np.diff(speed[200:1000])),95)<.02
    assert np.max(np.ptp(np.array(trace)[1500:],axis=0))<.01
