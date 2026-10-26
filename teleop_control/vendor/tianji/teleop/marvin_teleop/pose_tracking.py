"""Local pose continuation followed by the common joint motion envelope.

Each distinct hand target owns a frozen task metric and a finite local solve.
Posture preferences are part of the same objective as their gradients, and
fade to zero before pose-only tracking. Positional escape has one finite
continuation budget per clutch.
A held/unreachable target cannot continually restart a nullspace velocity field.
Each new hand target is seeded from the last accepted joint command, as in
test-teleop's incremental IK. At task singularities, continuously retain
the previous local continuation so tiny input noise cannot choose a different
redundant configuration. Elbow posture is a finite soft preference, never a
joint-sign limit; the final pose refinement retains the complete physical range.
"""
import numpy as np
from .differential import rotation_vector
from .constrained_ik import bounded_correction, DAMPING_SQ
from .motion import advance

POSITION_TOLERANCE_M = 9e-5
TARGET_ROTATION_TOLERANCE_RAD = 9e-4
MAX_ITERATIONS = 96
ITERATIONS_PER_TICK = 1
TRUST_STEP_RAD = .12
REFERENCE_WEIGHT = 1e-6
POSITIVE_ELBOW_WEIGHT = 1e-4
PREFERENCE_ITERATIONS = 32
POSITION_GUIDE_ACTIVATION_M = .002
ELBOW_PREFERRED_BEND_DEG = 20.


def _error(arm, target, q):
    pose = arm.fk(q)
    error = np.r_[target[:3, 3] - pose[:3, 3],
                  rotation_vector(target[:3, :3] @ pose[:3, :3].T)]
    return pose, error


def _cost(error, weight):
    weighted = error * weight
    return float(weighted @ weighted)


def _settings(quality):
    values = dict(rotation_weight_m=.35, singular_sigma_low=.02,
                  singular_sigma_high=.08, joint_limit_soft_margin_deg=0.,
                  joint_limit_reserve_deg=0., shoulder_motion_weight=1.)
    for key in values:
        values[key] = quality.get(key, values[key])
    values['singular_rotation_weight_m'] = quality.get(
        'singular_rotation_weight_m', values['rotation_weight_m'])
    if (not all(np.isfinite(v) for v in values.values())
            or not 0 < values['singular_rotation_weight_m'] <= values['rotation_weight_m']
            or not 0 < values['singular_sigma_low'] < values['singular_sigma_high']
            or not 1 <= values['shoulder_motion_weight'] <= 10
            or not 0 <= values['joint_limit_soft_margin_deg'] <= 45
            or not 0 <= values['joint_limit_reserve_deg'] < max(
                values['joint_limit_soft_margin_deg'], 1e-12)):
        raise ValueError('invalid local pose IK settings')
    return values


