"""Strict offline IK, Hermite interpolation, limits and sampled collision checks."""
import math
import time
import numpy as np
from umi_data import relative_target, gripper_trigger


def joint_diagnostics(arm, q, margin=.2):
    q = np.asarray(q, float)
    if q.shape != (7,) or not np.isfinite(q).all():
        return dict(issues=['逆解关节角无效'])
    issues, limits = [], []
    for i, value in enumerate(q):
        lo, hi = float(arm.lower[i]+margin), float(arm.upper[i]-margin)
        limits.append([lo, hi])
        if not lo <= value <= hi:
            issues.append(f'J{i+1}={value:.4f}° 超出允许范围 [{lo:.4f}, {hi:.4f}]°')
    j6, j7 = q[5:]
    quadrant = 0 if j6 >= 0 and j7 >= 0 else 1 if j6 < 0 <= j7 else 2 if j6 < 0 and j7 < 0 else 3
    a, b, c = arm.bd[quadrant]
    boundary = float(a*j6*j6+b*j6+c + (-margin if j7 >= 0 else margin))
    allowed = j7 <= boundary if j7 >= 0 else j7 >= boundary
    relation = '<=' if j7 >= 0 else '>='
    if not allowed:
        issues.append(f'腕部联动限位：J6={j6:.4f}° 时 J7 需 {relation} {boundary:.4f}°，逆解 J7={j7:.4f}°')
    return dict(candidate_joints_deg=q.tolist(), allowed_joint_ranges_deg=limits,
                wrist=dict(j6_deg=float(j6), j7_deg=float(j7), relation=relation,
                           boundary_deg=boundary, valid=bool(allowed)), issues=issues)


class PlanFailure(ValueError):
    def __init__(self, message, diagnostic):
        super().__init__(message)
        self.diagnostic = diagnostic


def continuous_interval(solve, begin, end, reference, depth=0):
    """Refine both arms on one time grid; never raise the 5deg continuity bound."""
    solution = solve(end, reference)
    deltas = {s: solution[s]-reference[s] for s in reference}
    side = max(deltas, key=lambda s: float(np.max(abs(deltas[s]))))
    joint = int(np.argmax(abs(deltas[side])))
    largest = float(abs(deltas[side][joint]))
    if largest <= 5:
        return [(end, solution)]
    if depth >= 7 or end-begin <= .0002:
        detail = dict(side=side, replay_source_time_s=float(end), interval_start_s=float(begin),
                      interval_duration_s=float(end-begin), joint=joint+1, max_delta_deg=largest,
                      previous_valid_joints_deg=reference[side].tolist(),
                      candidate_joints_deg=solution[side].tolist(), delta_joints_deg=deltas[side].tolist(),
                      refinement_depth=depth)
        raise PlanFailure(f'{side} t={end:.6f}s: 加密后 J{joint+1} 仍变化 {largest:.4f}° >5°；'
                          '无法确认连续逆解，拒绝跨越该区间。', detail)
    middle = (begin+end)/2
    left = continuous_interval(solve, begin, middle, reference, depth+1)
    right = continuous_interval(solve, middle, end, left[-1][1], depth+1)
    return left+right


class Curve:
    """Shape-preserving cubic Hermite curve, with zero endpoint velocities."""
    def __init__(self, t, y):
        self.t, self.y = np.asarray(t, float), np.asarray(y, float)
        h = np.diff(self.t)[:, None]
        d = np.diff(self.y, axis=0)/h
        m = np.zeros_like(self.y)
        same = d[:-1]*d[1:] > 0
        w1, w2 = 2*h[1:]+h[:-1], h[1:]+2*h[:-1]
        with np.errstate(divide='ignore', invalid='ignore'):
            harmonic = (w1+w2)/(w1/d[:-1]+w2/d[1:])
        m[1:-1] = np.where(same, harmonic, 0)
        delta = np.diff(self.y, axis=0)
        self.a = -2*delta+h*(m[:-1]+m[1:])
        self.b = 3*delta-h*(2*m[:-1]+m[1:])
        self.c = h*m[:-1]
        self.h = h
        self.vmax, self.amax = self.bounds()

    def at(self, t):
        i = int(np.clip(np.searchsorted(self.t, t, side='right')-1, 0, len(self.t)-2))
        u = np.clip((t-self.t[i])/self.h[i, 0], 0, 1)
        return ((self.a[i]*u+self.b[i])*u+self.c[i])*u+self.y[i]

    def bounds(self):
        a, b, c, h = self.a, self.b, self.c, self.h
        with np.errstate(divide='ignore', invalid='ignore'):
            u = np.clip(-b/(3*a), 0, 1)
        u = np.where(np.isfinite(u), u, 0)
        v = np.maximum.reduce([abs(c/h), abs((3*a+2*b+c)/h), abs((3*a*u*u+2*b*u+c)/h)])
        acc = np.maximum(abs(2*b/h**2), abs((6*a+2*b)/h**2))
        return v, acc


