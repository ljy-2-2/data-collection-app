"""Local working-endpoint tuning. No transport, device or parameter writes."""
from copy import deepcopy
from datetime import datetime, timezone
from teleop.control.kuavo5_gripper_calibration import load_runtime_config, save_document
from teleop.robots.damiao_gripper import finite


def explicit_endpoint_config(document):
    """Name both endpoint reserves explicitly, preserving legacy effective values."""
    config = load_runtime_config(document)
    result = deepcopy(document)
    for side, p in config.profiles.items():
        profile = result['motors'][side]['profile']
        opened, closed = p.endpoint_reserves
        profile.pop('target_reserve_rad', None)
        profile['open_target_reserve_rad'] = opened
        profile['closed_target_reserve_rad'] = closed
    load_runtime_config(result)
    return result


def tune_closing(document, reserves, *, source_label='', source_sha256=''):
    """Change closing reserves only, at most 0.01 rad per explicit candidate."""
    before = load_runtime_config(document)
    if not reserves or not set(reserves) <= set(before.profiles):
        raise ValueError('specify left and/or right closing reserve')
    candidate = explicit_endpoint_config(document)
    for side, reserve in reserves.items():
        reserve = finite(reserve, side + ' closed reserve')
        p = before.profiles[side]
        if p.control_mode != 4:
            raise ValueError('closing tuning requires previously prepared mode 4')
        if abs(reserve-p.endpoint_reserves[1]) > .010000000001:
            raise ValueError('one candidate may change closing reserve by at most 0.01 rad')
        candidate['motors'][side]['profile']['closed_target_reserve_rad'] = reserve
    load_runtime_config(candidate)
    # Editable working values have one source of truth: motors.*.profile.
    # Do not copy them into metadata that becomes stale after a manual edit.
    candidate.pop('target_policy', None)
    candidate['closing_tuning'] = dict(
        created_utc=datetime.now(timezone.utc).isoformat(), source=source_label,
        source_sha256=source_sha256, physical_validation_completed=False,
        historical_readbacks_preserved=True, current_robot_readiness_verified=False)
    return candidate


def save_candidate(candidate, path):
    """Publish a complete NEW config atomically; never replace existing evidence."""
    load_runtime_config(candidate)
    return save_document(path, candidate)
