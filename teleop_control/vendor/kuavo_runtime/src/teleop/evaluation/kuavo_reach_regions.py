"""Offline regional separation of layer errors; no solver or device access.

An outer geometric bound can exclude a pose. Its interior cannot establish
joint feasibility, collision freedom, tracking delay, or physical safety.
"""
from collections.abc import Mapping
from teleop.contracts.intent import CartesianPose
from teleop.evaluation.kuavo_layers import EDGES, _pose, pose_error, stats
from teleop.retargeting.kuavo_workspace_bound import ArmReachBound

REGIONS = ('inside_nominal_not_proven_feasible',
           'outside_nominal_within_tolerance', 'excluded_by_pose_bound')


def group_layers_by_reach(report, bounds):
    if (not isinstance(bounds, Mapping) or not bounds
            or set(bounds) - {'left', 'right'}
            or any(not isinstance(b, ArmReachBound) for b in bounds.values())):
        raise ValueError('explicit arm bounds required')
    groups = {name: [] for name in REGIONS}
    missing = 0
    for sample in report['samples']:
        sides = sample['active_sides']
        if len(sides) != len(set(sides)) or set(sides) - set(bounds):
            raise ValueError('distinct active sides with matching bounds required')
        for side in sides:
            row = sample['sides'][side]
            poses = row['poses']
            if poses.get('A') is None:
                missing += 1
                continue
            raw = _pose(poses['A'])
            evidence = bounds[side].assess(CartesianPose(
                raw['frame_id'], raw['position_m'], raw['orientation_xyzw']))
            if evidence['outside_pose_bound']:
                region = REGIONS[2]
            elif evidence['wrist_distance_m'] > evidence['wrist_radius_m']:
                region = REGIONS[1]
            else:
                region = REGIONS[0]
            errors = {}
            for edge in EDGES:
                a, b = (poses.get(key) for key in edge)
                if (a is not None and b is not None
                        and (edge != 'CD' or row['candidate_reference_relation'] == 'same_solve')):
                    errors[edge] = pose_error(a, b)
            groups[region].append(errors)
    result = {}
    for name, rows in groups.items():
        result[name] = dict(samples=len(rows), errors={edge: {
            unit: stats([r[edge][unit] for r in rows if edge in r])
            for unit in ('position_m', 'rotation_deg')} for edge in EDGES})
    return dict(regions=result, classified_samples=sum(len(r) for r in groups.values()),
                missing_raw_samples=missing,
                scope='geometric_region_layer_separation_not_causal_delay_or_feasibility',
                physical_robot_command_sent=False)
