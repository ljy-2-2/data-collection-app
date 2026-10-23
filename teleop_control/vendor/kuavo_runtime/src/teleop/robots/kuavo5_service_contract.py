"""Versioned client contract, independent of robot implementation source.

Only public metadata from an already verified installation belongs here. A
contract authenticates no server by itself: use it only after TLS/token checks.
Robot installation hashes and per-session integrity checks remain mandatory.
This module is pure stdlib and does not load ROS, SDKs, models or hardware.
"""
import copy
import hashlib
import json
import re


SESSION_PROTOCOL = 'kuavo_robot_session_v1'
MAINTENANCE_PROTOCOL = 'kuavo_idle_maintenance_v1'
ARM_CAPABILITY = 'arms_v63_0p7_elbow'
GRIPPER_CAPABILITY = 'damiao_trigger_v1'
RETURN_CAPABILITY = 'return_yb_v1'
DEFAULT_REQUIREMENTS = dict(protocol=SESSION_PROTOCOL,
    capabilities=[ARM_CAPABILITY, GRIPPER_CAPABILITY, RETURN_CAPABILITY])
_PROFILE = dict(target_recovery='bounded_candidate', speed_profile='responsive',
                arm_posture='elbow_preference', gripper_mode='damiao')
_CONTROL_SHA = 'fecef6f980806af7d2c4b1abe56d3fe1d0dbfbd69b3475303eb158bcc06c85dc'
_IK_SHA = '9cc9692a04232f0f3fd4aadb6dc2515b19964f1c1c64b7db834cdcad7b459eb7'
_SOURCE_IK_SHA = '4d7a3819a4879602f8af56f1125212a27fa747c35a297cff98e7e45b504a44d1'
_BASE_READY = dict(robot_version='63', urdf_sha256=_CONTROL_SHA,
    ik_model_sha256=_IK_SHA, profile_configured=True, gripper_mode='damiao',
    gripper_can_send_enabled=True, target_recovery='bounded_candidate',
    speed_profile='responsive', arm_posture='elbow_preference',
    elbow_preference_weight=.1, max_velocity_rad_s=.7,
    planning_acceleration_rad_s2=5., tracking_soft_lead_rad=.12,
    reference_sync='per_arm', compute_kind='official_library_control_intersection')
_TEMPLATE_FIELDS = {'protocol', 'capabilities', 'profile', 'ready_expectations',
                    'shared_bus_compatibility_verified'}
_IDENTITY_FIELDS = {'service_instance_id', 'installation_sha256', 'bundle_sha256'}


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{'+str(length)+'}', value) is not None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def validate_requirements(value=None):
    value = copy.deepcopy(DEFAULT_REQUIREMENTS if value is None else value)
    if not isinstance(value, dict) or set(value) != {'protocol', 'capabilities'}:
        raise ValueError('exact supported contract requirements required')
    if value['protocol'] != SESSION_PROTOCOL:
        raise ValueError('unsupported session protocol')
    caps = value['capabilities']
    known = {ARM_CAPABILITY, GRIPPER_CAPABILITY, RETURN_CAPABILITY}
    if (not isinstance(caps, list) or any(not isinstance(x, str) for x in caps)
            or len(caps) != len(set(caps)) or not set(caps) <= known
            or not {ARM_CAPABILITY, GRIPPER_CAPABILITY} <= set(caps)):
        raise ValueError('supported arm/gripper contract capabilities required')
    value['capabilities'] = sorted(caps)
    return value


def _check_template(value):
    if not isinstance(value, dict) or set(value) != _TEMPLATE_FIELDS:
        raise ValueError('exact session contract fields required')
    if value['protocol'] != SESSION_PROTOCOL:
        raise ValueError('unsupported session protocol')
    profile = value['profile']
    expected_fields = set(_PROFILE) | {'gripper_config_sha256'}
    if isinstance(profile, dict) and 'return_home_config_sha256' in profile:
        expected_fields.add('return_home_config_sha256')
    if (not isinstance(profile, dict) or set(profile) != expected_fields
            or any(profile.get(k) != v for k, v in _PROFILE.items())
            or not _hex(profile.get('gripper_config_sha256'), 64)
            or ('return_home_config_sha256' in profile
                and not _hex(profile['return_home_config_sha256'], 64))):
        raise ValueError('unsupported installed motion profile')
    caps = [ARM_CAPABILITY, GRIPPER_CAPABILITY]
    expected = dict(_BASE_READY, gripper_config_sha256=profile['gripper_config_sha256'])
    if 'return_home_config_sha256' in profile:
        caps.append(RETURN_CAPABILITY)
        expected.update(return_home_enabled=True,
            return_home_config_sha256=profile['return_home_config_sha256'],
            return_home_buttons=dict(request='left_y', cancel='right_b'))
    else:
        expected.update(return_home_enabled=False, return_home_config_sha256=None)
    if (value['capabilities'] != sorted(caps)
            or value['ready_expectations'] != expected
            or type(value['shared_bus_compatibility_verified']) is not bool):
        raise ValueError('unsupported capabilities or execution semantics')
    return copy.deepcopy(value)


