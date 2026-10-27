"""Noisy held hands must not generate repeated servo reversals; no hardware."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from teleop_control import tianji_worker
from marvin_teleop.configuration import configuration
from marvin_teleop.control import Controller
from marvin_teleop.geometry import rotation, angle
from marvin_teleop.hand_stability import HandStability
from marvin_teleop.model import Models

ROOT=Path(__file__).resolve().parents[1]
CASES=json.loads((ROOT/'tests/fixtures/tianji_held_target_oscillation.json').read_text())


def test_cached_frames_do_not_change_filter_or_extend_settle_window():
    f=HandStability(np.eye(4),0.)
    moved=np.eye(4);moved[0,3]=.01
    expected=f.update(moved,.01)
    for _ in range(100):np.testing.assert_array_equal(f.update(moved,.01),expected)
    assert len(f.history)==2


@pytest.mark.parametrize('translation', [True,False])
def test_deliberate_slow_motion_accumulates_beyond_fixed_hold_anchor(translation):
    f=HandStability(np.eye(4),0.)
    for i in range(1,401):
        pose=np.eye(4)
        if translation:pose[0,3]=i*.00002  # 2 mm/s, smaller than the band per frame.
        else:pose[:3,:3]=rotation([0,0,1],i*.0001)
        target=f.update(pose,i*.01)
    if translation:assert abs(target[0,3]-.008)<.001
    else:assert angle(target[:3,:3]@pose[:3,:3].T)<np.radians(.25)


def test_moving_rotation_does_not_unlock_stationary_position_noise():
    f=HandStability(np.eye(4),0.);rng=np.random.default_rng(2026)
    for i in range(1,200):
        pose=np.eye(4);pose[:3,3]=rng.normal(0,.0003,3)
        pose[:3,:3]=rotation([0,0,1],i*.005)
        target=f.update(pose,i*.01)
        np.testing.assert_array_equal(target[:3,3],np.zeros(3))
    assert not f.rotation_held and f.position_held
    assert angle(target[:3,:3]@pose[:3,:3].T)<.02


@pytest.fixture(scope='module')
def models():return Models(configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'))


@pytest.mark.parametrize('case',CASES,ids=lambda c:str(c['case']))
def test_actual_dual_arm_controller_holds_noisy_stationary_hands(models,case,monkeypatch):
    config=configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json')
    q={'right':np.array(case['q']), 'left':models.arms['left'].tracking_mirror.signs*np.array(case['q'])}
    initial={s:v.copy() for s,v in q.items()}
    # The test isolates the input/controller path; it never constructs Robot.
    # Collision geometry and physical controls are covered by their own tests.
    monkeypatch.setattr(models,'collision_reason',lambda *a:None)
    def command(targets,**kwargs):
        for s,v in targets.items():q[s]=v.copy()
    robot=SimpleNamespace(enabled=True,command=command)
    ctrl=Controller(models,robot,config)
    frame=SimpleNamespace(head=np.eye(4),hands={s:np.eye(4) for s in q},sequence=0,source_time=0.,received=0.)
    for s in q:ctrl._activate(s,frame,q)
    rng=np.random.default_rng(42);trace=[]
    for i in range(1,401):
        now=time.monotonic();frame.received=now;ctrl.last_tick=now
        frame.source_time=i*.01;frame.sequence=i
        for s in q:
            hand=np.eye(4);hand[:3,3]=rng.normal(0,.0003,3)
            hand[:3,:3]=rotation([0,0,1],rng.normal(0,.0015))
            frame.hands[s]=hand
        ctrl.measured={s:v.copy() for s,v in q.items()}
        targets={s:ctrl._target(s,frame,q,.01) for s in q}
        ctrl._send(targets,time.monotonic(),now,frame)
        trace.append(np.concatenate([q['left'],q['right']]))
    assert np.max(np.ptp(np.asarray(trace)[200:],axis=0))<1e-8
    assert np.max(np.abs(np.asarray(trace)[-1]-np.r_[initial['left'],initial['right']]))<.01
    assert all(s['hand_position_held'] and s['hand_rotation_held'] for s in ctrl.status().values())
    # A real large discontinuity remains rejected before input filtering.
    frame.hands['right'][0,3]+=.2
    with pytest.raises(ValueError,match='跳变'):ctrl._target('right',frame,q,.01)


def test_motion_then_stop_freezes_target_and_resumes_without_reclutch():
    f=HandStability(np.eye(4),0.);rng=np.random.default_rng(7);tail=[]
    for i in range(1,501):
        pose=np.eye(4);pose[0,3]=.04*min(i/100,1)
        pose[:3,3]+=rng.normal(0,.0003,3)
        pose[:3,:3]=rotation([0,1,0],.1*min(i/100,1)+rng.normal(0,.0015))
        result=f.update(pose,i*.01)
        if i>350:tail.append(result)
    assert max(np.max(abs(p-tail[0])) for p in tail)==0
    assert f.position_held and f.rotation_held
    pose[0,3]+=.01
    for i in range(501,511):result=f.update(pose,i*.01)
    assert result[0,3]>.048
