"""Validated session settings derived from one installed robot configuration.

Pure policy: no files, sockets, ROS or device access. The original calibration,
reference checks, mechanical endpoints and source bundle remain authoritative.
"""
import copy
import hashlib
import json
import math
import re
import struct

from teleop.control.kuavo5_gripper_calibration import load_runtime_config
from teleop.robots.kuavo5_session_profile import bundle_profile
from teleop.robots.kuavo5_service_contract import validate_settings_values


SETTINGS_PROTOCOL = 'kuavo_session_settings_v2'
ARM_SPEED_MAX = 1.
ARM_SPEED_DEFAULT = .7
ARM_ACCELERATION_MAX = 10.
ARM_ACCELERATION_DEFAULT = 5.
_SNAPSHOT_FIELDS = frozenset(('protocol', 'installation_sha256', 'bundle_sha256',
                              'revision', 'values', 'settings_sha256'))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def _number(value, name):
    if type(value) not in (int, float):
        raise ValueError(name + ' must be a finite number')
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(name + ' must be a finite number') from exc
    if not math.isfinite(number):
        raise ValueError(name + ' must be a finite number')
    return number


def _speed(value, name):
    value = _number(value, name)
    if not .01 <= value <= 15.:
        raise ValueError(name + ' must be within 0.01..15 rad/s')
    ticks = round(value*100)
    if not math.isclose(value*100, ticks, rel_tol=0., abs_tol=1e-9):
        raise ValueError(name + ' requires increments of 0.01 rad/s; no truncation')
    return ticks/100.


def _wire_speed(value):
    ticks = round(value*100)
    result = value
    # The existing mode4 codec uses int(speed * 100). Correct only the binary
    # representation of e.g. 0.29; its wire value must stay 29, never 28.
    if int(result*100) != ticks:
        bits = struct.unpack('<Q', struct.pack('<d', result))[0]
        result = struct.unpack('<d', struct.pack('<Q', bits+1))[0]
    if int(result*100) != ticks:
        raise ValueError('speed is not representable by the current mode4 codec')
    return result


def _configuration(bundle):
    if not isinstance(bundle, dict) or not _hash(bundle.get('sha256')):
        raise ValueError('installed bundle identity required for session settings')
    profile = bundle_profile(bundle)
    if (profile.get('target_recovery') != 'bounded_candidate'
            or profile.get('speed_profile') != 'responsive'
            or profile.get('gripper_mode') != 'damiao'):
        raise ValueError('session settings require the installed bounded dual-arm and damiao profile')
    configured = load_runtime_config(bundle['gripper_config'])
    if set(configured.profiles) != {'left', 'right'} or any(
            p.control_mode != 4 for p in configured.profiles.values()):
        raise ValueError('session settings require both commissioned mode4 grippers')
    return configured


def _defaults(configured):
    values = {}
    for side in ('left', 'right'):
        values.update({side+'_arm_scale': 1., side+'_arm_speed_rad_s': ARM_SPEED_DEFAULT,
            side+'_arm_acceleration_rad_s2': ARM_ACCELERATION_DEFAULT})
    for side, profile in configured.profiles.items():
        values[side+'_gripper_speed_rad_s'] = _speed(profile.speed_rad_s, side+' speed')
        values[side+'_gripper_acceleration_rad_s2'] = profile.accel_rad_s2
        values[side+'_gripper_open_target_rad'] = profile.target(0.)
        values[side+'_gripper_closed_target_rad'] = profile.target(1.)
    return values


def settings_defaults(bundle):
    return _defaults(_configuration(bundle))


def settings_schema(bundle):
    configured = _configuration(bundle)
    defaults = _defaults(configured)
    schema = {}
    for side, label in (('left', '左臂'), ('right', '右臂')):
        schema[side+'_arm_scale'] = dict(label=label+'位置移动比例', unit='x',
            min=.1, max=2., default=1.)
        schema[side+'_arm_speed_rad_s'] = dict(label=label+'关节速度上限', unit='rad/s',
            min=0., min_exclusive=True, max=ARM_SPEED_MAX, default=ARM_SPEED_DEFAULT)
        schema[side+'_arm_acceleration_rad_s2'] = dict(label=label+'关节参考规划加速度上限',
            unit='rad/s^2', min=0., min_exclusive=True, max=ARM_ACCELERATION_MAX,
            default=ARM_ACCELERATION_DEFAULT)
    for side, label in (('left', '左夹爪'), ('right', '右夹爪')):
        profile = configured.profiles[side]
        speed = side+'_gripper_speed_rad_s'
        schema[speed] = dict(label=label+'开合速度上限', unit='rad/s', min=.01,
                             max=defaults[speed], default=defaults[speed], step=.01)
        accel = side+'_gripper_acceleration_rad_s2'
        schema[accel] = dict(label=label+'开合参考规划加速度上限', unit='rad/s^2',
            min=0., min_exclusive=True, max=defaults[accel], default=defaults[accel])
        reserve = .01 if profile.position_recovery else 0.
        low, high = profile.bounds
        for endpoint, description in (('open', '张开'), ('closed', '闭合')):
            key = side+'_gripper_'+endpoint+'_target_rad'
            schema[key] = dict(label=label+description+'基准目标角（上电偏移自动换算）', unit='rad',
                               min=low+reserve, max=high-reserve, default=defaults[key])
    return schema


