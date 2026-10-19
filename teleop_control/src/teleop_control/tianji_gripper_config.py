"""Read calibrated gripper defaults without importing a hardware driver."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from .tianji_config import installation_id

GRIPPER_CONFIG = Path(__file__).resolve().parents[2] / 'vendor/tianji/teleop/gripper/config.json'
GRIPPER_PARAMETERS = ('left_gripper_speed', 'right_gripper_speed',
                      'left_gripper_accel', 'right_gripper_accel')
GRIPPER_ENDPOINTS = tuple(f'{side}_gripper_{endpoint}_rad'
                          for side in ('left', 'right') for endpoint in ('open', 'closed'))
GRIPPER_ANGLE_LIMIT = math.pi - .02


def gripper_parameter_defaults() -> dict[str, float]:
    motors = json.loads(GRIPPER_CONFIG.read_text())['motors']
    if not isinstance(motors, dict) or not motors or not set(motors) <= {'left', 'right'}:
        raise ValueError('夹爪配置必须包含 left 和／或 right')
    result = {}
    for side in ('left', 'right'):
        if side not in motors:
            # A missing physical side keeps the
            # previous conservative placeholder; no motor is added to the
            # worker configuration and no command is sent to that absent side.
            result[side + '_gripper_speed'] = 10
            result[side + '_gripper_accel'] = 20
            result[side + '_gripper_open_rad'] = 0
            result[side + '_gripper_closed_rad'] = 1
            continue
        value = motors[side]['speed_rad_s']
        if type(value) not in (int, float) or not math.isfinite(value) or not .01 <= value <= 100:
            raise ValueError(f'{side}.speed_rad_s 必须为 0.01～100 的有效数值')
        result[side + '_gripper_speed'] = value
        accel = motors[side].get('accel_rad_s2', 20.0)
        if type(accel) not in (int, float) or not math.isfinite(accel) or not .1 <= accel <= 200:
            raise ValueError(f'{side}.accel_rad_s2 必须为 0.1～200 的有效数值')
        result[side + '_gripper_accel'] = accel
        for endpoint in ('open', 'closed'):
            result[f'{side}_gripper_{endpoint}_rad'] = motors[side][endpoint + '_rad']
    return result


def validate_gripper_endpoints(values: dict) -> None:
    motors = json.loads(GRIPPER_CONFIG.read_text())['motors']
    for side in ('left', 'right'):
        opened = values[f'{side}_gripper_open_rad']
        closed = values[f'{side}_gripper_closed_rad']
        if any(type(value) not in (float, int) or not math.isfinite(value)
               or abs(value) >= GRIPPER_ANGLE_LIMIT for value in (opened, closed)):
            raise ValueError(f'{side} 夹爪角度必须在启动单圈范围内')
        margin = motors.get(side, {}).get('travel_margin_rad', .02)
        if not math.isfinite(margin) or margin < .002 or abs(closed - opened) < .01 or abs(closed - opened) <= 2 * margin:
            raise ValueError(f'{side} 夹爪开闭行程必须大于两倍安全余量且不少于 0.01 rad')


def _replace_config(content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=GRIPPER_CONFIG.parent, prefix='.gripper-', delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), GRIPPER_CONFIG.stat().st_mode & 0o777)
            output.write(content)
        temporary.replace(GRIPPER_CONFIG)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def synchronized_gripper_endpoints(values: dict):
    validate_gripper_endpoints(values)
    original = GRIPPER_CONFIG.read_bytes()
    config = json.loads(original)
    changed = False
    for side, motor in config['motors'].items():
        for endpoint in ('open', 'closed'):
            key = endpoint + '_rad'
            value = values[f'{side}_gripper_{key}']
            changed |= motor[key] != value
            motor[key] = value
    if changed:
        _replace_config((json.dumps(config, indent=2) + '\n').encode())
    try:
        yield
    except Exception:
        if changed:
            _replace_config(original)
        raise


def gripper_parameter_default(key: str) -> float:
    return gripper_parameter_defaults()[key]


def gripper_config_fingerprint() -> str:
    # Whitespace-only edits must not discard the operator's explicit settings.
    content = json.loads(GRIPPER_CONFIG.read_text())
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def stored_parameters(payload: dict) -> dict:
    """Keep explicit settings only while they refer to this gripper setup."""
    if not isinstance(payload, dict):
        raise ValueError('天机参数必须为 JSON 对象')
    if 'parameters' in payload:
        if not isinstance(payload['parameters'], dict):
            raise ValueError('天机 parameters 必须为 JSON 对象')
        values = dict(payload['parameters'])
        current = payload.get('gripper_config_sha256') == gripper_config_fingerprint()
        if (not current and installation_id() in (
                'physical_left_a_right_b_verified_jog_v7_20261020',
                'physical_left_a_right_b_shoulder_half_turn_v8_20261020',
                'physical_left_a_right_b_gripper_half_turn_v9_20261020')
                and payload.get('installation_id') == 'physical_left_b_right_a_relabel_v6_20261020'):
            previous = json.loads(GRIPPER_CONFIG.read_text())
            motors = previous['motors']
            if set(motors) == {'left', 'right'}:
                previous['motors'] = {'left': motors['right'], 'right': motors['left']}
                previous_hash = hashlib.sha256(json.dumps(previous, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                if payload.get('gripper_config_sha256') == previous_hash:
                    original = dict(values)
                    for side, peer in (('left', 'right'), ('right', 'left')):
                        for suffix in ('speed', 'accel', 'open_rad', 'closed_rad'):
                            key, source = f'{side}_gripper_{suffix}', f'{peer}_gripper_{suffix}'
                            values.pop(key, None)
                            if source in original:
                                values[key] = original[source]
                    current = True
    else:
        values = dict(payload)
        current = False  # Legacy files cannot prove their calibrated source.
    # Retired UI gains must not invalidate otherwise valid saved settings.
    # The driver's per-motor configuration retains its own MIT gains.
    values.pop('kp', None)
    values.pop('kd', None)
    for key in GRIPPER_ENDPOINTS:
        values.pop(key, None)
    if not current:
        for key in GRIPPER_PARAMETERS:
            values.pop(key, None)
    return values


def parameter_storage(values: dict) -> dict:
    return {'installation_id': installation_id(), 'gripper_config_sha256': gripper_config_fingerprint(), 'parameters': values}
