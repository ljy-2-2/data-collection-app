"""Robot-independent bounded differential IK; no model or hardware calls.

First maximize independent task progress without changing each task's direction.
Then minimize weighted joint motion at that progress. Posture preference cannot
buy a smaller cost by moving a hand away from its requested twist.
"""
import numpy as np
from scipy.optimize import linprog, minimize


class TaskStepError(ValueError):
    """No verified bounded task step is available."""


def bounded_task_step(jacobians, errors, lower, upper, metric, posture_step,
                      *, priorities=None, inequality=None):
    lower, upper = np.asarray(lower), np.asarray(upper)
    metric, posture_step = np.asarray(metric), np.asarray(posture_step)
    n, tasks = len(lower), len(jacobians)
    if (not tasks or upper.shape != (n,) or metric.shape != (n,)
            or posture_step.shape != (n,) or len(errors) != tasks
            or np.any(lower > upper) or np.any(metric <= 0)):
        raise TaskStepError('invalid bounded task dimensions or limits')
    j = np.vstack(jacobians)
    e = np.zeros((j.shape[0], tasks))
    offset = 0
    for index, (jacobian, error) in enumerate(zip(jacobians, errors)):
        rows = len(error)
        if np.shape(jacobian) != (rows, n):
            raise TaskStepError('invalid task Jacobian shape')
        e[offset:offset+rows, index] = error
        offset += rows
    priority = np.ones(tasks) if priorities is None else np.asarray(priorities)
    if priority.shape != (tasks,) or np.any(priority <= 0):
        raise TaskStepError('invalid task priorities')
    if not all(np.all(np.isfinite(a)) for a in (j, e, lower, upper, metric, posture_step, priority)):
        raise TaskStepError('non-finite bounded task input')
    # Unit-box variables avoid numerical tolerances dominating milliradian steps.
    unit = np.maximum(np.maximum(abs(lower), abs(upper)), 1e-9)
    equality = np.column_stack((j * unit, -e))
    row_scale = np.maximum(np.linalg.norm(equality, axis=1), 1e-9)
    equality /= row_scale[:, None]
    bounds = list(zip(lower / unit, upper / unit))
    a_ub = b_ub = None
    if inequality is not None:
        a, b_ub = (np.asarray(v) for v in inequality)
        if a.ndim != 2 or a.shape[1] != n or b_ub.shape != (a.shape[0],):
            raise TaskStepError('invalid task inequality dimensions')
        if not np.all(np.isfinite(a)) or not np.all(np.isfinite(b_ub)):
            raise TaskStepError('non-finite task inequality')
        a_ub = np.column_stack((a * unit, np.zeros((len(b_ub), tasks))))
    progress = linprog(np.r_[np.zeros(n), -priority], A_eq=equality,
        b_eq=np.zeros(len(j)), A_ub=a_ub, b_ub=b_ub,
        bounds=bounds + [(0., 1.)] * tasks, method='highs',
        options={'primal_feasibility_tolerance':1e-9, 'dual_feasibility_tolerance':1e-9})
    if not progress.success:
        raise TaskStepError(f'bounded task progress unavailable: {progress.message}')

    if np.max(abs(e), initial=0.) < 1e-10 and np.all(lower <= 0) and np.all(upper >= 0):
        if b_ub is None or np.all(b_ub >= 0):
            return np.zeros(n), (1.,) * tasks
    # Leave a small feasible interior for posture optimization instead of forcing
    # it to an LP vertex where several speed bounds coincide. Never use this when
    # zero is infeasible (that case belongs to the caller's recovery controller).
    if np.all(lower <= 0) and np.all(upper >= 0) and (b_ub is None or np.all(b_ub >= -1e-12)):
        progress.x *= .98

    eq, rhs = equality[:, :n], -equality[:, n:] @ progress.x[n:]
    # Remove dependent rows before SLSQP (e.g. a singular/unreachable task).
    u, singular, vt = np.linalg.svd(eq, full_matrices=True)
    rank = int(np.sum(singular > max(float(singular[0]), 1.) * 1e-10)) if len(singular) else 0
    reduced = vt[:rank]
    reduced_rhs = (u[:, :rank].T @ rhs) / singular[:rank]
    weights = metric * unit**2 / float(max(unit)**2)
    weights /= max(weights)
    reference = posture_step / unit
    # Optimize only in the nullspace: task equalities cannot be traded away and
    # SLSQP need not rediscover a poorly conditioned equality-constrained face.
    origin = progress.x[:n] + reduced.T @ (reduced_rhs - reduced @ progress.x[:n])
    nullspace = vt[rank:].T
    candidate = origin
    if nullspace.shape[1]:
        c = np.vstack((nullspace, -nullspace))
        bound = np.r_[upper/unit-origin, origin-lower/unit]
        if a_ub is not None:
            c = np.vstack((c, a_ub[:, :n] @ nullspace))
            bound = np.r_[bound, b_ub-a_ub[:, :n] @ origin]
        scale = np.maximum(np.linalg.norm(c, axis=1), 1e-9)
        c, bound = c/scale[:, None], bound/scale
        hessian = nullspace.T @ (weights[:, None]*nullspace)
        gradient = nullspace.T @ (weights*(origin-reference))
        posture = minimize(lambda z: .5*z @ hessian @ z + gradient @ z,
            np.zeros(nullspace.shape[1]), jac=lambda z: hessian @ z + gradient,
            constraints={'type':'ineq', 'fun':lambda z: bound-c @ z, 'jac':lambda z: -c},
            method='SLSQP', options={'ftol':1e-10, 'maxiter':60})
        if not posture.success or not np.all(np.isfinite(posture.x)):
            raise TaskStepError(f'bounded task posture unavailable: {posture.message}')
        candidate = origin + nullspace @ posture.x
    # Only roundoff-sized corrections are allowed, followed by full verification.
    x = np.clip(candidate, lower/unit, upper/unit)
    if (np.max(abs(eq @ x - rhs), initial=0.) > 1e-7
            or np.max(abs(x-candidate), initial=0.) > 1e-7
            or (a_ub is not None and np.max(a_ub[:, :n] @ x-b_ub, initial=0.) > 1e-10)):
        raise TaskStepError('bounded task candidate failed residual verification')
    return x * unit, tuple(float(v) for v in progress.x[n:])
