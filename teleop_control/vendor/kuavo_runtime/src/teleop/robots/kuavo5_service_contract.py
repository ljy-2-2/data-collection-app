"""Versioned client contract, independent of robot implementation source.

Only public metadata from an already verified installation belongs here. A
contract authenticates no server by itself: use it only after TLS/token checks.
Robot installation hashes and per-session integrity checks remain mandatory.
This module is pure stdlib and does not load ROS, SDKs, models or hardware.
"""
import copy
import ast
import hashlib
import json
import math
import re


SESSION_PROTOCOL = 'kuavo_robot_session_v1'
CONFIGURABLE_SESSION_PROTOCOL = 'kuavo_robot_session_v2'
SETTINGS_PROTOCOL = 'kuavo_session_settings_v1'
CONFIGURABLE_ARM_CAPABILITY = 'arms_v63_configurable_elbow_v1'
MOTION_SESSION_PROTOCOL = 'kuavo_robot_session_v3'
MOTION_SETTINGS_PROTOCOL = 'kuavo_session_settings_v2'
MOTION_ARM_CAPABILITY = 'arms_v63_per_arm_motion_v1'
CONFIGURABLE_SESSION_PROTOCOLS = (CONFIGURABLE_SESSION_PROTOCOL, MOTION_SESSION_PROTOCOL)
SETTING_FIELDS = frozenset(('arm_speed_rad_s', 'left_gripper_speed_rad_s',
    'right_gripper_speed_rad_s', 'left_gripper_open_target_rad',
    'left_gripper_closed_target_rad', 'right_gripper_open_target_rad',
    'right_gripper_closed_target_rad'))
MOTION_SETTING_FIELDS = (SETTING_FIELDS - {'arm_speed_rad_s'}) | {
    side+suffix for side in ('left', 'right') for suffix in (
        '_arm_scale', '_arm_speed_rad_s', '_arm_acceleration_rad_s2',
        '_gripper_acceleration_rad_s2')}
SETTING_ALIASES = {'arm_speed_rad_s': ('left_arm_speed_rad_s', 'right_arm_speed_rad_s'),
    'arm_acceleration_rad_s2': ('left_arm_acceleration_rad_s2', 'right_arm_acceleration_rad_s2')}

def expand_settings_changes(changes, fields):
    """Expand legacy both-arm shortcuts for a per-arm installation only."""
    validate_settings_values(changes, partial=True)
    result = copy.deepcopy(changes)
    if set(fields) == MOTION_SETTING_FIELDS:
        for alias, targets in SETTING_ALIASES.items():
            if alias in result:
                if any(key in result for key in targets):
                    raise ValueError('cannot mix a both-arm shortcut with side-specific values')
                value = result.pop(alias)
                result.update(dict.fromkeys(targets, value))
    if not set(result) <= set(fields):
        raise ValueError('settings fields unavailable in this installation')
    return result
MAINTENANCE_PROTOCOL = 'kuavo_idle_maintenance_v1'
ARM_CAPABILITY = 'arms_v63_0p7_elbow'
GRIPPER_CAPABILITY = 'damiao_trigger_v1'
RETURN_CAPABILITY = 'return_yb_v1'
TOGGLE_RETURN_CAPABILITY = 'return_y_toggle_v1'
BASE_CAPABILITY = 'base_vendor_vr_x_v1'
JOINT_BASE_READY = dict(base_mode='vendor_vr_x', base_lock_button='left_x',
    base_reference_frame='body_relative', base_speed_profile='vendor_vr',
    base_motion_policy='owned_body_relative_v1')
LEGACY_RETURN_BUTTONS = dict(request='left_y', cancel='right_b')
TOGGLE_RETURN_BUTTONS = dict(request='left_y', cancel='left_y')
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