def _new_target(arm, target, seed, settings, previous=None, reference=None):
    current, error = _error(arm, target, seed)
    continuation = (previous['continuation'] if previous is not None else
                    dict(iterations=0, nominal=seed.copy(), extension=0.,
                         origin=current.copy(), active=False))
    turn_direction = rotation_vector(target[:3, :3] @ continuation['origin'][:3, :3].T)
    turning = np.linalg.norm(turn_direction) > TARGET_ROTATION_TOLERANCE_RAD
    raw = arm.world_jacobian(seed, current)
    sigma = np.linalg.svd(raw, compute_uv=False)
    low, high = settings['singular_sigma_low'], settings['singular_sigma_high']
    faces, bounds = arm.joint_halfspaces()
    slack = float(np.min(bounds - faces @ seed))
    margin = settings['joint_limit_soft_margin_deg']
    blend = min(float(np.clip((sigma[-1] - low) / (high - low), 0., 1.)),
                float(np.clip(slack / margin, 0., 1.)) if margin else 1.)
    normal = settings['rotation_weight_m']
    singular = settings['singular_rotation_weight_m']
    rw = singular + blend * (normal - singular)
    turn_response = 0.
    if turning:
        # Only accelerate angular directions that can be realized while
        # holding TCP position. A missing singular direction must not be
        # amplified into large shoulder/elbow motion.
        position = raw[:3]
        null = np.eye(7) - position.T @ np.linalg.solve(
            position @ position.T + 1e-10 * np.eye(3), position)
        gram = raw[3:] @ null @ raw[3:].T
        response = gram @ np.linalg.solve(gram + DAMPING_SQ * np.eye(3), turn_direction)
        turn_response = float(np.clip(
            2. * (turn_direction @ response) / (turn_direction @ turn_direction) - 1., 0., 1.))
        # Restore angular response for a reachable turn. If a combined target
        # is already outside the positional workspace, retain the stable
        # positional projection instead of rotating through a remote posture.
        reachable = float(np.clip(1. - np.linalg.norm(error[:3]) / .005, 0., 1.))
        reachable = reachable * reachable * (3. - 2. * reachable)
        rw += reachable * turn_response * (normal - rw)
    weight = np.array([1.] * 3 + [rw] * 3)
    # Shoulder/wrist rotational redundancy alone must not recenter the arm.
    # Only poor positional conditioning activates the elbow escape guide.
    position_sigma = np.linalg.svd(raw[:3], compute_uv=False)[-1]
    position_high = max(low + 1e-6, high / 2)
    extension = float(np.clip((position_high - position_sigma) / (position_high - low), 0., 1.))
    activity = min(1., (np.linalg.norm(error * weight) / .01) ** 2)
    metric = np.ones(7)
    metric[0] = settings['shoulder_motion_weight'] ** 2
    # At a singularity, a microscopic translation must not force a large wrist
    # reconfiguration merely to eliminate a tiny angular residual. Express that
    # tolerance in the same metre-equivalent metric used by the pose objective.
    rotation_tolerance = .0008 + max(0., 2e-4 / rw - .0008) * (1. - activity)
    # Keep one finite guide per clutch. Restarting a moving centre attraction
    # with each target created stick-slip and noise-sensitive joint drift.
    # Elbow bending helps leave a positional singularity but is unrelated to a
    # wrist turn. Activate it once, only after a real translation from the
    # clutch origin, including when the clutch initially held a zero-error pose.
    translation = np.linalg.norm(target[:3, 3] - continuation['origin'][:3, 3])
    if (not continuation['active'] and translation > POSITION_GUIDE_ACTIVATION_M
            and extension > 0.):
        nominal = seed.copy()
        nominal[3] = min(seed[3], arm.elbow_singularity_deg - ELBOW_PREFERRED_BEND_DEG)
        continuation.update(active=True, nominal=nominal, extension=extension)
    if turning:
        rotation_tolerance = TARGET_ROTATION_TOLERANCE_RAD
    previous_target = previous['target'] if previous is not None else current
    turn_in_place = turning and np.linalg.norm(
        target[:3, 3] - previous_target[:3, 3]) <= POSITION_TOLERANCE_M
    return dict(target=target.copy(), goal=seed.copy(),
                reference=(seed if reference is None else reference).copy(),
                nominal=continuation['nominal'], continuation=continuation, settings=settings,
                weight=weight, metric=metric,
                rotation_tolerance=rotation_tolerance, iterations=0,
                reference_weight=REFERENCE_WEIGHT * np.sqrt(activity),
                positive_elbow_weight=POSITIVE_ELBOW_WEIGHT * activity,
                turn_response=turn_response,
                damping_sq=DAMPING_SQ - turn_response * (DAMPING_SQ - .01**2)
                           if turn_in_place else DAMPING_SQ,
                posture_metric=np.array([0., 0., 0., 1., 0., 0., 0.]),
                posture_weight=(continuation['extension'] * 0.005 * np.sqrt(activity)
                                if translation > POSITION_GUIDE_ACTIVATION_M and not turn_in_place else 0.),
                settled=False, constrained=False, partial=True)


