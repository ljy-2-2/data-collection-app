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
        assert ui[side + '_gripper_accel'] == 170
    assert 'kp' not in ui and 'kd' not in ui
    # New instances re-read the file rather than use an import-time constant.
    change(gripper_file, speed_rad_s=19, kd=2.5)
    assert TianjiParameters().left_gripper_speed == 19
    assert bridge.initial_state()['parameters']['left_gripper_speed'] == 19


def test_current_requested_values_reach_worker_and_travel_is_retained(gripper_file):
    original = json.loads(gripper_file.read_text())
    parameters = TianjiParameters().model_dump()
    actual = gripper_configuration(parameters, ('left', 'right'), path=gripper_file)
    assert actual['motors'] == original['motors']


def test_operator_overrides_remain_effective_and_do_not_replace_calibration(gripper_file):
    parameters = TianjiParameters(left_gripper_speed=7, right_gripper_speed=9,
                                 left_gripper_accel=12, right_gripper_accel=35)
    actual = gripper_configuration(parameters.model_dump(), ('left', 'right'), path=gripper_file)
    source = json.loads(gripper_file.read_text())
    for side, speed in (('left', 7), ('right', 9)):
        motor = actual['motors'][side]
        assert motor['speed_rad_s'] == speed
        assert motor['accel_rad_s2'] == (12 if side == 'left' else 35)
        for key in ('open_rad', 'closed_rad', 'max_current_a', 'travel_margin_rad', 'kp', 'kd'):
            assert motor[key] == source['motors'][side][key]


@pytest.mark.parametrize('gain', ['kp', 'kd'])
def test_per_motor_driver_gains_are_preserved_without_exposing_ui_controls(gripper_file, gain):
    source = json.loads(gripper_file.read_text())
    source['motors']['right'][gain] = source['motors']['left'][gain] / 2
    gripper_file.write_text(json.dumps(source))
    params = TianjiParameters().model_dump()
    assert gain not in params and gain not in bridge.initial_state()['parameters']
    assert gain not in {field['key'] for field in bridge.FIELDS}
    actual = gripper_configuration(params, ('left', 'right'), path=gripper_file)
    for side in ('left', 'right'):
        assert actual['motors'][side][gain] == source['motors'][side][gain]


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
    selected = TianjiParameters(scale=.8, left_gripper_speed=6, right_gripper_speed=8,
                               left_gripper_accel=12, right_gripper_accel=35)
    stored = calibration.parameter_storage(selected.model_dump())
    assert TianjiParameters.model_validate(calibration.stored_parameters(stored)) == selected
    # Formatting alone does not invalidate an explicit operator setting.
    gripper_file.write_text(json.dumps(json.loads(gripper_file.read_text()), indent=4))
    assert TianjiParameters.model_validate(calibration.stored_parameters(stored)) == selected
    change(gripper_file, speed_rad_s=24, kd=4.5)
    updated = TianjiParameters.model_validate(calibration.stored_parameters(stored))
    assert (updated.left_gripper_speed, updated.right_gripper_speed) == (24, 24)
    assert updated.scale == .8
    assert updated.left_gripper_accel == updated.right_gripper_accel == 200


def test_existing_saved_parameters_gain_acceleration_without_losing_overrides(gripper_file):
    selected = TianjiParameters(left_gripper_speed=7, right_gripper_speed=9).model_dump()
    selected.pop('left_gripper_accel')
    selected.pop('right_gripper_accel')
    restored = TianjiParameters.model_validate(calibration.stored_parameters(calibration.parameter_storage(selected)))
    assert (restored.left_gripper_speed, restored.right_gripper_speed) == (7, 9)
    assert restored.left_gripper_accel == restored.right_gripper_accel == 200


def test_old_gain_fields_do_not_discard_saved_speed_acceleration_or_scale(gripper_file, tmp_path, monkeypatch):
    from teleop_control import tianji
    from teleop_control.main import MockController
    monkeypatch.setattr(tianji, 'RUNTIME', tmp_path)
    expected = TianjiParameters(scale=.6, left_gripper_speed=7, right_gripper_speed=9,
                               left_gripper_accel=12, right_gripper_accel=35)
    old = calibration.parameter_storage(dict(expected.model_dump(), kp=80, kd=4))
    path = tmp_path / 'parameters.json'
    path.write_text(json.dumps(old))
    manager = tianji.TianjiManager(MockController())
    assert manager.parameters() == expected
    manager.save_parameters(manager.parameters())
    saved = json.loads(path.read_text())['parameters']
    assert saved == expected.model_dump()
    assert 'kp' not in saved and 'kd' not in saved


