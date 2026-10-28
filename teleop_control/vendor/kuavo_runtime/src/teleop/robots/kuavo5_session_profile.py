"""Fixed network profiles and config identity; stdlib only, no device access."""
import hashlib
import json
import re
import stat
from pathlib import Path

ARM_PROFILE = dict(target_recovery='bounded_candidate', speed_profile='responsive',
                   arm_posture='elbow_preference', gripper_mode='off')


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def validate_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError('fixed network profile required')
    expected = set(ARM_PROFILE)
    if 'base_mode' in profile:
        expected.add('base_mode')
        if profile['base_mode'] not in ('disabled', 'vendor_vr_x'):
            raise ValueError('unsupported installed base mode')
        if profile['base_mode'] == 'vendor_vr_x' and (
                profile.get('gripper_mode') != 'damiao' or 'return_home_config_sha256' not in profile):
            raise ValueError('joint base mode requires the combined gripper and return profile')
    if profile.get('gripper_mode') == 'damiao':
        expected.add('gripper_config_sha256')
        if not valid_digest(profile.get('gripper_config_sha256')):
            raise ValueError('pinned gripper config digest required')
        if 'return_home_config_sha256' in profile:
            expected.add('return_home_config_sha256')
            if not valid_digest(profile['return_home_config_sha256']):
                raise ValueError('pinned return-home config digest required')
    elif profile.get('gripper_mode') != 'off':
        raise ValueError('network supports grippers off or damiao only')
    if set(profile) != expected or any(profile.get(k) != v for k, v in ARM_PROFILE.items()
                                      if k != 'gripper_mode'):
        raise ValueError('network requires saved 0.7 bounded elbow-preference profile')
    return dict(profile)


def bundle_profile(bundle, *, return_config_validator=None):
    profile = {key: bundle.get(key) for key in ARM_PROFILE}
    if 'base_mode' in bundle:
        profile['base_mode'] = bundle['base_mode']
    if profile['gripper_mode'] == 'damiao':
        document = bundle.get('gripper_config')
        if not isinstance(document, dict):
            raise ValueError('installed gripper configuration required')
        digest = hashlib.sha256(json.dumps(document, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if digest != bundle.get('gripper_config_sha256'):
            raise ValueError('gripper configuration integrity mismatch')
        profile['gripper_config_sha256'] = digest
    elif 'gripper_config' in bundle or 'gripper_config_sha256' in bundle:
        raise ValueError('gripper configuration requires damiao profile')
    if 'standalone_return' in bundle:
        raise ValueError('standalone return-home is not part of the network teleoperation profile')
    return_fields = {key for key in bundle if key.startswith('return_home')}
    if return_fields:
        if return_fields != {'return_home_config', 'return_home_config_sha256'}:
            raise ValueError('exact installed return-home configuration and digest required')
        if profile['gripper_mode'] != 'damiao':
            raise ValueError('return-home requires the calibrated damiao profile')
        document = bundle['return_home_config']
        if not isinstance(document, dict):
            raise ValueError('installed return-home configuration required; paths are not accepted')
        if return_config_validator is None:
            from teleop.robots.kuavo5_return_config import load_return_config
            return_config_validator = load_return_config
        checked = return_config_validator(document)
        digest = hashlib.sha256(json.dumps(checked, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if document != checked or digest != bundle['return_home_config_sha256']:
            raise ValueError('return-home configuration integrity mismatch')
        profile['return_home_config_sha256'] = digest
    return validate_profile(profile)


def require_root_owned(path):
    """Every component must be immutable to the unprivileged listener/operator."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute protected installation required')
    for component in (path, *path.parents):
        info = component.lstat()
        if (info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode)
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise PermissionError('root-owned non-writable installation required: '+str(component))
