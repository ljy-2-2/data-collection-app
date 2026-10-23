"""Observe accepted waypoint progress; no IK, controller, or reachability claim."""
from teleop.evaluation.kuavo_layers import pose_error


def audit_progress(solves, diagnostics):
    def key(s):
        return s['epoch'], s['sequence'], s['started_ns']
    indexed = {}
    for d in diagnostics:
        k = d['context']['epoch'], d['source_sequence_id'], d['started_monotonic_ns']
        if k in indexed:
            raise ValueError('duplicate diagnostic')
        indexed[k] = d
    if len({key(s) for s in solves}) != len(solves):
        raise ValueError('duplicate solve')
    if any(b['started_ns'] <= a['started_ns'] for a, b in zip(solves, solves[1:])):
        raise ValueError('solve times must increase')
    previous = {}; epoch = None; rows = []
    for s in solves:
        if key(s) not in indexed:
            raise ValueError('missing matching diagnostic')
        generations = s.get('clutch_generations')
        if not isinstance(generations, (list, tuple)) or len(generations) != 2:
            raise ValueError('per-hand generation evidence required')
        d = indexed[key(s)]
        if (s['error'] is None) != (d['event'] == 'ik_trace'):
            raise ValueError('inconsistent accepted result')
        if epoch != s['epoch']:
            previous = {}; epoch = s['epoch']
        for i, side in enumerate(('left', 'right')):
            old = previous.get(side)
            if old and old[0] != generations[i]:
                previous.pop(side); old = None
            if s['error'] is not None:
                continue
            progress = d.get('target_progress') or {}
            raw = (progress.get('raw_targets') or {}).get(side)
            target = (progress.get('effective_targets') or {}).get(side)
            actual = (d.get('actual_poses') or {}).get(side)
            if raw is None or target is None or actual is None:
                previous.pop(side, None)
                continue
            residual = pose_error(target, actual)
            remaining = pose_error(raw, actual)
            if old and 0 < s['started_ns']-old[1] <= 200_000_000:
                advance = pose_error(old[3], actual)
                requested = pose_error(old[3], target)
                rows.append(dict(side=side,epoch=epoch,sequence=s['sequence'],
                    previous_sequence=old[2],started_ns=s['started_ns'],
                    advance_m=advance['position_m'],advance_rotation_deg=advance['rotation_deg'],
                    requested_step_m=requested['position_m'],remaining_m=remaining['position_m'],
                    remaining_rotation_deg=remaining['rotation_deg'],residual_m=residual['position_m'],
                    small_advance_far_goal=(remaining['position_m'] > .1 and advance['position_m'] < .001),
                    unreachable_proven=False))
            previous[side] = (generations[i], s['started_ns'], s['sequence'], actual)
    return dict(scope='accepted_waypoint_observation_not_reachability_or_physical_execution',
                thresholds=dict(far_goal_m=.1,small_advance_m=.001,previous_max_age_ms=200),
                rows=rows,small_advance_far_goal_count=sum(r['small_advance_far_goal'] for r in rows),
                physical_robot_command_sent=False)