@pytest.mark.parametrize('key', ['left_gripper_accel', 'right_gripper_accel'])
@pytest.mark.parametrize('value', [0, -1, 200.1, float('nan'), float('inf')])
def test_invalid_acceleration_cannot_be_saved(key, value):
    with pytest.raises(ValueError):
        TianjiParameters(**{key: value})


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


def test_endpoint_fields_read_current_config(gripper_file):
    source = change(gripper_file, open_rad=-.7, closed_rad=1.2)
    values = TianjiParameters().model_dump()
    ui = bridge.initial_state()['parameters']
    fields = {field['key']: field for field in bridge.FIELDS}
    for side in ('left', 'right'):
        for endpoint in ('open', 'closed'):
            key = f'{side}_gripper_{endpoint}_rad'
            assert values[key] == ui[key] == source['motors'][side][endpoint + '_rad']
            assert fields[key]['unit'] == 'rad'


def test_endpoint_save_updates_file_worker_and_reload(gripper_file, tmp_path, monkeypatch):
    from teleop_control import tianji
    from teleop_control.main import MockController
    monkeypatch.setattr(tianji, 'RUNTIME', tmp_path / 'runtime')
    original = json.loads(gripper_file.read_text())
    manager = tianji.TianjiManager(MockController())
    selected = TianjiParameters(left_gripper_speed=10, right_gripper_speed=10,
                               left_gripper_open_rad=-.75, left_gripper_closed_rad=.95,
                               right_gripper_open_rad=.25, right_gripper_closed_rad=1.6)
    manager.save_parameters(selected)
    updated = json.loads(gripper_file.read_text())
    expected = json.loads(json.dumps(original))
    for side in ('left', 'right'):
        for endpoint in ('open', 'closed'):
            expected['motors'][side][endpoint + '_rad'] = getattr(selected, f'{side}_gripper_{endpoint}_rad')
    assert updated == expected
    assert manager.parameters() == selected
    worker = gripper_configuration(manager.parameters().model_dump(), ('left', 'right'), path=gripper_file)
    for side in ('left', 'right'):
        assert worker['motors'][side]['open_rad'] == expected['motors'][side]['open_rad']
        assert worker['motors'][side]['closed_rad'] == expected['motors'][side]['closed_rad']
        assert worker['motors'][side]['speed_rad_s'] == 10
    stored = json.loads((tianji.RUNTIME / 'parameters.json').read_text())
    assert stored['gripper_config_sha256'] == calibration.gripper_config_fingerprint()
    change(gripper_file, open_rad=-.8)
    assert manager.parameters().left_gripper_open_rad == -.8


@pytest.mark.parametrize('key', calibration.GRIPPER_ENDPOINTS)
@pytest.mark.parametrize('value', [float('nan'), float('inf'), -calibration.GRIPPER_ANGLE_LIMIT,
                                   calibration.GRIPPER_ANGLE_LIMIT])
def test_invalid_endpoint_rejected(gripper_file, key, value):
    with pytest.raises(ValueError):
        TianjiParameters(**{key: value})


@pytest.mark.parametrize('side', ['left', 'right'])
def test_endpoint_travel_must_leave_safety_margin(gripper_file, side):
    with pytest.raises(ValueError, match='安全余量'):
        TianjiParameters(**{f'{side}_gripper_open_rad': 1, f'{side}_gripper_closed_rad': 1.03})
    selected = TianjiParameters(**{f'{side}_gripper_open_rad': 1, f'{side}_gripper_closed_rad': -.5})
    assert getattr(selected, f'{side}_gripper_open_rad') == 1