def rotation_angle(R):
    return math.acos(float(np.clip((np.trace(R)-1)/2, -1, 1)))


def basis(first, anchor, alignment, rotation):
    if alignment == 'initial-tool':
        return anchor[:3, :3] @ first[:3, :3].T
    C = np.asarray(rotation, float)
    if C.shape != (3, 3) or not np.isfinite(C).all() or not np.allclose(C.T@C, np.eye(3), atol=1e-6) or not np.isclose(np.linalg.det(C), 1, atol=1e-6):
        raise ValueError('World rotation must be a proper 3x3 rotation, no scaling/reflection')
    return C


def make_plan(data, models, initial, cfg, options, motors, endpoints, trace=None):
    sides = data.sides
    position_only = getattr(options, 'position_only', False)
    if position_only:
        from numerical_ik import solver_models
        models = solver_models(models, getattr(options, 'ik_solver', 'hybrid'), True)
    target_provider = getattr(options, "target_provider", None)
    scale = getattr(options, "scale", 1.0)
    if not math.isfinite(scale) or not 0 < scale <= 1:
        raise ValueError("位移比例必须在 (0, 1] 内")
    start, end = options.start, data.duration if options.end is None else options.end
    if not 0 <= start < end <= data.duration:
        raise ValueError('Requested replay segment is outside shared data range')
    # Offline checks only: do not import the control loop or robot transport.
    for s in sides:
        arm=models.arms[s]
        if not arm.valid_joints(initial[s]):raise ValueError(f'{s}: initial joint/wrist limit')
        if abs(initial[s][3]-arm.elbow_singularity_deg)<.1:raise ValueError(f'{s}: initial elbow singularity')
    reason=models.collision_reason(initial,cfg['collision_margin_m'])
    if reason:raise ValueError('Initial pose: '+reason)
    first = {s: data.at(s, start)[0] for s in sides}
    anchors = {s: models.arms[s].fk(initial[s]) for s in sides}
    C = {s: basis(first[s], anchors[s], options.alignment, options.rotation) for s in sides}
    # Raw streams may carry almost identical left/right timestamps. Do not make
    # those separate control knots: tiny IK tolerances divided by nanoseconds
    # produce meaningless velocity/acceleration bounds. Plan on a regular grid,
    # then verify the resulting full pose at EVERY original timestamp below.
    raw=[data.pt[s][(data.pt[s]>=start)&(data.pt[s]<=end)]-start for s in sides]
    raw_t=np.unique(np.concatenate(raw))
    coarse_t = np.linspace(0,end-start,math.ceil((end-start)*100)+1)
    t = [0.]
    joints = {s: [initial[s].copy()] for s in sides}
    # Optional offline viewer trace. Copies only; never changes planning decisions.
    if trace is not None:
        trace.append((0., {s: initial[s].copy() for s in sides}))
    triggers = {s: [] for s in motors}
    clipping = {s: 0 for s in motors}
    max_move = {s: 0.0 for s in sides}
    last_progress = time.monotonic()
    def solve(elapsed, reference):
        solved = {}
        for s in sides:
            source, _ = data.at(s, start+elapsed)
            target = target_provider(s, start+elapsed) if target_provider else relative_target(first[s], source, anchors[s], C[s], scale)
            displacement = np.linalg.norm(target[:3, 3]-anchors[s][:3, 3])
            max_move[s] = max(max_move[s], float(displacement))
            if displacement > cfg.get('max_displacement_m',math.inf):
                raise ValueError(f'{s} t={elapsed:.3f}s displacement {displacement:.3f}m exceeds App limit; no clipping allowed')
            arm = models.arms[s]
            try:
                solution = arm.ik(target, reference[s])
                initial_distance = initial[s][3]-arm.elbow_singularity_deg
                branch_sign = 1 if initial_distance >= 0 else -1
                if branch_sign*(solution[3]-arm.elbow_singularity_deg) < min(.1, abs(initial_distance)):
                    raise ValueError(f'{s}: IK crosses the initial elbow branch or its 0.1deg singular reserve')
            except ValueError as exc:
                detail = dict(side=s, replay_source_time_s=float(elapsed),
                              recording_time_s=float(start+elapsed), target_pose=target.tolist(),
                              previous_valid_joints_deg=reference[s].tolist(),
                              vendor_flags=getattr(arm, 'last_vendor_flags', {}),
                              accepted_ik_knots=len(t), refined_ik_knots=max(0, len(t)-int(np.searchsorted(coarse_t,t[-1]))-1))
                # Read failed vendor solve only as a diagnostic, never a command.
                if str(exc).endswith('joint or coupled wrist limit'):
                    try:
                        detail.update(joint_diagnostics(arm, arm.kine.sp.m_Output_RetJoint.to_list()))
                    except (AttributeError, TypeError, ValueError):
                        detail['diagnostic_note'] = '无法读取失败逆解的关节角'
                why = '；'.join(detail.get('issues', []))
                flags = detail['vendor_flags']
                raise PlanFailure(f'{s} replay t={elapsed:.3f}s: {exc}'
                                  + (f'；{why}' if why else '') + f'；厂家标志={flags}'
                                  + '；这是轨迹几何/限位问题，降低速度不会消除。', detail) from exc
            solved[s] = solution
        return solved

    for i, elapsed in enumerate(coarse_t[1:], 1):
        added = continuous_interval(solve, t[-1], elapsed, {s: joints[s][-1] for s in sides})
        for stamp, solved in added:
            t.append(stamp)
            for s in sides:
                joints[s].append(solved[s])
            if trace is not None:
                trace.append((float(stamp), {s: solved[s].copy() for s in sides}))
        if len(added) > 1:
            print(f'连续性检查：t={elapsed:.3f}s 区间加密为 {len(added)} 段；保留原位姿目标和 5° 单步限制。', flush=True)
        if time.monotonic()-last_progress > 5:
            print(f'IK 预检 {i+1}/{len(coarse_t)}', flush=True)
            last_progress = time.monotonic()
    t = np.asarray(t)
    for elapsed in t:
        for s in motors:
            _, q = data.at(s, start+elapsed)
            closed, opened = endpoints[s]
            raw_fraction = (q-closed)/(opened-closed)
            clipping[s] += int(not 0 <= raw_fraction <= 1)
            triggers[s].append(gripper_trigger(q, closed, opened))
    curves = {s: Curve(t, joints[s]) for s in sides}
    gc = {s: Curve(t, np.asarray(triggers[s])[:, None]) for s in motors}
    stretch = 1/options.speed
    for s, curve in curves.items():
        arm = models.arms[s]
        vmax = np.minimum(arm.velocity_limits, min(options.joint_speed, cfg['max_joint_speed_deg_s']))
        amax = np.minimum(arm.acceleration_limits, min(options.joint_accel, cfg['max_joint_accel_deg_s2']))
        stretch = max(stretch, float(np.max(curve.vmax/vmax))*1.05,
                      math.sqrt(float(np.max(curve.amax/amax)))*1.05)
    for s, curve in gc.items():
        from gripper.config import travel_bounds
        lo, hi = travel_bounds(motors[s])
        span = hi-lo
        stretch = max(stretch, float(np.max(curve.vmax))*span/motors[s]['speed_rad_s']*1.05,
                      math.sqrt(float(np.max(curve.amax))*span/motors[s]['accel_rad_s2'])*1.05)
    # Validate interpolation, not just IK knots. Samples <=0.25deg apart and
    # <=10ms of source time. These checks do NOT model the surrounding workbench.
    count, max_p, max_r = 0, 0., 0.
    for i in range(len(t)-1):
        dt = t[i+1]-t[i]
        maximum = max(float(np.max(c.vmax[i])) for c in curves.values())
        n = max(2, math.ceil(maximum*dt/.25),math.ceil(dt/.01))
        lo=np.searchsorted(raw_t,t[i],side='left');hi=np.searchsorted(raw_t,t[i+1],side='right')
        validation_t=np.unique(np.r_[np.linspace(t[i],t[i+1],n+1),raw_t[lo:hi]])
        for elapsed in validation_t:
            q = {s: initial[s].copy() for s in initial}
            for s, curve in curves.items():
                q[s] = curve.at(elapsed)
                if not models.arms[s].valid_joints(q[s]):
                    raise ValueError(f'{s}: interpolated path violates joint limits')
                desired = target_provider(s, start+elapsed) if target_provider else relative_target(first[s], data.at(s, start+elapsed)[0], anchors[s], C[s], scale)
                actual = models.arms[s].fk(q[s])
                if target_provider and np.linalg.svd(models.arms[s].world_jacobian(q[s],actual)[:3 if position_only else 6],compute_uv=False)[-1]<1e-5:
                    raise ValueError(f'{s}: origin replay interpolation approaches Jacobian singularity at {elapsed:.3f}s')
                ep = np.linalg.norm(actual[:3, 3]-desired[:3, 3])
                er = rotation_angle(actual[:3, :3]@desired[:3, :3].T)
                max_p, max_r = max(max_p, float(ep)), max(max_r, float(er))
                if ep > .002 or (not position_only and er > math.radians(.5)):
                    tolerance = '2mm position' if position_only else '2mm/0.5deg'
                    raise ValueError(f'{s}: interpolation error >{tolerance} at {elapsed:.3f}s')
            reason = models.collision_reason(q, cfg['collision_margin_m'])
            if reason:
                raise ValueError(f't={elapsed:.3f}s collision precheck: {reason}')
            count += 1
        if time.monotonic()-last_progress > 5:
            print(f'插值/碰撞预检 {i+1}/{len(t)-1}', flush=True)
            last_progress = time.monotonic()
    report = dict(position_only=position_only, orientation_enforced=not position_only,
                  tracking_mode='position-only' if position_only else 'full-pose',
                  rotation_error_role='unconstrained diagnostic' if position_only else 'enforced',
                  initial_joints_deg={s: v.tolist() for s, v in initial.items()},
                  robot_tcp={s: models.arms[s].control_point_name for s in sides},
                  alignment=options.alignment, rotation_umi_to_robot={s: v.tolist() for s, v in C.items()},
                  umi_point=data.point, source_start_s=start, source_duration_s=end-start,
                  planned_duration_s=(end-start)*stretch, actual_speed_ratio=1/stretch,
                  scale=scale, max_displacement_m=max_move,
                  ik_knots=len(t), added_refinement_knots=len(t)-len(coarse_t),
                  raw_pose_timestamps_checked=len(raw_t),control_knot_rate_hz=100,
                  max_interpolation_position_error_m=max_p, max_interpolation_rotation_error_deg=math.degrees(max_r),
                  collision_samples=count, gripper_clipped_samples=clipping,
                  umi_gripper_endpoints_rad={s: dict(closed=v[0], opened=v[1]) for s, v in endpoints.items()})
    return dict(curves=curves, grippers=gc, stretch=stretch, duration=(end-start)*stretch,
                initial=initial, report=report)


def plan_at(plan, elapsed):
    t = elapsed/plan['stretch']
    return ({s: c.at(t) for s, c in plan['curves'].items()},
            {s: float(np.clip(c.at(t)[0], 0, 1)) for s, c in plan['grippers'].items()})
