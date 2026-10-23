"""Offline evidence classification. No solver, device, transport or authority.

A witness establishes an endpoint within the stated model/tolerances only.
Continuity is a necessary numerical step bound, not a collision/path proof.
"""
import math
from numbers import Real
from teleop.evaluation.kuavo_layers import pose_error
from teleop.robots.kuavo5_native_ik import ARM_NAMES


def _vector(values):
    values=tuple(values)
    if len(values)!=14 or any(isinstance(v,bool) or not isinstance(v,Real) or not math.isfinite(v) for v in values):
        raise ValueError('fourteen finite joint angles required')
    return values


def assess_answer(schema,q,targets,actual,previous,*,active_sides,response_success):
    if schema.names!=ARM_NAMES or type(response_success) is not bool:
        raise ValueError('ordered arm schema and boolean response required')
    q,previous=_vector(q),_vector(previous)
    sides=tuple(active_sides)
    if not sides or len(set(sides))!=len(sides) or set(sides)-{'left','right'}:
        raise ValueError('distinct active sides required')
    if set(targets)!={'left','right'} or set(actual)!={'left','right'}:
        raise ValueError('both target and actual hand poses required')
    errors={side:pose_error(targets[side],actual[side]) for side in ('left','right')}
    axis={side:max(abs(a-b) for a,b in zip(targets[side].position_m,actual[side].position_m)) for side in targets}
    outside=[j.name for j,v in zip(schema.joints,q) if not j.position_min<=v<=j.position_max]
    if any(not j.position_min<=v<=j.position_max for j,v in zip(schema.joints,previous)):
        raise ValueError('reference pose outside model')
    active_indices=[i for i in range(14) if ('left' if i<7 else 'right') in sides]
    margin=math.radians(.5)
    margin_ok=all(schema.joints[i].position_min+margin<=q[i]<=schema.joints[i].position_max-margin for i in active_indices)
    step=max(abs(q[i]-previous[i]) for i in active_indices)
    position_ok=all(errors[s]['position_m']<=math.sqrt(3)*.02 and axis[s]<=.02 for s in targets)
    orientation_ok=all(errors[s]['rotation_deg']<=math.degrees(.1) for s in targets)
    position_witness=response_success and not outside and position_ok
    full=position_witness and orientation_ok
    return dict(response_success=response_success,errors=errors,axis_error_m=axis,outside_joints=outside,
        position_witness=position_witness,full_pose_witness=full,active_margin_ok=margin_ok,
        max_active_delta_rad=step,continuous_candidate=full and margin_ok and step<=.2,
        collision_free_path_proven=False,physical_robot_command_sent=False,
        tolerances=dict(position_axis_m=.02,position_norm_m=math.sqrt(3)*.02,
                        rotation_deg=math.degrees(.1),continuity_rad=.2,active_margin_rad=margin))


def summarize_attempts(attempts):
    full=sum(bool(a['full_pose_witness']) for a in attempts)
    position=sum(bool(a['position_witness']) for a in attempts)
    return dict(attempts=len(attempts),full_pose_witnesses=full,position_witnesses=position,
        continuous_candidates=sum(bool(a['continuous_candidate']) for a in attempts),
        finding=('full_pose_witness_found' if full else 'only_position_witness_found' if position
                 else 'no_witness_in_these_attempts'),unreachable_proven=False,
        collision_free_path_proven=False,physical_robot_command_sent=False)
