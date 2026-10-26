"""Offline full-pose replay, with real kinematics and no transport imports."""
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/umi_replay_sim'))
from simulate import load_initial,offline_guard
from umi_data import relative_target,pose,slerp
from replay_plan import basis,make_plan,Curve
from numerical_ik import solver_models
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models
from marvin_teleop.geometry import transform,rotation


def test_full_relative_pose_is_exact_se3_alignment():
    first=transform([.2,.4,-.3],[.3,-.4,.8])
    current=transform([.25,.38,-.25],[.6,-.2,1.])
    anchor=transform([.4,-.2,.6],[.8,.3,-.9])
    C=basis(first,anchor,'initial-tool',None)
    np.testing.assert_allclose(relative_target(first,first,anchor,C),anchor,atol=1e-12)
    target=relative_target(first,current,anchor,C)
    np.testing.assert_allclose(target,anchor@np.linalg.inv(first)@current,atol=1e-12)
    assert not np.allclose(target[:3,:3],anchor[:3,:3])
    assert np.linalg.norm(target[:3,3]-anchor[:3,3])==pytest.approx(np.linalg.norm(current[:3,3]-first[:3,3]))


def test_quaternion_sign_and_invalid_initial(tmp_path):
    np.testing.assert_allclose(slerp([0,0,0,1],[0,0,0,-1],.5),[0,0,0,1])
    path=tmp_path/'initial.json';path.write_text('{"robot":"tianji","units":"rad"}')
    with pytest.raises(ValueError,match='units'):load_initial(path)
    with pytest.raises(ValueError,match='quaternion'):pose([0,0,0],[0,0,0,0])


@pytest.mark.parametrize('event,args',[('socket.connect',()),('socket.bind',()),('subprocess.Popen',()),('os.system',()),('ctypes.dlopen',('/tmp/librobot.so',))])
def test_simulation_rejects_hardware_paths(event,args):
    with pytest.raises(RuntimeError):offline_guard(event,args)


@pytest.fixture(scope='module')
def models():return Models(configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'))


def case(models):
    right=np.array([20.,-35,15,-65,5,15,-10])
    initial={'right':right,'left':right*np.array([-1,1,-1,1,-1,1,-1])}
    class Data:
        sides=('left','right');point='synthetic-known-TCP';duration=.2
        pt={s:np.array([0.,.017,.073,.141,.2]) for s in sides}
        def at(self,side,t):
            q=initial[side].copy();q[0]+=t*2
            return np.linalg.inv(models.arms[side].fk(initial[side]))@models.arms[side].fk(q),0.
    options=SimpleNamespace(start=0.,end=None,alignment='initial-tool',rotation=None,scale=1.,speed=1.,joint_speed=30.,joint_accel=60.,position_only=False)
    cfg=configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json')
    return Data(),initial,options,cfg


def test_real_robot_model_accepts_known_full_pose_path_and_checks_timing(models):
    data,initial,options,cfg=case(models)
    plan=make_plan(data,solver_models(models,'hybrid'),initial,cfg,options,{}, {})
    assert plan['report']['orientation_enforced'] and not plan['report']['position_only']
    assert plan['report']['max_interpolation_position_error_m']<.002
    assert plan['report']['max_interpolation_rotation_error_deg']<.5
    for curve in plan['curves'].values():
        assert np.max(curve.vmax)/plan['stretch']<=options.joint_speed
        assert np.max(curve.amax)/plan['stretch']**2<=options.joint_accel
    assert plan['report']['raw_pose_timestamps_checked']==len(data.pt['left'])


def test_near_duplicate_source_stamps_do_not_create_extreme_time_stretch(models):
    data,initial,options,cfg=case(models)
    data.pt={s:np.array([0.,.02,.020000001,.073,.141,.2]) for s in data.sides}
    plan=make_plan(data,solver_models(models,'hybrid'),initial,cfg,options,{}, {})
    assert plan['stretch']<10
    assert plan['report']['raw_pose_timestamps_checked']==6


def test_off_grid_recorded_position_is_still_validated(models):
    data,initial,options,cfg=case(models)
    def at(side,t):
        T=np.eye(4)
        if abs(t-.017)<1e-12:T[0,3]=.004
        return T,0.
    data.at=at
    with pytest.raises(ValueError,match='interpolation error'):
        make_plan(data,solver_models(models,'hybrid'),initial,cfg,options,{}, {})


def test_orientation_error_is_rejected_even_when_position_is_exact(models):
    data,initial,options,cfg=case(models)
    def at(side,t):
        T=np.eye(4);T[:3,:3]=rotation([1,0,0],t)
        return T,0.
    data.at=at
    class StuckArm:
        def __init__(self,side):self.side=side;self.arm=models.arms[side]
        def __getattr__(self,name):return getattr(self.arm,name)
        def ik(self,target,reference):return initial[self.side].copy()
    stuck=SimpleNamespace(arms={s:StuckArm(s) for s in data.sides},collision_reason=models.collision_reason)
    with pytest.raises(ValueError,match='interpolation error'):
        make_plan(data,stuck,initial,cfg,options,{}, {})


def test_curve_continuous_bounds_cover_dense_sampling():
    curve=Curve([0,.1,.3,.5],[[0,2],[1,-1],[-.5,3],[2,0]])
    t=np.linspace(0,.5,20001);q=np.array([curve.at(v) for v in t])
    dt=t[1]-t[0]
    assert np.max(abs(np.diff(q,axis=0)/dt))<=np.max(curve.vmax)+1e-6
    assert np.max(abs(np.diff(q,n=2,axis=0)/dt**2))<=np.max(curve.amax)+1e-4


def test_viewer_failed_prefix_holds_robot_but_shows_full_target(tmp_path):
    import json
    from PySide6.QtCore import QCoreApplication
    from view import Replay
    app=QCoreApplication.instance() or QCoreApplication([])
    initial={s:[0.]*7 for s in ('left','right')}
    (tmp_path/'run.json').write_text(json.dumps(dict(initial_pose=dict(joints=initial,measured=False),display_from_world=np.eye(4).tolist())))
    rows=[dict(row=4,folder='first',status='failed',error='unreachable',preview='preview.npz'),
          dict(row=5,folder='second',status='failed',error='invalid data')]
    (tmp_path/'summary.json').write_text(json.dumps(dict(episodes=rows)))
    (tmp_path/'row-004').mkdir()
    target=np.tile(np.eye(4),(2,2,1,1));target[1,:,0,3]=.1
    np.savez(tmp_path/'row-004/preview.npz',source_time_s=[0.,.1],joints_deg=np.array([np.zeros((2,7)),np.ones((2,7))]),target_time_s=[0.,1.],target_tcp=target)
    class Scene:
        def update_pose(self,status):self.status=status
    scene=Scene();viewer=Replay(tmp_path,scene)
    viewer.seek(.8)
    assert scene.status['joints']['left']==[1.]*7
    assert viewer.targetFrames[0]['position'].x()==pytest.approx(.1)
    assert not viewer.state['complete']
    viewer.toggle();assert viewer.state['playing']
    viewer.select(1)
    assert not viewer.state['playing'] and viewer.state['duration']==0
    assert scene.status['joints']==initial
