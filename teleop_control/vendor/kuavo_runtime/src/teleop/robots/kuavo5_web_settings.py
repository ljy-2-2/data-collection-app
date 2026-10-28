"""Pure website degree/radian adapter; no network or device access.

Render settings_for_web(client.get_session_settings()). Convert edited fields
using changes_from_web before preview/apply. Retain the observed revision and
service/installation identities. Do not resubmit rounded, unedited fields.
"""
import copy
import math
from .kuavo5_service_contract import validate_settings_values

_ARM_UNITS = {side+suffix: side+replacement for side in ('left', 'right')
    for suffix, replacement in (('_arm_speed_rad_s', '_arm_speed_deg_s'),
                                ('_arm_acceleration_rad_s2', '_arm_acceleration_deg_s2'))}


def changes_from_web(changes):
    """Arms use degrees in the web editor; grippers keep explicit radian units."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError('nonempty edited website fields required')
    reverse = {v: k for k, v in _ARM_UNITS.items()}
    result = {}
    for name, value in changes.items():
        if name in _ARM_UNITS or name in ('arm_speed_rad_s', 'arm_acceleration_rad_s2'):
            raise ValueError('website arm fields require explicit degree units and side')
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('finite numeric website settings required')
        result[reverse.get(name, name)] = math.radians(value) if name in reverse else value
    return validate_settings_values(result, partial=True)


def settings_for_web(result):
    """Convert values/schema together; preserve editability and revision."""
    if not isinstance(result, dict) or result.get('ok') is not True:
        raise ValueError('successful settings response required')
    from .kuavo5_service_contract import validate_settings_snapshot, validate_settings_schema, MOTION_SETTINGS_PROTOCOL
    snapshot = validate_settings_snapshot(result.get('snapshot'))
    if snapshot['protocol'] != MOTION_SETTINGS_PROTOCOL:
        raise ValueError('per-arm settings service required for this website editor')
    schema = validate_settings_schema(result.get('schema'), result.get('defaults'))
    values = snapshot['values']
    web_values, web_schema = {}, {}
    for key, value in values.items():
        target = _ARM_UNITS.get(key, key)
        item = copy.deepcopy(schema[key])
        if key in _ARM_UNITS:
            value = math.degrees(value)
            for field in ('min', 'max', 'default', 'step'):
                if field in item:
                    item[field] = math.degrees(item[field])
            item['unit'] = 'deg/s^2' if key.endswith('acceleration_rad_s2') else 'deg/s'
        web_values[target], web_schema[target] = value, item
    return dict(values=web_values, schema=web_schema, editable=result.get('editable') is True,
        locked_reason=result.get('locked_reason'), revision=snapshot['revision'],
        settings_sha256=snapshot['settings_sha256'],
        service_instance_id=result.get('service_instance_id'),
        installation_sha256=result.get('installation_sha256'))
