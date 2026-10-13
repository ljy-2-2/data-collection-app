"""Preserve camera brightness confirmed in preview when applying UMI setup."""

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest
import yaml


HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "umi_collection/vendor/galaxis/tools/web_device_config.py"
)
SPEC = importlib.util.spec_from_file_location("umi_web_device_config", HELPER_PATH)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def parameters(settings, side):
    return settings[f"{side}_camera_brightness_controller"]["ros__parameters"]


@pytest.mark.parametrize("roles", [
    ("left", "right", "center"), ("left", "center"), ("right", "center"),
])
def test_persists_confirmed_hardware_brightness_for_selected_cameras(roles):
    requested = {side: value for side, value in zip(roles, (-32, 0, 143))}

    saved = yaml.safe_load(yaml.safe_dump(helper.build_camera_settings(roles, requested)))

    for side, value in requested.items():
        assert parameters(saved, side)["brightness"] == value
        assert type(parameters(saved, side)["brightness"]) is int
    inactive = {"left", "right", "center"} - set(roles)
    assert all(f"{side}_camera_brightness_controller" not in saved for side in inactive)


def test_keeps_other_side_flip_and_unknown_settings_without_mutating_source():
    existing = {
        "left_camera_brightness_controller": {
            "ros__parameters": {"brightness": 20, "startup_delay_sec": 2.5},
            "custom_node_setting": "keep",
        },
        "right_camera_brightness_controller": {"ros__parameters": {"brightness": 47}},
        "center_camera_brightness_controller": {
            "ros__parameters": {"brightness": -12, "max_attempts": 6}
        },
        "center_camera_flip": {"ros__parameters": {"vertical_flip": True, "other": 7}},
        "future_camera_setting": {"arbitrary": [1, 2]},
    }
    before = deepcopy(existing)

    saved = helper.build_camera_settings(("left", "center"), {"left": 91}, existing)

    expected = deepcopy(before)
    parameters(expected, "left")["brightness"] = 91
    assert saved == expected
    assert existing == before


def test_legacy_payload_keeps_existing_brightness_and_defaults_new_roles_to_zero():
    existing = {
        "left_camera_brightness_controller": {"ros__parameters": {"brightness": 58}}
    }

    saved = helper.build_camera_settings(("left", "center"), None, existing)

    assert parameters(saved, "left")["brightness"] == 58
    assert parameters(saved, "center")["brightness"] == 0
    assert saved["center_camera_flip"]["ros__parameters"]["vertical_flip"] is False


@pytest.mark.parametrize("value", [True, False, 3.0, 3.5, "3", None, [], {}])
def test_rejects_noninteger_brightness(value):
    with pytest.raises(ValueError, match="must be an integer"):
        helper.build_camera_settings(("left", "center"), {"left": value})


@pytest.mark.parametrize("brightness", [{"right": 7}, {"rear": 9}, [], 4, False])
def test_rejects_unselected_roles_and_invalid_brightness_maps(brightness):
    with pytest.raises(ValueError, match="active camera roles"):
        helper.build_camera_settings(("left", "center"), brightness)


@pytest.mark.parametrize("roles", [(), ("rear",), ("left", "left")])
def test_rejects_invalid_active_roles(roles):
    with pytest.raises(ValueError, match="invalid active camera roles"):
        helper.build_camera_settings(roles, {})


@pytest.mark.parametrize("existing", [
    [], {"left_camera_brightness_controller": None},
    {"left_camera_brightness_controller": {"ros__parameters": []}},
])
def test_rejects_malformed_saved_settings_instead_of_overwriting_them(existing):
    with pytest.raises(ValueError, match="invalid saved"):
        helper.build_camera_settings(("left", "center"), {"left": 7}, existing)
