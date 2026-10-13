"""Read calibrated gripper defaults without importing a hardware driver."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

GRIPPER_CONFIG = Path(__file__).resolve().parents[2] / 'vendor/tianji/teleop/gripper/config.json'
GRIPPER_PARAMETERS = ('left_gripper_speed', 'right_gripper_speed', 'kp', 'kd')


def gripper_parameter_defaults() -> dict[str, float]:
    motors = json.loads(GRIPPER_CONFIG.read_text())['motors']
    if not isinstance(motors, dict) or not motors or not set(motors) <= {'left', 'right'}:
        raise ValueError('夹爪配置必须包含 left 和／或 right')
    result = {}
    for side in ('left', 'right'):
        if side not in motors:
            # The page has two speed fields. A missing physical side keeps the
            # previous conservative placeholder; no motor is added to the
            # worker configuration and no command is sent to that absent side.
            result[side + '_gripper_speed'] = 10
            continue
        value = motors[side]['speed_rad_s']
        if type(value) not in (int, float) or not math.isfinite(value) or not .01 <= value <= 100:
            raise ValueError(f'{side}.speed_rad_s 必须为 0.01～100 的有效数值')
        result[side + '_gripper_speed'] = value
    for key in ('kp', 'kd'):
        values = [motor[key] for motor in motors.values()]
        value = values[0]
        if any(other != value for other in values[1:]):
            raise ValueError(f'左右夹爪 {key} 不一致；当前页面使用共用参数，请明确配置相同值')
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f'夹爪 {key} 必须为有效数值')
        if not (0 <= value <= 500 if key == 'kp' else 0 < value <= 5):
            raise ValueError(f'夹爪 {key} 超出页面可设置范围')
        result[key] = value
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
    if not current:
        for key in GRIPPER_PARAMETERS:
            values.pop(key, None)
    return values


def parameter_storage(values: dict) -> dict:
    return {'gripper_config_sha256': gripper_config_fingerprint(), 'parameters': values}
