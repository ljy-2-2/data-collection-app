"""Configuration propagation only: no robot/driver is instantiated."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'teleop_control/src'), str(ROOT / 'scripts')]
from teleop_control import tianji_gripper_config as calibration
from teleop_control.tianji_models import TianjiParameters
from teleop_control.tianji_worker import gripper_configuration
from qt_ui import bridge


@pytest.fixture
def gripper_file(tmp_path, monkeypatch):
    data = json.loads(calibration.GRIPPER_CONFIG.read_text())
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(calibration, 'GRIPPER_CONFIG', path)
    return path


def change(path, **fields):
    data = json.loads(path.read_text())
    for side in ('left', 'right'):
        data['motors'][side].update(fields)
    path.write_text(json.dumps(data))
    return data


def test_changed_file_reaches_ui_model_and_actual_worker(gripper_file):
    change(gripper_file, speed_rad_s=22, kp=95, kd=3.5, accel_rad_s2=170)
    parameters = TianjiParameters()
    ui = bridge.initial_state()['parameters']
    assert ui == parameters.model_dump()
    actual = gripper_configuration(ui, ('left', 'right'), path=gripper_file)
    source = json.loads(gripper_file.read_text())
    for side in ('left', 'right'):
        assert actual['motors'][side] == source['motors'][side]
        assert ui[side + '_gripper_speed'] == 22
    assert ui['kp'] == 95 and ui['kd'] == 3.5
    # New instances re-read the file rather than use an import-time constant.
    change(gripper_file, speed_rad_s=19, kd=2.5)
    assert TianjiParameters().left_gripper_speed == 19
    assert bridge.initial_state()['parameters']['kd'] == 2.5


def test_current_requested_values_reach_worker_and_travel_is_retained(gripper_file):
    original = json.loads(gripper_file.read_text())
    parameters = TianjiParameters().model_dump()
    actual = gripper_configuration(parameters, ('left', 'right'), path=gripper_file)
    assert actual['motors'] == original['motors']


def test_operator_overrides_remain_effective_and_do_not_replace_calibration(gripper_file):
    parameters = TianjiParameters(left_gripper_speed=7, right_gripper_speed=9, kp=60, kd=1.5)
    actual = gripper_configuration(parameters.model_dump(), ('left', 'right'), path=gripper_file)
    source = json.loads(gripper_file.read_text())
    for side, speed in (('left', 7), ('right', 9)):
        motor = actual['motors'][side]
        assert (motor['speed_rad_s'], motor['kp'], motor['kd']) == (speed, 60, 1.5)
        for key in ('open_rad', 'closed_rad', 'accel_rad_s2', 'max_current_a', 'travel_margin_rad'):
            assert motor[key] == source['motors'][side][key]


@pytest.mark.parametrize('gain', ['kp', 'kd'])
def test_unequal_shared_gains_are_rejected_instead_of_choosing_one_side(gripper_file, gain):
    source = json.loads(gripper_file.read_text())
    source['motors']['right'][gain] = source['motors']['left'][gain] / 2
    gripper_file.write_text(json.dumps(source))
    with pytest.raises(ValueError, match='不一致'):
        TianjiParameters()
    with pytest.raises(ValueError, match='不一致'):
        bridge.initial_state()


def test_speeds_can_differ_per_side(gripper_file):
    source = json.loads(gripper_file.read_text())
    source['motors']['left']['speed_rad_s'] = 11
    source['motors']['right']['speed_rad_s'] = 17
    gripper_file.write_text(json.dumps(source))
    params = TianjiParameters()
    assert (params.left_gripper_speed, params.right_gripper_speed) == (11, 17)


@pytest.mark.parametrize('side', ['left', 'right'])
def test_existing_single_gripper_config_does_not_create_an_absent_motor(gripper_file, side):
    source = json.loads(gripper_file.read_text())
    source['motors'] = {side: source['motors'][side]}
    gripper_file.write_text(json.dumps(source))
    params = TianjiParameters()
    assert getattr(params, side + '_gripper_speed') == source['motors'][side]['speed_rad_s']
    assert bridge.initial_state()['parameters'] == params.model_dump()
    assert gripper_configuration(params.model_dump(), ('left', 'right'), path=gripper_file)['motors'] == source['motors']


def test_legacy_saved_values_do_not_override_new_calibration(gripper_file):
    legacy = dict(scale=.7, left_gripper_speed=10, right_gripper_speed=10, kp=80, kd=2)
    restored = TianjiParameters.model_validate(calibration.stored_parameters(legacy))
    expected = calibration.gripper_parameter_defaults()
    assert restored.scale == .7
    for key, value in expected.items():
        assert getattr(restored, key) == value


def test_current_saved_override_survives_but_config_change_resets_only_gripper(gripper_file):
    selected = TianjiParameters(scale=.8, left_gripper_speed=6, right_gripper_speed=8, kd=1.5)
    stored = calibration.parameter_storage(selected.model_dump())
    assert TianjiParameters.model_validate(calibration.stored_parameters(stored)) == selected
    # Formatting alone does not invalidate an explicit operator setting.
    gripper_file.write_text(json.dumps(json.loads(gripper_file.read_text()), indent=4))
    assert TianjiParameters.model_validate(calibration.stored_parameters(stored)) == selected
    change(gripper_file, speed_rad_s=24, kd=4.5)
    updated = TianjiParameters.model_validate(calibration.stored_parameters(stored))
    assert (updated.left_gripper_speed, updated.right_gripper_speed, updated.kd) == (24, 24, 4.5)
    assert updated.scale == .8


def test_manager_storage_loads_current_and_migrates_stale_parameters(gripper_file, tmp_path, monkeypatch):
    from teleop_control import tianji
    from teleop_control.main import MockController
    monkeypatch.setattr(tianji, 'RUNTIME', tmp_path / 'runtime')
    manager = tianji.TianjiManager(MockController())
    selected = TianjiParameters(scale=.6, left_gripper_speed=5)
    manager.save_parameters(selected)
    assert manager.parameters() == selected
    change(gripper_file, speed_rad_s=21)
    assert manager.parameters().left_gripper_speed == 21
    assert manager.parameters().scale == .6
    assert not manager.active()
