"""Explicit return-home configuration; reading/validation performs no device I/O.

The wheel controller's runtime /robot_init_state_param is authoritative, not
kuavo.json init_arm_pos or an invented all-zero pose. Joint-range validation
does not perform collision checking or grant hardware motion permission.
"""
import hashlib
import json
import math
from pathlib import Path


RETURN_BUTTONS = {'request': 'left_y', 'cancel': 'left_y'}
DEFAULT_RETURN_CONFIG = {
    'schema_version': 1,
    'pose_source': 'robot_init_state_param',
    'gripper_target': 'session_initial_feedback',
    'max_velocity_rad_s': .3,
    'max_acceleration_rad_s2': .6,
    'timeout_s': 120.,
    'position_tolerance_rad': .03,
    'quiet_velocity_rad_s': .05,
    'settle_seconds': .3,
}


def _number(value, low, high, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(name+' outside the return-home range')
    return float(value)


def load_return_config(value):
    if isinstance(value, (str, Path)):
        value = json.loads(Path(value).read_text(encoding='utf-8'))
    if not isinstance(value, dict) or set(value) != set(DEFAULT_RETURN_CONFIG):
        raise ValueError('return-home configuration has missing or unknown fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('unsupported return-home schema')
    for field in ('pose_source', 'gripper_target'):
        if value[field] != DEFAULT_RETURN_CONFIG[field]:
            raise ValueError('unsupported return-home '+field)
    result = dict(value)
    for field, low, high in (
            ('max_velocity_rad_s', .05, .3),
            ('max_acceleration_rad_s2', .1, .6),
            ('timeout_s', 5., 120.),
            ('position_tolerance_rad', .005, .03),
            ('quiet_velocity_rad_s', .005, .05),
            ('settle_seconds', .2, 2.)):
        result[field] = _number(value[field], low, high, field)
    # Match ArmReturnState's coupled braking reserve before creating SSH or
    # robot resources. Individually valid speed/acceleration may still conflict.
    speed, acceleration = result['max_velocity_rad_s'], result['max_acceleration_rad_s2']
    if .06+speed*speed/(2*acceleration)+speed*.02 >= .2:
        raise ValueError('return-home lacks tracking-bound braking reserve')
    return result


def return_config_digest(config):
    return hashlib.sha256(json.dumps(load_return_config(config),
        sort_keys=True, allow_nan=False).encode()).hexdigest()


def resolve_vendor_return(config, robot_init_state, schema):
    """Extract exactly the v63 wheel arm segment, in native left7/right7 order.

    Runtime identity checks establish v63 before this function is called.
    Floating base7 + lower body4 precede arms14; head2 follows. Never command
    these other joints, and never call the vendor whole-body reset action.
    """
    from teleop.robots.kuavo5_native_ik import ARM_NAMES
    config = load_return_config(config)
    if tuple(schema.names) != ARM_NAMES:
        raise ValueError('return-home requires audited left7/right7 arm schema')
    if not isinstance(robot_init_state, (list, tuple)) or len(robot_init_state) != 27 \
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in robot_init_state):
        raise ValueError('/robot_init_state_param must contain 27 finite v63 wheel values')
    targets = dict(zip(ARM_NAMES, map(float, robot_init_state[11:25])))
    for joint in schema.joints:
        if not joint.position_min <= targets[joint.name] <= joint.position_max:
            raise ValueError('vendor return target exceeds joint limit: '+joint.name)
    return dict(targets=targets, **{k: config[k] for k in (
        'max_velocity_rad_s', 'max_acceleration_rad_s2', 'timeout_s',
        'position_tolerance_rad', 'quiet_velocity_rad_s', 'settle_seconds')})
