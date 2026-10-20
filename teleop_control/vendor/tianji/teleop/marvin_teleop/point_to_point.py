"""App manual pose/reset motion, shared by offline UMI entry planning."""
import math
import time
import numpy as np


def validate_pose(models, q, config, *, hard_limits=False, collision_guard=None):
    if any(not (models.arms[s].valid_hardware_joints(q[s]) if hard_limits
                else models.arms[s].valid_joints(q[s])) for s in q):
        raise ValueError('动作超出厂家关节限位')
    reason=(collision_guard.reason(q) if collision_guard is not None
            else models.collision_reason(q,config['collision_margin_m']))
    if reason:raise ValueError('路径存在干涉：'+reason)


def pose_plan(models, measured, goal, selected, config, *, acceleration=4.0,
              hard_limits=False, validate=True, speed_limit=10.0):
    start={s:np.asarray(q,float).copy() for s,q in measured.items()}
    goal={s:np.asarray(q,float).copy() for s,q in goal.items()}
    selected=tuple(selected)
    if set(start)!=set(models.arms) or set(goal)!=set(start):
        raise ValueError('动作必须包含双臂状态用于碰撞检查')
    if not selected or not set(selected)<=set(start):raise ValueError('无效的机械臂选择')
    for side in start:
        if goal[side].shape!=(7,) or not np.isfinite(goal[side]).all():raise ValueError('每臂必须为 7 个有效关节角度')
        if side not in selected and not np.array_equal(goal[side],start[side]):raise ValueError('未选中机械臂不得移动')
    delta={s:goal[s]-start[s] for s in goal}
    largest=max(float(np.max(abs(d))) for d in delta.values())
    samples=max(2,math.ceil(largest/.5)+1)
    if validate:
        for u in np.linspace(0,1,samples):
            q={s:start[s]+u*delta[s] for s in goal}
            validate_pose(models,q,config,hard_limits=hard_limits)
    speed=min(speed_limit,config['max_joint_speed_deg_s'])
    accel=min(acceleration,config['max_joint_accel_deg_s2'])
    duration=1.05*max(1.,1.875*largest/speed,math.sqrt(10/math.sqrt(3)*largest/accel))
    return dict(start=start,goal=goal,delta=delta,sides=selected,duration=duration,elapsed=0.,
                enabled_at=time.monotonic(),speed=speed,accel=accel,samples=samples)


def pose_at(plan,elapsed):
    u=float(np.clip(elapsed/plan['duration'],0,1));f=10*u**3-15*u**4+6*u**5
    return {s:plan['start'][s]+f*plan['delta'][s] for s in plan['goal']}
