"""Standard drawing axes are independent of earlier empirical mounting flips."""

from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from audit_tianji_drawing_directions import (
    DRAWING_WORLD_FROM_BASES,
    DRAWING_WORLD_FROM_MOUNT,
    MOUNT,
    NEUTRAL_WORLD_FROM_PICO,
    VENDOR,
    configuration,
    drawing_audit,
)
from marvin_teleop.geometry import relative_target, rotation
from marvin_teleop.mapping import same_facing_map


@pytest.mark.parametrize('side,roll', [('left', -np.pi / 2), ('right', np.pi / 2)])
def test_original_fixture_origins_match_drawing(side, roll):
    letter = 'L' if side == 'left' else 'R'
    origin = ET.parse(MOUNT).getroot().find(f"joint[@name='Joint0_{letter}']/origin")
    expected_position = [DRAWING_WORLD_FROM_BASES[side][1, 3], 0., .142]
    np.testing.assert_allclose(np.fromstring(origin.get('xyz'), sep=' '), expected_position, atol=1e-12)
    np.testing.assert_allclose(np.fromstring(origin.get('rpy'), sep=' '), [roll, 0., -np.pi / 2], atol=1e-12)
    np.testing.assert_allclose(DRAWING_WORLD_FROM_MOUNT[:3, :3] @ [0., -1., 0.], [1., 0., 0.])


@pytest.mark.parametrize('side,base_axes_in_world', [
    ('left', [[1., 0., 0.], [0., 0., -1.], [0., 1., 0.]]),
    ('right', [[1., 0., 0.], [0., 0., 1.], [0., -1., 0.]]),
])
def test_manufacturer_axes_are_not_bilateral_copies(side, base_axes_in_world):
    basis = DRAWING_WORLD_FROM_BASES[side][:3, :3]
    np.testing.assert_array_equal(basis.T, base_axes_in_world)
    assert np.linalg.det(basis) == pytest.approx(1.)


@pytest.mark.parametrize('yaw', [0., .7, -1.2])
@pytest.mark.parametrize('side', ['left', 'right'])
def test_drawing_translation_and_orientation_share_proper_vr_rotation(side, yaw):
    head = np.eye(4)
    head[:3, :3] = rotation([0., 1., 0.], yaw)
    basis = same_facing_map(head)
    np.testing.assert_allclose(basis, NEUTRAL_WORLD_FROM_PICO @ head[:3, :3].T, atol=1e-12)
    anchor = DRAWING_WORLD_FROM_BASES[side].copy()
    for world_axis in np.eye(3):
        for sign in (-1., 1.):
            tracking_axis = head[:3, :3] @ NEUTRAL_WORLD_FROM_PICO.T @ world_axis
            hand = np.eye(4)
            hand[:3, 3] = tracking_axis * sign * .01
            hand[:3, :3] = rotation(tracking_axis, sign * .1)
            target = relative_target(hand, np.eye(4), anchor, basis, 1.)
            np.testing.assert_allclose(target[:3, 3] - anchor[:3, 3], world_axis * sign * .01, atol=1e-12)
            np.testing.assert_allclose(target[:3, :3], rotation(world_axis, sign * .1) @ anchor[:3, :3], atol=1e-12)


@pytest.mark.parametrize('mount,overrides,expected', [
    ('cad', {}, {'left': True, 'right': True}),
    ('same_facing_y_half_turn', {'right': 'cad'}, {'left': False, 'right': True}),
    ('same_facing_y_half_turn', {}, {'left': False, 'right': False}),
])
def test_audit_reports_disagreement_without_applying_changes(mount, overrides, expected):
    config = configuration(VENDOR / 'teleop/marvin_teleop/config.json')
    config.update(mount_orientation=mount, mount_orientation_overrides=overrides)
    report = drawing_audit(config)
    assert {side: value['matches_standard_drawing'] for side, value in report['arms'].items()} == expected
    for side, matches in expected.items():
        assert report['arms'][side]['configured_rotation_difference_deg'] == pytest.approx(0. if matches else 180.)
    assert report['physical_installation_verified'] is False
    assert report['live_configuration_changed'] is False
    assert config['mount_orientation'] == mount
    assert config['mount_orientation_overrides'] == overrides