def validate_settings_values(values, *, partial=False):
    """Public numeric envelope only; robot additionally checks formal bounds."""
    if (not isinstance(values, dict) or not values or not set(values) <= MOTION_SETTING_FIELDS | set(SETTING_ALIASES)
            or (not partial and set(values) not in (SETTING_FIELDS, MOTION_SETTING_FIELDS))):
        raise ValueError('exact supported session setting fields required')
    for name, value in values.items():
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('finite numeric session settings required')
        if name.endswith('arm_speed_rad_s') and not 0 < value <= 1.:
            raise ValueError('arm speed must remain in (0, 1.0] rad/s')
        if name.endswith('gripper_speed_rad_s') and not .01 <= value <= 15.:
            raise ValueError('gripper speed must remain in [0.01, 15] rad/s')
        if name.endswith('arm_acceleration_rad_s2') and not 0 < value <= 10.:
            raise ValueError('arm acceleration must remain in (0, 10] rad/s^2')
        if name.endswith('gripper_acceleration_rad_s2') and not 0 < value <= 200.:
            raise ValueError('gripper acceleration must remain in (0, 200] rad/s^2')
        if name.endswith('_arm_scale') and not .1 <= value <= 2.:
            raise ValueError('arm position scale must remain within 0.1..2')
    if not partial and set(values) == MOTION_SETTING_FIELDS:
        for side in ('left', 'right'):
            speed = values[side+'_arm_speed_rad_s']
            acceleration = values[side+'_arm_acceleration_rad_s2']
            if .12 + speed*speed/(2*acceleration) + speed*.01 >= .2:
                raise ValueError(side+' arm speed/acceleration pair lacks tracking braking reserve; '
                    'choose lower speed or higher acceleration within the allowed range')
    if not partial:
        for side in ('left', 'right'):
            if values[side+'_gripper_open_target_rad'] == values[side+'_gripper_closed_target_rad']:
                raise ValueError('gripper open and closed targets must differ')
    return copy.deepcopy(values)


def validate_settings_snapshot(snapshot):
    fields = {'protocol', 'installation_sha256', 'bundle_sha256', 'revision', 'values', 'settings_sha256'}
    if (not isinstance(snapshot, dict) or set(snapshot) != fields
            or snapshot.get('protocol') not in (SETTINGS_PROTOCOL, MOTION_SETTINGS_PROTOCOL)
            or type(snapshot.get('revision')) is not int or snapshot['revision'] < 0
            or any(not _hex(snapshot.get(k), 64) for k in ('installation_sha256', 'bundle_sha256', 'settings_sha256'))):
        raise ValueError('valid versioned session settings snapshot required')
    validate_settings_values(snapshot['values'])
    fields = MOTION_SETTING_FIELDS if snapshot['protocol'] == MOTION_SETTINGS_PROTOCOL else SETTING_FIELDS
    if set(snapshot['values']) != fields:
        raise ValueError('settings fields do not match the declared protocol')
    if _digest({k: v for k, v in snapshot.items() if k != 'settings_sha256'}) != snapshot['settings_sha256']:
        raise ValueError('session settings integrity mismatch')
    return copy.deepcopy(snapshot)


def _settings_ready(snapshot):
    """Match the core's startup-only numeric profile, including braking ramp."""
    values = snapshot['values']
    if snapshot['protocol'] == MOTION_SETTINGS_PROTOCOL:
        arms = {side: dict(scale=values[side+'_arm_scale'],
            speed_rad_s=values[side+'_arm_speed_rad_s'],
            acceleration_rad_s2=values[side+'_arm_acceleration_rad_s2'])
            for side in ('left', 'right')}
        return dict(max_velocity_rad_s=max(p['speed_rad_s'] for p in arms.values()),
            planning_acceleration_rad_s2=max(p['acceleration_rad_s2'] for p in arms.values()),
            arm_motion_parameters=arms, session_settings_revision=snapshot['revision'],
            session_settings_sha256=snapshot['settings_sha256'])
    speed = values['arm_speed_rad_s']
    return dict(max_velocity_rad_s=speed,
                planning_acceleration_rad_s2=snapshot['values'].get(
                    'arm_acceleration_rad_s2', 10. if speed > .7 else 5.),
                session_settings_revision=snapshot['revision'],
                session_settings_sha256=snapshot['settings_sha256'])


def validate_settings_schema(schema, defaults):
    """Check the public editor metadata; robot retains the full model policy."""
    validate_settings_values(defaults)
    if not isinstance(schema, dict) or set(schema) != set(defaults):
        raise ValueError('exact session settings schema required')
    for name, item in schema.items():
        required = {'label', 'unit', 'min', 'max', 'default'}
        if (not isinstance(item, dict) or not required <= set(item)
                or not set(item) <= required | {'min_exclusive', 'step'}
                or not isinstance(item['label'], str) or not 0 < len(item['label']) <= 256
                or item['unit'] != ('rad/s' if name.endswith('speed_rad_s') else
                                   'rad/s^2' if name.endswith('acceleration_rad_s2') else
                                   'x' if name.endswith('_arm_scale') else 'rad')
                or ('min_exclusive' in item and type(item['min_exclusive']) is not bool)):
            raise ValueError('invalid session settings editor metadata')
        for key in ('min', 'max', 'default', 'step'):
            if key not in item:
                continue
            try:
                finite = type(item[key]) in (int, float) and math.isfinite(item[key])
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError('finite numeric session settings metadata required')
        if (item['min'] > item['max'] or item['default'] != defaults[name]
                or not item['min'] <= item['default'] <= item['max']
                or (item.get('min_exclusive', False) and item['default'] == item['min'])
                or ('step' in item and item['step'] <= 0)):
            raise ValueError('session settings editor bounds/default mismatch')
    return copy.deepcopy(schema)


