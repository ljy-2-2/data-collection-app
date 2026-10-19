"""API-to-wire verification with an in-memory CAN bus; never opens hardware."""
import json
import struct
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'teleop_control/src'))
from teleop_control import tianji
from teleop_control.main import MockController
from teleop_control.tianji_models import TianjiParameters
from teleop_control.tianji_worker import gripper_configuration
from gripper.damiao import Driver, Feedback
from gripper.runtime import Control
from gripper.config import travel_bounds


def wire_trace(parameters, side, monkeypatch):
    motors = gripper_configuration(parameters, (side,))['motors']
    motor = motors[side]
    assert motor['control_mode'] == 4
    frames = []
    class MemoryBus:
        def send(self, can_id, payload):
            frames.append((can_id, payload))
        def recv(self):
            return None
    driver = Driver(MemoryBus(), motors)
    # Synthetic disabled-feedback/preflight state; no register queries are sent.
    driver.current_limits[side] = .1
    lo, hi = travel_bounds(motor)
    opened = lo if motor['open_rad'] < motor['closed_rad'] else hi
    driver.feedback[side] = Feedback(opened, 0, 0, 25, 25, 0, 100.)
    control = Control(motors, driver, .2)
    clock = [100.]
    monkeypatch.setattr('gripper.runtime.time.monotonic', lambda: clock[0])
    def tick(trigger):
        clock[0] += .01
        f = driver.feedback[side]
        f.received = clock[0]
        # Simulated feedback follows the state of the previously accepted enable.
        if control.states[side]['state'] in ('ENABLING', 'ACTIVE'):
            f.state = 1
        control.step(dict(valid=True, emergency=False, context=(1, 0),
                          submitted=clock[0], received=clock[0], triggers={side: trigger}), clock[0])
    tick(0.)
    tick(1.)
    tick(1.)
    assert control.states[side]['state'] == 'ACTIVE'
    frames.clear()
    velocities = []
    for _ in range(8):
        tick(1.)
        velocities.append(control.states[side]['command_velocity_rad_s'])
    decoded = [struct.unpack('<fHH', payload) for can_id, payload in frames]
    assert len(decoded) == 8
    assert all(can_id == 0x300 + motor['motor_id'] for can_id, _ in frames)
    assert all(lo <= position <= hi for position, _, _ in decoded)
    assert all(speed / 100 == parameters[side + '_gripper_speed'] for _, speed, _ in decoded)
    previous = 0.
    for velocity in velocities:
        assert abs(velocity - previous) <= parameters[side + '_gripper_accel'] * .01 + 1e-9
        previous = velocity
    assert control.status()['motors'][side]['accel_rad_s2'] == parameters[side + '_gripper_accel']
    return decoded


def test_saved_ui_parameters_reach_start_move_and_change_real_driver_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(tianji, 'RUNTIME', tmp_path)
    manager = tianji.TianjiManager(MockController())
    monkeypatch.setattr(manager, 'require_other_idle', lambda: None)
    requests = []
    def command(action, payload):
        requests.append((action, payload))
        return {'accepted': True}
    monkeypatch.setattr(manager, 'command', command)
    app = FastAPI()
    app.include_router(tianji.tianji_router(manager))
    selected = TianjiParameters(left_gripper_accel=10, right_gripper_accel=80,
                               left_gripper_speed=7, right_gripper_speed=9).model_dump()
    with TestClient(app) as client:
        saved = client.post('/tianji/parameters', json=selected)
        assert saved.status_code == 200, saved.text
        assert client.get('/tianji/parameters').json() == selected
        assert json.loads((tmp_path / 'parameters.json').read_text())['parameters'] == selected
        assert tianji.TianjiManager(MockController()).parameters().model_dump() == selected
        started = client.post('/tianji/start', json=dict(task='offline fixture', parameters=saved.json()))
        assert started.status_code == 200, started.text
        assert requests[-1] == ('start', dict(task='offline fixture', mode='dual', upload_mode=None, parameters=selected))
        started_parameters = requests[-1][1]['parameters']
        moved = client.post('/tianji/move', json=dict(kind='gripper', side='left', closure=.5))
        assert moved.status_code == 200, moved.text
        assert requests[-1][1]['parameters'] == selected
        before = {s: wire_trace(started_parameters, s, monkeypatch) for s in ('left', 'right')}
        selected['left_gripper_accel'] = 40
        assert client.post('/tianji/parameters', json=selected).status_code == 200
        client.post('/tianji/start', json=dict(task='offline fixture', parameters=client.get('/tianji/parameters').json()))
        after = {s: wire_trace(requests[-1][1]['parameters'], s, monkeypatch) for s in ('left', 'right')}
    assert before['right'] == after['right']  # One side's edit cannot affect the other.
    assert before['left'][0][0] != after['left'][0][0]
    assert before['left'][-1][0] != after['left'][-1][0]
    # Retired gain settings cannot be submitted as if they controlled mode 4.
    assert 'kp' not in started_parameters and 'kd' not in started_parameters


def test_active_session_refuses_parameter_save(tmp_path, monkeypatch):
    monkeypatch.setattr(tianji, 'RUNTIME', tmp_path)
    manager = tianji.TianjiManager(MockController())
    monkeypatch.setattr(manager, 'snapshot', lambda: {'state': 'running'})
    with pytest.raises(ValueError, match='停止控制'):
        manager.save_parameters(TianjiParameters(left_gripper_accel=30))
    assert not (tmp_path / 'parameters.json').exists()