def _validated(bundle, values):
    configured = _configuration(bundle)
    defaults = _defaults(configured)
    if not isinstance(values, dict) or set(values) != set(defaults):
        raise ValueError('exact complete session settings fields required')
    normalized = {key: _number(value, key) for key, value in values.items()}
    validate_settings_values(normalized)
    document = copy.deepcopy(configured.document)
    for side, profile in configured.profiles.items():
        speed_key = side+'_gripper_speed_rad_s'
        speed = _speed(normalized[speed_key], speed_key)
        if speed > defaults[speed_key]:
            raise ValueError(speed_key + ' exceeds the installed speed limit')
        normalized[speed_key] = speed
        raw = document['motors'][side]['profile']
        # Keep user/snapshot values at their exact requested hundredth. Only
        # the effective document compensates for binary rounding in the existing
        # int(speed * 100) encoder, so e.g. 0.29 really encodes 29 rather than 28.
        wire_speed = _wire_speed(speed)
        if wire_speed != profile.speed_rad_s:
            raw['speed_rad_s'] = wire_speed
        accel_key = side+'_gripper_acceleration_rad_s2'
        if normalized[accel_key] > defaults[accel_key]:
            raise ValueError(accel_key+' exceeds the installed acceleration limit')
        if normalized[accel_key] != profile.accel_rad_s2:
            raw['accel_rad_s2'] = normalized[accel_key]
        direction = 1. if profile.closed_rad > profile.open_rad else -1.
        low, high = profile.bounds
        minimum_reserve = .01 if profile.position_recovery else 0.
        for endpoint, fraction in (('open', 0.), ('closed', 1.)):
            key = side+'_gripper_'+endpoint+'_target_rad'
            wanted = normalized[key]
            if not low+minimum_reserve <= wanted <= high-minimum_reserve:
                raise ValueError(key + ' exceeds commissioned soft travel or endpoint reserve')
            if wanted == profile.target(fraction):
                continue  # Preserve exact baseline values and its config digest.
            anchor = profile.open_rad if endpoint == 'open' else profile.closed_rad
            sign = direction if endpoint == 'open' else -direction
            reserve = sign*(wanted-anchor)-profile.margin_rad
            if math.isclose(reserve, minimum_reserve, rel_tol=0., abs_tol=1e-12):
                reserve = minimum_reserve
            raw[endpoint+'_target_reserve_rad'] = reserve
        opened = normalized[side+'_gripper_open_target_rad']
        closed = normalized[side+'_gripper_closed_target_rad']
        original_travel = abs(profile.target(1.)-profile.target(0.))
        minimum_travel = min(original_travel, document['motors'][side]['calibration'].get(
            'minimum_travel_rad', math.radians(5.)))
        if direction*(closed-opened) < minimum_travel:
            raise ValueError(side + ': working targets reverse direction or leave insufficient travel')
    # Reuse commissioning, identities, measured-mark and native envelope checks.
    effective = load_runtime_config(document)
    for side, profile in effective.profiles.items():
        for endpoint, fraction in (('open', 0.), ('closed', 1.)):
            key = side+'_gripper_'+endpoint+'_target_rad'
            # The existing reserve and linear mapping can differ by a floating
            # ULP. Keep the requested value stable across preview/save/reload;
            # refuse a material mismatch instead of rewriting it each time.
            if not math.isclose(normalized[key], profile.target(fraction), rel_tol=0., abs_tol=1e-12):
                raise ValueError(key + ' cannot be represented by the installed target mapping')
    return normalized, document


def validate_settings(bundle, values):
    return _validated(bundle, values)[0]


def apply_settings(bundle, values):
    normalized, document = _validated(bundle, values)
    result = copy.deepcopy(bundle)
    result['gripper_config'] = document
    # Retain the existing canonical encoding used for gripper config identities.
    result['gripper_config_sha256'] = hashlib.sha256(json.dumps(
        document, sort_keys=True, allow_nan=False).encode()).hexdigest()
    result['runtime_arm_motion_parameters'] = {side: dict(
        scale=normalized[side+'_arm_scale'],
        speed_rad_s=normalized[side+'_arm_speed_rad_s'],
        acceleration_rad_s2=normalized[side+'_arm_acceleration_rad_s2'])
        for side in ('left', 'right')}
    return result


def make_snapshot(bundle, installation_sha, revision, values):
    if not _hash(installation_sha) or type(revision) is not int or revision < 0:
        raise ValueError('valid installation identity and nonnegative settings revision required')
    normalized = validate_settings(bundle, values)
    result = dict(protocol=SETTINGS_PROTOCOL, installation_sha256=installation_sha,
                  bundle_sha256=bundle.get('sha256'), revision=revision,
                  values=normalized)
    result['settings_sha256'] = _digest(result)
    return result


def validate_snapshot(bundle, installation_sha, snapshot):
    if not isinstance(bundle, dict):
        raise ValueError('installed bundle required')
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
        raise ValueError('exact session settings snapshot required')
    if (snapshot['protocol'] != SETTINGS_PROTOCOL
            or snapshot['installation_sha256'] != installation_sha
            or snapshot['bundle_sha256'] != bundle.get('sha256')):
        raise ValueError('session settings snapshot belongs to another installation or bundle')
    checked = make_snapshot(bundle, installation_sha, snapshot['revision'], snapshot['values'])
    if checked != snapshot:
        raise ValueError('session settings snapshot integrity or normalization mismatch')
    return checked