def compatible_requirements(status_or_contract, required_contract=None):
    """Explicit new-client support for v1 installations and bounded v2 settings."""
    requirements = validate_requirements(required_contract)
    value = status_or_contract
    if isinstance(value, dict) and 'session_contract' in value:
        value = value['session_contract']
    if (isinstance(value, dict) and value.get('protocol') in CONFIGURABLE_SESSION_PROTOCOLS
            and requirements['protocol'] == SESSION_PROTOCOL):
        requirements['protocol'] = value['protocol']
        arm = MOTION_ARM_CAPABILITY if value['protocol'] == MOTION_SESSION_PROTOCOL else CONFIGURABLE_ARM_CAPABILITY
        requirements['capabilities'] = sorted(arm if x == ARM_CAPABILITY else x
                                               for x in requirements['capabilities'])
    if isinstance(value, dict) and TOGGLE_RETURN_CAPABILITY in value.get('capabilities', []):
        requirements['capabilities'] = sorted(TOGGLE_RETURN_CAPABILITY if x == RETURN_CAPABILITY else x
                                             for x in requirements['capabilities'])
    return requirements


def validate_requirements(value=None):
    value = copy.deepcopy(DEFAULT_REQUIREMENTS if value is None else value)
    if not isinstance(value, dict) or set(value) != {'protocol', 'capabilities'}:
        raise ValueError('exact supported contract requirements required')
    if value['protocol'] not in (SESSION_PROTOCOL, *CONFIGURABLE_SESSION_PROTOCOLS):
        raise ValueError('unsupported session protocol')
    caps = value['capabilities']
    arm = (ARM_CAPABILITY if value['protocol'] == SESSION_PROTOCOL else
           MOTION_ARM_CAPABILITY if value['protocol'] == MOTION_SESSION_PROTOCOL else CONFIGURABLE_ARM_CAPABILITY)
    known = {arm, GRIPPER_CAPABILITY, RETURN_CAPABILITY, TOGGLE_RETURN_CAPABILITY, BASE_CAPABILITY}
    if (not isinstance(caps, list) or any(not isinstance(x, str) for x in caps)
            or len(caps) != len(set(caps)) or not set(caps) <= known
            or not {arm, GRIPPER_CAPABILITY} <= set(caps)
            or {RETURN_CAPABILITY, TOGGLE_RETURN_CAPABILITY} <= set(caps)):
        raise ValueError('supported arm/gripper contract capabilities required')
    value['capabilities'] = sorted(caps)
    return value


def _check_template(value):
    configurable = isinstance(value, dict) and value.get('protocol') in CONFIGURABLE_SESSION_PROTOCOLS
    fields = _TEMPLATE_FIELDS | ({'session_settings'} if configurable else set())
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('exact session contract fields required')
    if value['protocol'] not in (SESSION_PROTOCOL, *CONFIGURABLE_SESSION_PROTOCOLS):
        raise ValueError('unsupported session protocol')
    profile = value['profile']
    expected_fields = set(_PROFILE) | {'gripper_config_sha256'}
    if isinstance(profile, dict) and 'return_home_config_sha256' in profile:
        expected_fields.add('return_home_config_sha256')
    if isinstance(profile, dict) and 'base_mode' in profile:
        expected_fields.add('base_mode')
    if (not isinstance(profile, dict) or set(profile) != expected_fields
            or any(profile.get(k) != v for k, v in _PROFILE.items())
            or not _hex(profile.get('gripper_config_sha256'), 64)
            or ('return_home_config_sha256' in profile
                and not _hex(profile['return_home_config_sha256'], 64))
            or profile.get('base_mode', 'disabled') not in ('disabled', 'vendor_vr_x')):
        raise ValueError('unsupported installed motion profile')
    arm = MOTION_ARM_CAPABILITY if value['protocol'] == MOTION_SESSION_PROTOCOL else CONFIGURABLE_ARM_CAPABILITY
    caps = [arm if configurable else ARM_CAPABILITY, GRIPPER_CAPABILITY]
    expected = dict(_BASE_READY, gripper_config_sha256=profile['gripper_config_sha256'])
    if configurable:
        snapshot = validate_settings_snapshot(value['session_settings'])
        expected_protocol = MOTION_SETTINGS_PROTOCOL if value['protocol'] == MOTION_SESSION_PROTOCOL else SETTINGS_PROTOCOL
        if snapshot['protocol'] != expected_protocol:
            raise ValueError('settings protocol does not match the session protocol')
        expected.update(_settings_ready(snapshot))
    if 'return_home_config_sha256' in profile:
        toggle = TOGGLE_RETURN_CAPABILITY in value['capabilities']
        caps.append(TOGGLE_RETURN_CAPABILITY if toggle else RETURN_CAPABILITY)
        expected.update(return_home_enabled=True,
            return_home_config_sha256=profile['return_home_config_sha256'],
            return_home_buttons=dict(TOGGLE_RETURN_BUTTONS if toggle else LEGACY_RETURN_BUTTONS))
    else:
        expected.update(return_home_enabled=False, return_home_config_sha256=None)
    if profile.get('base_mode') == 'vendor_vr_x':
        if TOGGLE_RETURN_CAPABILITY not in caps:
            raise ValueError('integrated base requires Y-toggle return capability')
        caps.append(BASE_CAPABILITY)
        expected.update(JOINT_BASE_READY)
    if (value['capabilities'] != sorted(caps)
            or value['ready_expectations'] != expected
            or type(value['shared_bus_compatibility_verified']) is not bool):
        raise ValueError('unsupported capabilities or execution semantics')
    return copy.deepcopy(value)


