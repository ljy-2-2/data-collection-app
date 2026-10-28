"""Session-only mode4 coordinates for the observed DM4310 turn ambiguity.

This is an explicit, bounded empirical model, not a firmware root-cause claim.
The original endpoint samples and operator targets never change. Only a unique
0/+one nominal rotor-turn offset fitting the original two-encoder interval may
be used. All raw targets and native limits use that same offset. Unsupported
identities, other shifts, wrap and changing offsets fail before control.
"""
from dataclasses import dataclass, replace
import math
import struct

from .damiao_gripper import MotorProfile, ENABLE, DISABLE, encode_position, finite
from .kuavo5_gripper_reference import report_reference, ReferenceMismatch

STRATEGY = 'dm4310_observed_boot_turn_v1'
IDENTITY = (1445998644, 808595767, 825241648)
GEAR_RATIO = 10.0
TURN_RAD = 2*math.pi/GEAR_RATIO


def validate_policy(motor):
    policy = motor.get('position_reference_policy')
    if policy is None:
        return False
    if (not isinstance(policy, dict) or set(policy) != {'strategy', 'nominal_gear_ratio',
            'allowed_turn_offsets', 'physical_verification_required'}
            or policy['strategy'] != STRATEGY
            or type(policy['nominal_gear_ratio']) not in (int, float)
            or policy['nominal_gear_ratio'] != GEAR_RATIO
            or policy['allowed_turn_offsets'] != [0, 1]
            or any(type(value) is not int for value in policy['allowed_turn_offsets'])
            or policy['physical_verification_required'] is not True):
        raise ValueError('unsupported gripper position reference policy')
    if motor.get('profile', {}).get('control_mode') != 4:
        raise ValueError('boot coordinate conversion supports verified mode4 only')
    if tuple(motor.get('readback', {}).get(key) for key in
             ('hardware_version', 'software_version', 'sub_version')) != IDENTITY:
        raise ValueError('boot coordinate policy requires the recorded DM4310/7120/0001 identity')
    return True


@dataclass(frozen=True)
class SessionCoordinates:
    offset_rad: float
    turns: int
    profile: MotorProfile
    raw_profile: MotorProfile
    report: dict

    def to_canonical(self, raw_position):
        return finite(raw_position, 'raw motor position')-self.offset_rad

    def to_wire(self, payload):
        if payload in (ENABLE, DISABLE) or self.offset_rad == 0:
            return payload
        if not isinstance(payload, bytes) or len(payload) != 8 or self.profile.control_mode != 4:
            raise ValueError('mode4 position payload required for coordinate conversion')
        canonical = struct.unpack('<f', payload[:4])[0]
        # Re-encode with the raw envelope to preserve float32 inward rounding.
        raw = encode_position(self.raw_profile, canonical+self.offset_rad, takeover=True)
        return raw[:4]+payload[4:]


def resolve_coordinates(motor, measured, now_ns=None):
    enabled = validate_policy(motor)
    profile = MotorProfile(**motor['profile'])
    report = report_reference(motor, measured, now_ns,
                              configured_working_coverage=enabled)
    if not enabled:
        if report['status'] != 'compatible':
            raise ReferenceMismatch(report)
        return SessionCoordinates(0., 0, profile, profile, report)
    if report['status'] not in ('compatible', 'shift_detected'):
        raise ReferenceMismatch(report)
    reference, current = report['reference'], report['current']
    low, high = reference['expanded_difference_interval_rad']
    candidates = []
    for turns in (0, 1):
        offset = turns*TURN_RAD
        if all(low <= current[key]-offset <= high for key in ('difference_rad', 'difference_after_rad')):
            candidates.append(turns)
    if len(candidates) != 1:
        report.update(status='unsupported_reference_shift',
                      reason='no unique supported 0/+one-turn offset; no arbitrary fitted compensation')
        raise ReferenceMismatch(report)
    turns = candidates[0]
    offset = turns*TURN_RAD
    raw_endpoints = (profile.open_rad+offset, profile.closed_rad+offset)
    if any(abs(value) >= min(math.pi-.02, profile.pmax) for value in raw_endpoints):
        report.update(status='unsupported_reference_shift', reason='translated mechanical envelope crosses supported single-turn range')
        raise ReferenceMismatch(report)
    raw = replace(profile, open_rad=raw_endpoints[0], closed_rad=raw_endpoints[1])
    report.update(status='session_coordinates_resolved',
        reason='unique bounded observed turn model; physical closure still requires operator validation',
        strategy=STRATEGY, turn_offsets=turns, offset_rad=offset,
        compensation_applied=offset != 0., nominal_gear_ratio=GEAR_RATIO,
        gear_ratio_source='manufacturer_v1.1_manual', gear_ratio_read_back=False,
        firmware_root_cause='unknown', physical_verification_required=True,
        canonical_mechanical_bounds_rad=sorted((profile.open_rad, profile.closed_rad)),
        raw_mechanical_bounds_rad=sorted((raw.open_rad, raw.closed_rad)),
        canonical_soft_bounds_rad=list(profile.bounds), raw_soft_bounds_rad=list(raw.bounds))
    return SessionCoordinates(offset, turns, profile, raw, report)
