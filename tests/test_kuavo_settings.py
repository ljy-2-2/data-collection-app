"""Configuration tests use temporary directories and no robot connections."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control.kuavo import KuavoManager, Options
from teleop_control.kuavo_settings import BASELINE, ConnectionSettings, GripperSettings, LocalSettings, digest
from teleop_control.kuavo_worker import require_gripper_config


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket,'connect',Mock(side_effect=AssertionError('no network')))
    return KuavoManager(SimpleNamespace(hardware_enabled=False),root=tmp_path)


def values(manager):
    return copy.deepcopy(manager.snapshot()['gripper_settings']['values'])


def test_save_real_validator_preserves_evidence_and_survives_restart(manager):
    original=manager.settings.baseline
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest()=='3bc2e41d773a8aaa6a61f7fc334fbbe53cab36139bba63a2e65e4fb94ad1f225'
    changed=values(manager)
    changed['left'].update(open_rad=-2.5,closed_rad=-1.2,speed_rad_s=9.,accel_rad_s2=100.)
    changed['right'].update(open_rad=-2.2,closed_rad=-.8,speed_rad_s=8.,accel_rad_s2=90.)
    status=manager.save_settings(GripperSettings.model_validate(changed))['gripper_settings']
    assert status['saved'] and not status['matches_service'] and '待机器人端应用' in status['message']
    document=json.loads(Path(status['file']).read_text())
    assert document==manager.settings.document
    assert Path(status['file']).stat().st_mode&0o777==0o600
    for side in ('left','right'):
        assert document['motors'][side]['calibration']==original['motors'][side]['calibration']
        assert document['motors'][side]['readback']==original['motors'][side]['readback']
        assert document['motors'][side]['open_endpoint_source']=='operator_adjusted'
        for key,value in original['motors'][side]['profile'].items():
            if key not in changed[side]:assert document['motors'][side]['profile'][key]==value
    reloaded=LocalSettings(manager.root)
    assert not reloaded.error and reloaded.snapshot({})['values']==changed
    assert reloaded.expected_gripper_hash()==digest(document)
    manager.save_settings(GripperSettings.model_validate({s:{k:original['motors'][s]['profile'][k] for k in changed[s]} for s in changed}))
    assert manager.settings.document==original


@pytest.mark.parametrize('key,value',[('speed_rad_s',0),('speed_rad_s',16),('accel_rad_s2',201),('open_rad',float('nan')),('open_rad',3.13),('closed_rad',True),('bus','canbus9')])
def test_invalid_parameter_never_saved(manager,key,value):
    changed=values(manager);changed['right'][key]=value
    with pytest.raises(ValueError):manager.save_settings(GripperSettings.model_validate(changed))
    assert not manager.settings.gripper_path.exists()


def test_reversed_or_insufficient_travel_preserves_saved_config(manager):
    changed=values(manager);manager.save_settings(GripperSettings.model_validate(changed))
    before=manager.settings.gripper_path.read_bytes()
    for closed in (-2.8,-2.65):
        changed['left']['closed_rad']=closed
        with pytest.raises(ValueError):manager.save_settings(GripperSettings.model_validate(changed))
        assert manager.settings.gripper_path.read_bytes()==before


def test_original_validator_runs_with_network_forbidden():
    root=BASELINE.parents[3]
    code='''
import socket,sys
def forbidden(*a,**kw): raise AssertionError('No network in offline validation')
socket.socket.connect=forbidden
socket.create_connection=forbidden
from teleop_control.kuavo_worker import main
sys.argv=['worker','validate-grippers']
raise SystemExit(main())
'''
    result=subprocess.run([sys.executable,'-c',code],input=json.dumps(json.loads(BASELINE.read_text())),text=True,capture_output=True,
        env={**os.environ,'PYTHONPATH':str(root/'teleop_control/src')},timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_pending_config_requires_matching_authenticated_service(manager):
    manager.save_settings(GripperSettings.model_validate(values(manager)))
    expected=manager.settings.expected_gripper_hash()
    contract={'profile':{'gripper_config_sha256':expected}}
    options={'expected_gripper_sha256':expected}
    require_gripper_config(contract,options)
    with pytest.raises(ValueError,match='尚未应用'):require_gripper_config({'profile':{'gripper_config_sha256':'0'*64}},options)
    manager.value['service']={'management_ready':True,'service':contract}
    assert manager.snapshot()['gripper_settings']['matches_service']
    manager.value['state']='unknown'
    assert not manager.snapshot()['gripper_settings']['matches_service']
    manager.value['state']='idle';manager.value['service']['management_ready']=False
    assert not manager.snapshot()['gripper_settings']['matches_service']


def connection_values(**changes):
    return ConnectionSettings(host='192.168.26.1',port=7443,certificate_sha256='a'*64,token='b'*64,**changes)


def test_connection_is_portable_private_and_does_not_echo_token(manager,monkeypatch):
    monkeypatch.setattr(subprocess,'run',Mock(side_effect=AssertionError('no child for connection save')))
    manager.value['service']={'management_ready':True}
    result=manager.save_settings(connection_values())
    assert 'b'*64 not in json.dumps(result)
    assert result['connection']['configured'] and not result['service']
    path=manager.settings.connection_path
    config=json.loads(path.read_text()); token=path.parent/config['network_token_file']
    assert not Path(config['network_token_file']).is_absolute()
    assert path.stat().st_mode&0o777==token.stat().st_mode&0o777==0o600
    assert token.read_text().strip()=='b'*64
    assert Options().network_namespace=='current'
    assert LocalSettings(manager.root).connection_snapshot()['host']=='192.168.26.1'


def test_bad_credentials_and_busy_save_leave_previous_files(manager):
    manager.save_settings(connection_values())
    before=manager.settings.connection_path.read_bytes()
    invalid=connection_values().model_copy(update={'certificate_sha256':'incorrect'})
    with pytest.raises(ValueError,match='指纹'):manager.save_settings(invalid)
    assert manager.settings.connection_path.read_bytes()==before
    manager.pending_operation=True
    with pytest.raises(RuntimeError):manager.save_settings(connection_values())
    with pytest.raises(RuntimeError):manager.save_settings(GripperSettings.model_validate(values(manager)))
    assert manager.settings.connection_path.read_bytes()==before


def test_tampered_config_is_visible_and_blocks_start(manager,monkeypatch):
    manager.save_settings(GripperSettings.model_validate(values(manager)))
    document=json.loads(manager.settings.gripper_path.read_text())
    document['motors']['left']['profile']['max_current_a']=20
    manager.settings.gripper_path.write_text(json.dumps(document))
    manager.settings=LocalSettings(manager.root)
    assert manager.snapshot()['gripper_settings']['error']
    monkeypatch.setattr(subprocess,'Popen',Mock(side_effect=AssertionError('must not launch')))
    with pytest.raises(ValueError,match='本地夹爪配置'):manager.start({'confirm_motion':True})
