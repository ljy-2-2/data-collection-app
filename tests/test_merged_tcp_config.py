"""The new portable config must keep existing named URDF tool frames."""
import json
import sys
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'teleop_control/src'))
sys.path.insert(0,str(ROOT/'teleop_control/vendor/tianji/teleop'))
from teleop_control import tianji_config
from marvin_teleop.configuration import configuration


def test_worker_dictionary_keeps_configured_tcp(monkeypatch):
    for name in ('TIANJI_CONFIG','TIANJI_ROBOT_IP','TELEOP_XR_SERVICE','TELEOP_XR_LAUNCHER'):
        monkeypatch.delenv(name,raising=False)
    direct=configuration(tianji_config.DEFAULT_CONFIG)
    portable=configuration(tianji_config.robot_configuration())
    for side,letter in [('left','L'),('right','R')]:
        assert portable['tool_tcp'][side]['name']==f'grasp_center_{letter}'
        np.testing.assert_allclose(portable['tool_tcp'][side]['xyz_mm'],direct['tool_tcp'][side]['xyz_mm'])
        np.testing.assert_allclose(portable['tool_tcp'][side]['rpy_deg'],direct['tool_tcp'][side]['rpy_deg'])


def test_custom_json_resolves_relative_urdf_against_its_directory(tmp_path,monkeypatch):
    config=json.loads(tianji_config.DEFAULT_CONFIG.read_text())
    (tmp_path/'tool.urdf').write_text('<robot name="tool"><link name="flange"/><link name="tcp"/>'
        '<joint name="tool" type="fixed"><parent link="flange"/><child link="tcp"/>'
        '<origin xyz="0.01 0.02 0.2" rpy="0 0 0"/></joint></robot>')
    config['tool_tcp']={side:dict(urdf='tool.urdf',flange_link='flange',tcp_link='tcp') for side in ('left','right')}
    path=tmp_path/'robot.json';path.write_text(json.dumps(config))
    monkeypatch.setenv('TIANJI_CONFIG',str(path))
    monkeypatch.chdir(tmp_path.parent)
    loaded=configuration(tianji_config.robot_configuration())
    for side in ('left','right'):
        assert loaded['tool_tcp'][side]['name']=='tcp'
        np.testing.assert_allclose(loaded['tool_tcp'][side]['xyz_mm'],[10,20,200])
