"""Read calibrated gripper defaults without importing a hardware driver."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

GRIPPER_CONFIG = Path(__file__).resolve().parents[2] / 'vendor/tianji/teleop/gripper/config.json'
GRIPPER_PARAMETERS = ('left_gripper_speed', 'right_gripper_speed',
                      'left_gripper_accel', 'right_gripper_accel')


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
            continue
        value = motors[side]['speed_rad_s']
        if type(value) not in (int, float) or not math.isfinite(value) or not .01 <= value <= 100:
            raise ValueError(f'{side}.speed_rad_s 必须为 0.01～100 的有效数值')
        result[side + '_gripper_speed'] = value
        accel = motors[side].get('accel_rad_s2', 20.0)
        if type(accel) not in (int, float) or not math.isfinite(accel) or not .1 <= accel <= 200:
            raise ValueError(f'{side}.accel_rad_s2 必须为 0.1～200 的有效数值')
        result[side + '_gripper_accel'] = accel
    return result


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
    else:
        values = dict(payload)
        current = False  # Legacy files cannot prove their calibrated source.
    # Retired UI gains must not invalidate otherwise valid saved settings.
    # The driver's per-motor configuration retains its own MIT gains.
    values.pop('kp', None)
    values.pop('kd', None)
    if not current:
        for key in GRIPPER_PARAMETERS:
            values.pop(key, None)
    return values


def parameter_storage(values: dict) -> dict:
    return {'gripper_config_sha256': gripper_config_fingerprint(), 'parameters': values}
