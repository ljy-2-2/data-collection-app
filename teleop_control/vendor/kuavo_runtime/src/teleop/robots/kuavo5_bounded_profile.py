"""Explicit v63 candidate; same shared input, official math, sole arm sender."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
from types import MappingProxyType

from teleop.robots.kuavo5_model_limits import intersect_arm_limits
from teleop.robots.kuavo5_native_ik import ARM_NAMES
from teleop.retargeting.kuavo_workspace_bound import read_arm_reach_bound

CONTROL_SHA = 'fecef6f980806af7d2c4b1abe56d3fe1d0dbfbd69b3475303eb158bcc06c85dc'
ORIGINAL_IK_SHA = '4d7a3819a4879602f8af56f1125212a27fa747c35a297cff98e7e45b504a44d1'
INTERSECTION_SHA = '9cc9692a04232f0f3fd4aadb6dc2515b19964f1c1c64b7db834cdcad7b459eb7'


def profile_fields():
    return dict(target_recovery='bounded_candidate',
                baseline_profile='kuavo5_bounded_candidate_20260921',
                target_progress_policy='fixed_anchor_bounded_latest_target',
                target_progress_failure_rebase='qualified_fk_waypoint_preserve_raw_clutch_anchor',
                target_position_step_m=.01, target_orientation_step_rad=.05,
                reference_sync='per_arm', workspace_soft_margin_m=.05,
                compute_kind='official_library_control_intersection',
                compute_model_policy='private_copy_no_vendor_model_write',
                candidate_status='offline_validated_robot_environment_and_motion_unverified')


@dataclass(frozen=True)
class BoundedModel:
    xml: str
    changes: tuple
    bounds: object
    identity: object


def prepare_model(ik_bytes, control_bytes):
    for data, expected in ((ik_bytes, ORIGINAL_IK_SHA), (control_bytes, CONTROL_SHA)):
        if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('candidate model hash differs from audited v63 snapshot')
    xml, changes = intersect_arm_limits(ik_bytes.decode(), control_bytes.decode())
    if hashlib.sha256(xml.encode()).hexdigest() != INTERSECTION_SHA:
        raise ValueError('candidate intersection model hash mismatch')
    # The pinned v63 config has eef_z_offset=0. The rigid end-effector offset
    # already lives in the URDF. Never equate it with the removed metal plate.
    bounds = {side: read_arm_reach_bound(xml, ARM_NAMES[i*7:(i+1)*7],
              'zarm_'+letter+'7_end_effector')
              for i, (side, letter) in enumerate((('left','l'),('right','r')))}
    identity = dict(compute_kind='official_library_control_intersection',
                    urdf_sha256=CONTROL_SHA, source_ik_sha256=ORIGINAL_IK_SHA,
                    ik_sha256=hashlib.sha256(xml.encode()).hexdigest(),
                    tool_geometry_status='historical_model_current_hardware_unverified')
    return BoundedModel(xml, tuple(changes), MappingProxyType(bounds), MappingProxyType(identity))


def validate_ready(packet, arm_posture='baseline'):
    """Do not send PICO input to a mismatched remote candidate configuration."""
    from teleop.robots.kuavo5_numeric_process import PINNED, VENDOR
    from teleop.robots.kuavo5_posture_source import numeric_source_for
    from teleop.robots.kuavo5_posture import validate_posture_ready
    validate_posture_ready(packet, arm_posture, 'bounded_candidate')
    SOURCE = numeric_source_for(arm_posture)
    if any(packet.get(k) != v for k, v in profile_fields().items()):
        raise ValueError('candidate execution profile mismatch')
    identity = packet.get('compute_identity')
    expected = dict(compute_kind='official_library_control_intersection',
        urdf_sha256=CONTROL_SHA, source_ik_sha256=ORIGINAL_IK_SHA,
        ik_sha256=INTERSECTION_SHA, plant_ik_sha256=PINNED[VENDOR/'lib/libplantIK.so'],
        drake_sha256=PINNED[Path('/opt/drake/lib/libdrake.so')],
        numeric_source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest())
    if not isinstance(identity, dict) or any(identity.get(k) != v for k,v in expected.items()) \
            or packet.get('ik_model_sha256') != INTERSECTION_SHA:
        raise ValueError('candidate computation identity mismatch')
    validate_posture_ready(identity, arm_posture, 'bounded_candidate')
