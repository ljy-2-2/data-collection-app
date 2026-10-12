"""Fail closed when the selected grippers have no calibrated zero reference."""

from pathlib import Path
import sys

import pytest
import yaml


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from galaxis_bringup.launch_registry import with_encoder_calibration


def write_parameters(path, parameters):
    path.write_text(
        yaml.safe_dump({"encoder_node": {"ros__parameters": parameters}}),
        encoding="utf-8",
    )
    return path


def test_start_requires_persisted_calibration(tmp_path):
    with pytest.raises(RuntimeError, match="Calibrate the minimum angle"):
        with_encoder_calibration(
            [{"active_sides": ["left"], "left_min_raw": 0.0}],
            tmp_path / "missing.yaml",
        )


def test_current_profile_wins_over_saved_active_sides(tmp_path):
    runtime = write_parameters(tmp_path / "runtime.yaml", {"active_sides": ["right"]})
    calibration = write_parameters(
        tmp_path / "calibration.yaml",
        {
            "active_sides": ["left"],
            "left_min_raw": 3.0,
            "right_min_raw": 1.25,
            "right_rotation_direction": -1,
        },
    )
    params = [str(runtime), {"right_rotation_direction": 1}]

    actual = with_encoder_calibration(params, calibration)

    assert actual == params + [{"right_min_raw": 1.25}]
    assert len(params) == 2


def test_later_parameter_files_and_dicts_override_earlier_profile(tmp_path):
    first = write_parameters(tmp_path / "first.yaml", {"active_sides": ["right"]})
    second = write_parameters(tmp_path / "second.yaml", {"active_sides": ["left"]})
    calibration = write_parameters(tmp_path / "calibration.yaml", {"right_min_raw": 0.0})
    params = [first, str(second), {"active_sides": ["right"]}]

    assert with_encoder_calibration(params, calibration)[-1] == {"right_min_raw": 0.0}


def test_legacy_calibration_without_active_sides_accepts_zero(tmp_path):
    calibration = write_parameters(
        tmp_path / "calibration.yaml", {"left_min_raw": 0, "right_min_raw": 4.5}
    )

    assert with_encoder_calibration([], calibration) == [
        {"left_min_raw": 0.0, "right_min_raw": 4.5}
    ]


def test_single_side_calibration_cannot_start_default_dual_profile(tmp_path):
    calibration = write_parameters(tmp_path / "calibration.yaml", {"left_min_raw": 1.0})

    with pytest.raises(ValueError, match="right_min_raw"):
        with_encoder_calibration([], calibration)


@pytest.mark.parametrize("value", [None, True, "1.2", float("nan"), float("inf"), -float("inf")])
def test_rejects_invalid_selected_minimum(tmp_path, value):
    calibration = write_parameters(tmp_path / "calibration.yaml", {"left_min_raw": value})

    with pytest.raises(ValueError, match="left_min_raw"):
        with_encoder_calibration([{"active_sides": ["left"]}], calibration)


@pytest.mark.parametrize("active_sides", [[], ["left", "left"], ["center"], "left"])
def test_rejects_invalid_profile(tmp_path, active_sides):
    calibration = write_parameters(tmp_path / "calibration.yaml", {"left_min_raw": 1.0})

    with pytest.raises(ValueError, match="active_sides"):
        with_encoder_calibration([{"active_sides": active_sides}], calibration)


def test_malformed_saved_calibration_cannot_start(tmp_path):
    calibration = tmp_path / "calibration.yaml"
    calibration.write_text("encoder_node: [", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Cannot read encoder parameters"):
        with_encoder_calibration([{"active_sides": ["left"]}], calibration)