def prepare_service_contract(bundle):
    """Build public contract from an installation already verified by bootstrap.

    Identity/config hashes bind this descriptor to that immutable installation;
    endpoint values, CAN paths and source text never travel to the PC.
    """
    profile = {key: bundle.get(key) for key in _PROFILE}
    profile['gripper_config_sha256'] = bundle.get('gripper_config_sha256')
    caps = [ARM_CAPABILITY, GRIPPER_CAPABILITY]
    ready = dict(_BASE_READY, gripper_config_sha256=profile['gripper_config_sha256'])
    if bundle.get('return_home_config') is not None:
        profile['return_home_config_sha256'] = bundle.get('return_home_config_sha256')
        caps.append(RETURN_CAPABILITY)
        ready.update(return_home_enabled=True,
            return_home_config_sha256=profile['return_home_config_sha256'],
            return_home_buttons=dict(request='left_y', cancel='right_b'))
    else:
        ready.update(return_home_enabled=False, return_home_config_sha256=None)
    preparation = bundle.get('gripper_config', {}).get('preparation', {})
    verified = preparation.get('shared_bus_compatibility_verified', False)
    return _check_template(dict(protocol=SESSION_PROTOCOL, capabilities=sorted(caps),
        profile=profile, ready_expectations=ready,
        shared_bus_compatibility_verified=verified))


def bind_service_contract(template, *, service_instance_id, installation_sha256, bundle_sha256):
    value = _check_template(template)
    if not _hex(service_instance_id, 32) or any(not _hex(v, 64)
            for v in (installation_sha256, bundle_sha256)):
        raise ValueError('complete service installation identity required')
    value.update(service_instance_id=service_instance_id,
                 installation_sha256=installation_sha256, bundle_sha256=bundle_sha256)
    value['contract_sha256'] = _digest(value)
    return value


def validate_contract(contract, required_contract=None):
    requirements = validate_requirements(required_contract)
    if (not isinstance(contract, dict)
            or set(contract) != _TEMPLATE_FIELDS | _IDENTITY_FIELDS | {'contract_sha256'}):
        raise ValueError('versioned service contract unavailable')
    expected = bind_service_contract({k: contract[k] for k in _TEMPLATE_FIELDS},
                                    **{k: contract[k] for k in _IDENTITY_FIELDS})
    if contract != expected:
        raise ValueError('service contract integrity mismatch')
    if not set(requirements['capabilities']) <= set(contract['capabilities']):
        raise ValueError('required service capabilities unavailable')
    return expected


def validate_service_contract(status, required_contract=None):
    """Validate only data from an authenticated status response, without I/O."""
    if not isinstance(status, dict) or status.get('event') != 'service_status':
        raise ValueError('service status required')
    contract = validate_contract(status.get('session_contract'), required_contract)
    if any(status.get(k) != contract[k] for k in _IDENTITY_FIELDS | {'profile'}):
        raise ValueError('service status and contract identity differ')
    if status.get('session_duration_policy') != 'zero_is_unbounded_v1':
        raise ValueError('continuous session policy unavailable')
    return contract


def validate_ready(packet, contract):
    """Verify concrete worker readiness before the first PICO reply is sent."""
    contract = validate_contract(contract, dict(protocol=SESSION_PROTOCOL,
        capabilities=[ARM_CAPABILITY, GRIPPER_CAPABILITY]))
    if not isinstance(packet, dict) or packet.get('event') != 'ready':
        raise ValueError('worker READY required')
    for key, expected in contract['ready_expectations'].items():
        actual = packet.get(key, False if key == 'return_home_enabled' else None)
        if actual != expected or (isinstance(expected, bool) and type(actual) is not bool):
            raise ValueError('worker READY differs from service contract: '+key)
    if packet.get('standalone_return', False) is not False:
        raise ValueError('unexpected standalone return worker')
    identity = packet.get('compute_identity')
    if not isinstance(identity, dict):
        raise ValueError('worker computation identity missing')
    expected_identity = dict(compute_kind='official_library_control_intersection',
        urdf_sha256=_CONTROL_SHA, source_ik_sha256=_SOURCE_IK_SHA, ik_sha256=_IK_SHA,
        arm_posture='elbow_preference', elbow_preference_weight=.1)
    if any(identity.get(k) != v for k, v in expected_identity.items()):
        raise ValueError('worker model/posture identity mismatch')
    # Implementation/library revisions are checked by the robot's immutable
    # installation and official-math loader, not compared to PC source files.
    for key in ('plant_ik_sha256', 'drake_sha256', 'numeric_source_sha256'):
        if not _hex(identity.get(key), 64):
            raise ValueError('worker computation digest missing: '+key)


def host_bundle(contract):
    """Metadata adapter for the existing PC lifecycle; contains no robot code."""
    contract = validate_contract(contract, dict(protocol=SESSION_PROTOCOL,
        capabilities=[ARM_CAPABILITY, GRIPPER_CAPABILITY]))
    value = dict(mode='execute', sha256=contract['bundle_sha256'], **contract['profile'])
    value['gripper_config'] = {'preparation': {'shared_bus_compatibility_verified':
                                             contract['shared_bus_compatibility_verified']}}
    if RETURN_CAPABILITY in contract['capabilities']:
        value['return_home_config'] = {}  # presence only; the robot owns all parameters
    value['service_contract'] = contract
    return value