def prepare_service_contract(bundle, session_settings=None):
    """Build public contract from an installation already verified by bootstrap.

    Identity/config hashes bind this descriptor to that immutable installation.
    V2 additionally exposes the bounded numeric settings for operator review;
    CAN paths and implementation source remain on the robot.
    """
    profile = {key: bundle.get(key) for key in _PROFILE}
    if 'base_mode' in bundle:
        profile['base_mode'] = bundle['base_mode']
    profile['gripper_config_sha256'] = bundle.get('gripper_config_sha256')
    caps = [ARM_CAPABILITY, GRIPPER_CAPABILITY]
    ready = dict(_BASE_READY, gripper_config_sha256=profile['gripper_config_sha256'])
    settings = {}
    if session_settings is not None:
        snapshot = validate_settings_snapshot(session_settings)
        ready.update(_settings_ready(snapshot))
        caps[0] = MOTION_ARM_CAPABILITY if snapshot['protocol'] == MOTION_SETTINGS_PROTOCOL else CONFIGURABLE_ARM_CAPABILITY
        settings['session_settings'] = snapshot
    if bundle.get('return_home_config') is not None:
        profile['return_home_config_sha256'] = bundle.get('return_home_config_sha256')
        # The installed core declares the actual button behavior. Older cores
        # retain Y/B; do not advertise a toggle merely because service code is new.
        buttons = dict(LEGACY_RETURN_BUTTONS)
        source = bundle.get('sources', {}).get('teleop.robots.kuavo5_return_config')
        if source is not None:
            declarations = [node for node in ast.parse(source).body if isinstance(node, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == 'RETURN_BUTTONS' for t in node.targets)]
            if len(declarations) != 1:
                raise ValueError('installed return button declaration missing or ambiguous')
            buttons = ast.literal_eval(declarations[0].value)
            if buttons not in (LEGACY_RETURN_BUTTONS, TOGGLE_RETURN_BUTTONS):
                raise ValueError('unsupported installed return buttons')
        caps.append(TOGGLE_RETURN_CAPABILITY if buttons == TOGGLE_RETURN_BUTTONS else RETURN_CAPABILITY)
        ready.update(return_home_enabled=True,
            return_home_config_sha256=profile['return_home_config_sha256'],
            return_home_buttons=dict(buttons))
    else:
        ready.update(return_home_enabled=False, return_home_config_sha256=None)
    if bundle.get('base_mode') == 'vendor_vr_x':
        caps.append(BASE_CAPABILITY)
        ready.update(JOINT_BASE_READY)
    preparation = bundle.get('gripper_config', {}).get('preparation', {})
    verified = preparation.get('shared_bus_compatibility_verified', False)
    protocol = (MOTION_SESSION_PROTOCOL if settings and snapshot['protocol'] == MOTION_SETTINGS_PROTOCOL
                else CONFIGURABLE_SESSION_PROTOCOL if settings else SESSION_PROTOCOL)
    return _check_template(dict(protocol=protocol,
        capabilities=sorted(caps),
        profile=profile, ready_expectations=ready,
        shared_bus_compatibility_verified=verified, **settings))


