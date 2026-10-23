"""Offline-only feedback-waypoint candidate, not a hardware-fault workaround.

A qualified IK result can be ahead of measured motion. This experiment bounds
an affected arm's next numerical waypoint relative to measured FK while leaving
the original clutch target intact. It neither verifies the actuator nor grants
motion/rearm authority. Deliberately not wired to any live entry.
"""
from dataclasses import dataclass
import math

from teleop.contracts.intent import CartesianPose, EndEffectorPoseIntent
from teleop.retargeting.kuavo_target_progress import _bounded_pose


@dataclass(frozen=True)
class FeedbackWaypointTrial:
    original_intent: EndEffectorPoseIntent
    effective_intent: EndEffectorPoseIntent
    seed_rad: tuple
    affected_sides: tuple


def prepare_feedback_waypoint(intent, measured_poses, measured_q, seed_q, *,
        affected_sides, feedback_time_ns, now_ns,
        position_step_m=.01, orientation_step_rad=.05):
    """Prepare a mathematical experiment; full IK/FK/limits still belong to solver.

    FK poses must be calculated from the supplied measured_q by the caller.
    This function cannot establish that provenance or physical freshness.
    Its age check only rejects stale metadata; it does not renew any timestamp.
    """
    if not isinstance(intent, EndEffectorPoseIntent):
        raise ValueError('end-effector intent required')
    if type(now_ns) is not int or type(feedback_time_ns) is not int \
            or not 0 <= feedback_time_ns <= now_ns \
            or now_ns-feedback_time_ns >= 100_000_000:
        raise ValueError('fresh, non-future feedback metadata required')
    if isinstance(affected_sides, str):
        raise ValueError('explicit affected arm tuple required')
    sides = tuple(affected_sides)
    if not sides or len(set(sides)) != len(sides) or set(sides)-set(intent.targets):
        raise ValueError('distinct affected arms must be active targets')
    for name, value, upper in (('position step',position_step_m,.01),
                              ('orientation step',orientation_step_rad,.05)):
        if type(value) not in (int,float) or not math.isfinite(value) or not 0 < value <= upper:
            raise ValueError(name+' outside experiment budget')
    def vector(q):
        if not isinstance(q,(tuple,list)) or len(q)!=14 or any(
                type(x) not in (int,float) or not math.isfinite(x) for x in q):
            raise ValueError('finite fourteen-joint vector required')
        return tuple(float(x) for x in q)
    measured_q, seed = vector(measured_q), list(vector(seed_q))
    targets = dict(intent.targets)
    for side in sides:
        pose = measured_poses.get(side) if hasattr(measured_poses,'get') else None
        if not isinstance(pose,CartesianPose) or pose.frame_id != targets[side].frame_id:
            raise ValueError('matching measured FK pose required for '+side)
        # Revalidate rather than trusting an externally supplied object.
        pose=CartesianPose(pose.frame_id,pose.position_m,pose.orientation_xyzw)
        targets[side]=_bounded_pose(pose,targets[side],position_step_m,orientation_step_rad)
        offset=0 if side=='left' else 7
        seed[offset:offset+7]=measured_q[offset:offset+7]
    return FeedbackWaypointTrial(intent,EndEffectorPoseIntent(targets,source=intent.source),tuple(seed),sides)