def _refine(arm, state, dt, speed):
    target, settings = state['target'], state['settings']
    for _ in range(min(state['iterations_per_tick'], MAX_ITERATIONS - state['iterations'])):
        state['iterations'] += 1
        goal = state['goal']
        current, error = _error(arm, target, goal)
        if (np.linalg.norm(error[:3]) < POSITION_TOLERANCE_M
                and np.linalg.norm(error[3:]) < state['rotation_tolerance']):
            state['settled'], state['partial'] = True, False
            break
        # A finite continuation removes the posture term before the final
        # pose solve. Both the gradient and line search use this same objective;
        # an inconsistent nullspace force used to stall and release in bursts.
        continuation = state['continuation']
        continuation['iterations'] += 1
        posture_weight = state['posture_weight'] * max(
            0., 1. - continuation['iterations'] / PREFERENCE_ITERATIONS) ** 2
        preference_fade = max(0., 1. - state['iterations'] / PREFERENCE_ITERATIONS) ** 2
        reference_weight = state['reference_weight'] * preference_fade
        positive_weight = state['positive_elbow_weight'] * preference_fade
        damping_sq = state['damping_sq']
        def objective(at, residual):
            displacement = np.radians(at-state['nominal'])
            reference_delta = np.radians(at-state['reference'])
            return (_cost(residual, state['weight'])
                    + posture_weight * float(displacement @ (state['posture_metric'] * displacement))
                    + reference_weight * float(reference_delta @ (state['metric'] * reference_delta))
                    + positive_weight * max(0., np.radians(at[3])) ** 2)
        cost = objective(goal, error)
        j = state['weight'][:, None] * arm.world_jacobian(goal, current)
        elbow_weight = posture_weight + (positive_weight if goal[3] > 0. else 0.)
        hessian = (j.T @ j + (damping_sq + reference_weight) * np.diag(state['metric'])
                   + elbow_weight * np.diag(state['posture_metric']))
        elbow_gradient = posture_weight * state['posture_metric'] * np.radians(state['nominal'] - goal)
        elbow_gradient[3] -= positive_weight * max(0., np.radians(goal[3]))
        correction = np.linalg.solve(hessian, j.T @ (error * state['weight'])
                       + elbow_gradient
                       + reference_weight * state['metric'] * np.radians(state['reference'] - goal))
        correction *= min(1., TRUST_STEP_RAD / max(1e-15, float(np.max(abs(correction)))))
        correction, constrained = bounded_correction(
            j, correction, arm, goal,
            soft_margin_deg=settings['joint_limit_soft_margin_deg'],
            reserve_deg=settings['joint_limit_reserve_deg'], dt=dt,
            max_outward_speed_deg_s=speed, joint_metric=state['metric'],
            damping_sq=damping_sq + reference_weight,
            extra_hessian=elbow_weight * np.diag(state['posture_metric']))
        state['constrained'] |= constrained
        accepted = False
        for scale in (1., .5, .25, .125, .0625, .03125, .015625):
            candidate = goal + np.degrees(correction) * scale
            if not arm.valid_joints(candidate):
                continue
            _, remaining = _error(arm, target, candidate)
            if objective(candidate, remaining) < cost - max(1e-16, cost * 1e-10):
                state['goal'], accepted = candidate, True
                break
        if not accepted:
            if posture_weight or reference_weight or positive_weight:
                continue
            state['settled'] = True
            break
    if state['iterations'] >= MAX_ITERATIONS:
        state['settled'] = True