def bind_service_contract(template, *, service_instance_id, installation_sha256, bundle_sha256):
    value = _check_template(template)
    if not _hex(service_instance_id, 32) or any(not _hex(v, 64)
            for v in (installation_sha256, bundle_sha256)):
        raise ValueError('complete service installation identity required')
    if 'session_settings' in value and any(value['session_settings'][k] != v for k, v in (
            ('installation_sha256', installation_sha256), ('bundle_sha256', bundle_sha256))):
        raise ValueError('session settings belong to a different installation')
    value.update(service_instance_id=service_instance_id,
                 installation_sha256=installation_sha256, bundle_sha256=bundle_sha256)
    value['contract_sha256'] = _digest(value)
    return value


def validate_contract(contract, required_contract=None):
    requirements = compatible_requirements(contract, required_contract)
    fields = _TEMPLATE_FIELDS | ({'session_settings'} if isinstance(contract, dict)
                                and contract.get('protocol') in CONFIGURABLE_SESSION_PROTOCOLS else set())
    if (not isinstance(contract, dict)
            or set(contract) != fields | _IDENTITY_FIELDS | {'contract_sha256'}):
        raise ValueError('versioned service contract unavailable')
    expected = bind_service_contract({k: contract[k] for k in fields},
                                    **{k: contract[k] for k in _IDENTITY_FIELDS})
    if contract != expected:
        raise ValueError('service contract integrity mismatch')
    if (requirements['protocol'] != contract['protocol']
            or not set(requirements['capabilities']) <= set(contract['capabilities'])):
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


def validate_pinned_settings_contract(status, *, expected_installation_sha256,
                                     expected_bundle_sha256, expected_profile):
    """Allow v2 parameter derivation within exactly one reviewed installation.

    The immutable client pins still select code, posture, recovery and return
    logic. Only the gripper configuration digest may vary with validated runtime
    settings; it comes from the authenticated, integrity-checked v2 descriptor.
    """
    from .kuavo5_session_profile import validate_profile
    profile = validate_profile(expected_profile)
    if any(not _hex(value, 64) for value in (expected_installation_sha256, expected_bundle_sha256)):
        raise ValueError('pinned installation and bundle required for runtime settings')
    protocol = (status.get('session_contract') or {}).get('protocol')
    if protocol not in CONFIGURABLE_SESSION_PROTOCOLS:
        raise ValueError('configurable session protocol required')
    capabilities = [MOTION_ARM_CAPABILITY if protocol == MOTION_SESSION_PROTOCOL else CONFIGURABLE_ARM_CAPABILITY,
                    GRIPPER_CAPABILITY]
    if 'return_home_config_sha256' in profile:
        capabilities.append(RETURN_CAPABILITY)
    contract = validate_service_contract(status,
        dict(protocol=protocol, capabilities=capabilities))
    if (contract['installation_sha256'] != expected_installation_sha256
            or contract['bundle_sha256'] != expected_bundle_sha256):
        raise ValueError('runtime settings cannot change pinned installation or code bundle')
    actual = contract['profile']
    if (set(actual) != set(profile) or any(actual[key] != value for key, value in profile.items()
                                         if key != 'gripper_config_sha256')):
        raise ValueError('runtime settings cannot change fixed profile or return configuration')
    return contract


def validate_ready(packet, contract):
    """Verify concrete worker readiness before the first PICO reply is sent."""
    contract = validate_contract(contract, compatible_requirements(contract, dict(protocol=SESSION_PROTOCOL,
        capabilities=[ARM_CAPABILITY, GRIPPER_CAPABILITY])))
    if not isinstance(packet, dict) or packet.get('event') != 'ready':
        raise ValueError('worker READY required')
    for key, expected in contract['ready_expectations'].items():
        actual = packet.get(key, False if key == 'return_home_enabled' else None)
        if actual != expected or (isinstance(expected, bool) and type(actual) is not bool):
            raise ValueError('worker READY differs from service contract: '+key)
        if key == 'session_settings_revision' and type(actual) is not int:
            raise ValueError('worker settings revision must be an integer')
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
    contract = validate_contract(contract, compatible_requirements(contract, dict(protocol=SESSION_PROTOCOL,
        capabilities=[ARM_CAPABILITY, GRIPPER_CAPABILITY])))
    value = dict(mode='execute', sha256=contract['bundle_sha256'], **contract['profile'])
    value['gripper_config'] = {'preparation': {'shared_bus_compatibility_verified':
                                             contract['shared_bus_compatibility_verified']}}
    if {RETURN_CAPABILITY, TOGGLE_RETURN_CAPABILITY} & set(contract['capabilities']):
        value['return_home_config'] = {}  # presence only; the robot owns all parameters
        value['return_home_buttons'] = dict(contract['ready_expectations']['return_home_buttons'])
    value['service_contract'] = contract
    return value