def test_endpoint_save_rolls_back_if_parameter_storage_fails(gripper_file, tmp_path, monkeypatch):
    from teleop_control import tianji
    from teleop_control.main import MockController
    monkeypatch.setattr(tianji, 'RUNTIME', tmp_path / 'runtime')
    manager = tianji.TianjiManager(MockController())
    manager.save_parameters(TianjiParameters(left_gripper_speed=10))
    original = gripper_file.read_bytes()
    stored = (tianji.RUNTIME / 'parameters.json').read_bytes()
    replace = Path.replace
    def fail_parameters(path, target):
        if path.name == 'parameters.tmp':
            raise PermissionError('test storage failure')
        return replace(path, target)
    monkeypatch.setattr(Path, 'replace', fail_parameters)
    with pytest.raises(PermissionError):
        manager.save_parameters(TianjiParameters(left_gripper_open_rad=-.8))
    assert gripper_file.read_bytes() == original
    assert (tianji.RUNTIME / 'parameters.json').read_bytes() == stored
    assert not (tianji.RUNTIME / 'parameters.tmp').exists()


@pytest.mark.parametrize('state', ['running', 'moving', 'checking', 'unknown'])
def test_endpoint_save_refuses_active_control(gripper_file, tmp_path, monkeypatch, state):
    from teleop_control import tianji
    from teleop_control.main import MockController
    monkeypatch.setattr(tianji, 'RUNTIME', tmp_path / 'runtime')
    manager = tianji.TianjiManager(MockController())
    monkeypatch.setattr(manager, 'snapshot', lambda: {'state': state})
    original = gripper_file.read_bytes()
    with pytest.raises(ValueError, match='停止控制'):
        manager.save_parameters(TianjiParameters(left_gripper_open_rad=-.8))
    assert gripper_file.read_bytes() == original
    assert not tianji.RUNTIME.exists()


def test_endpoint_save_error_does_not_claim_ui_success(gripper_file, tmp_path):
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    instance = bridge.Bridge({}, settings_path=tmp_path)
    instance.poll_timer.stop()
    try:
        instance.setParameter('left_gripper_open_rad', -.8)
        instance.save_timer.stop()
        previous = dict(instance.calibration)
        instance.request = lambda path, payload, done, **kwargs: kwargs['failed']('文件只读')
        instance.save_parameters()
        assert not instance.state['paramSaved']
        assert '文件只读' in instance.state['error']
        assert instance.calibration == previous
        instance.request = lambda path, payload, done, **kwargs: done(payload)
        instance.save_parameters()
        assert instance.state['paramSaved']
        assert instance.calibration['left'][0] == -.8
    finally:
        instance.save_timer.stop()
        instance.deleteLater()


def test_endpoint_api_saves_one_side_and_rejects_invalid_travel(gripper_file, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from teleop_control import tianji
    from teleop_control.main import MockController
    monkeypatch.setattr(tianji, 'RUNTIME', tmp_path / 'runtime')
    manager = tianji.TianjiManager(MockController())
    app = FastAPI()
    app.include_router(tianji.tianji_router(manager))
    original = json.loads(gripper_file.read_text())
    with TestClient(app) as client:
        selected = client.get('/tianji/parameters').json()
        selected['left_gripper_open_rad'] = -.8
        assert client.post('/tianji/parameters', json=selected).status_code == 200
        assert client.get('/tianji/parameters').json() == selected
        updated = json.loads(gripper_file.read_text())
        assert updated['motors']['left']['open_rad'] == -.8
        assert updated['motors']['right'] == original['motors']['right']
        selected['left_gripper_closed_rad'] = -.79
        assert client.post('/tianji/parameters', json=selected).status_code == 422
        assert json.loads(gripper_file.read_text()) == updated


def test_endpoint_sync_does_not_create_absent_motor(gripper_file):
    original = json.loads(gripper_file.read_text())
    del original['motors']['right']
    gripper_file.write_text(json.dumps(original))
    selected = TianjiParameters(left_gripper_open_rad=-.8)
    with calibration.synchronized_gripper_endpoints(selected.model_dump()):
        updated = json.loads(gripper_file.read_text())
        assert set(updated['motors']) == {'left'}
        assert updated['motors']['left']['open_rad'] == -.8


def test_scene_uses_updated_endpoint_mapping():
    from qt_ui.visuals import RobotScene
    scene = RobotScene(ROOT, load_model=False)
    status = {'grippers': {'motors': {'left': {'feedback': {'position_rad': .5, 'age_s': 0}}}}}
    scene.update_pose(status, {'left': (0, 1)})
    assert scene._pose_angles['gripper_drive_L'] == .5
    scene.update_pose(status, {'left': (0, 2)})
    assert scene._pose_angles['gripper_drive_L'] == .75