def step(arm, target, reference, dt, speed, velocity, max_accel, **quality):
    q = np.asarray(reference, dtype=float)
    target = np.asarray(target, dtype=float)
    velocity = np.asarray(velocity, dtype=float)
    if (q.shape != (7,) or not arm.valid_joints(q) or target.shape != (4, 4)
            or not np.isfinite(target).all() or not 0 < dt <= .1
            or velocity.shape != (7,) or not np.isfinite(velocity).all()
            or not np.isfinite(speed) or speed <= 0
            or not np.isfinite(max_accel) or max_accel <= 0):
        raise ValueError('invalid continuous pose IK input')
    settings = _settings(quality)
    state = getattr(arm, '_pose_tracking', None)
    if state is not None and not any(np.max(abs(q - state[name])) < 1e-7
                                     for name in ('last_input', 'last_output')):
        state = None  # External repositioning starts a fresh local continuation.
    observed = state.get('observed_target') if state is not None else None
    # Telemetry is used on clutch engagement, then Controller commits q only
    # after SDK acceptance. A new target must start from that integrator, not
    # the previous optimizer goal, which can be far ahead during hand reversals.
    seed = q
    continuation_blend = 0.
    if state is not None:
        raw_jacobian = arm.world_jacobian(q, arm.fk(q))
        sigma = np.linalg.svd(raw_jacobian, compute_uv=False)[-1]
        low = settings['singular_sigma_low']
        high = settings['singular_sigma_high']
        continuation_blend = float(np.clip((high - sigma) / (high - low), 0., 1.))
        continuation_blend = continuation_blend**2 * (3. - 2. * continuation_blend)
        seed = q + continuation_blend * (state['goal'] - q)
    # Keep a smooth singular continuation while turning in one direction, but
    # discard an unsent rotational goal as soon as the hand reverses direction.
    turn_delta = None
    if state is not None:
        turn_delta = rotation_vector(target[:3, :3] @ state['target'][:3, :3].T)
        previous_delta = state.get('turn_delta')
        if (previous_delta is not None
                and turn_delta @ previous_delta < -TARGET_ROTATION_TOLERANCE_RAD ** 2):
            seed = q
    if (state is None or settings != state['settings']
            or np.linalg.norm(target[:3, 3] - state['target'][:3, 3]) > POSITION_TOLERANCE_M
            or np.linalg.norm(rotation_vector(target[:3, :3] @ state['target'][:3, :3].T))
            > TARGET_ROTATION_TOLERANCE_RAD):
        state = _new_target(arm, target, seed, settings, previous=state, reference=q)
        state['turn_delta'] = turn_delta
    # Numerical effort follows the same conditioning measure.
    # Joint velocity/acceleration remain governed by one common motion envelope.
    state['iterations_per_tick'] = ITERATIONS_PER_TICK + int(np.ceil(3 * continuation_blend))
    if not state['settled']:
        _refine(arm, state, dt, speed)
    goal_velocity = None
    if (observed is not None and state['turn_response'] > .5
            and np.linalg.norm(target[:3, 3] - observed[:3, 3]) <= POSITION_TOLERANCE_M):
        angular_delta = rotation_vector(target[:3, :3] @ observed[:3, :3].T)
        if np.linalg.norm(angular_delta) > 1e-7:
            # Feed forward observed hand rotation, never optimizer-goal jumps.
            # This minimum-norm twist keeps TCP position fixed and only changes
            # the moving-goal braking envelope, not joint acceleration limits.
            j = state['weight'][:, None] * arm.world_jacobian(q, arm.fk(q))
            twist = np.r_[np.zeros(3), angular_delta / dt] * state['weight']
            metric = state['metric']
            goal_velocity = np.degrees(np.linalg.solve(
                j.T @ j + state['damping_sq'] * np.diag(metric), j.T @ twist))
    result, arm.last_motion_constrained = advance(
        arm, q, state['goal'], velocity, dt, speed, max_accel, goal_velocity=goal_velocity)
    state['observed_target'] = target.copy()
    state['last_input'], state['last_output'] = q.copy(), result.copy()
    arm._pose_tracking = state
    current = arm.fk(q)
    singular = np.linalg.svd(arm.world_jacobian(q, current), compute_uv=False)
    faces, bounds = arm.joint_halfspaces()
    arm.last_pose_ik = True
    arm.last_ik_constrained = state['constrained'] or (state['settled'] and state['partial'])
    arm.last_limit_assist = False
    arm.last_joint_limit_margin_deg = float(np.min(bounds - faces @ q))
    arm.last_shoulder_motion_weight = settings['shoulder_motion_weight']
    arm.last_rotation_weight_m = float(state['weight'][3])
    arm.last_jacobian_sigma_min = float(singular[-1])
    arm.last_jacobian_condition = float(singular[0] / max(singular[-1], 1e-15))
    return result
