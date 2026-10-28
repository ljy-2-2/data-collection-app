import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control.kuavo import KuavoManager

ROOT=Path(__file__).resolve().parents[1]

def test_manual_policy_and_owner_isolation_offline():
    result=subprocess.run([sys.executable,str(ROOT/'tests/fixtures/kuavo_manual_fake.py')],
        cwd=ROOT,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'manual offline checks:' in result.stdout


def manager(tmp_path):return KuavoManager(SimpleNamespace(hardware_enabled=False),root=tmp_path)

def test_old_service_cannot_start_or_fake_readiness(tmp_path,monkeypatch):
    m=manager(tmp_path);spawn=Mock();monkeypatch.setattr(subprocess,'Popen',spawn)
    assert len(m.snapshot()['manual']['joints'])==20
    m.value['service']={'service':{'backend':'robot'}}
    with pytest.raises(ValueError,match='更新'):m.manual.start()
    spawn.assert_not_called()


def test_manual_excludes_teleop_and_settings(tmp_path):
    m=manager(tmp_path)
    m.manual.process=SimpleNamespace(poll=lambda:None,stdin=io.StringIO())
    m.manual.session_id='owned';m.manual.state='ready'
    assert m.active() and not m.snapshot()['prestart_editable']
    with pytest.raises(RuntimeError):m.start({'confirm_motion':True})
    with pytest.raises(RuntimeError):m.inspect({})
    assert not m.snapshot()['control_ready']


@pytest.mark.parametrize('body',[
    dict(session_id='old',sequence=1,command={'action':'heartbeat'}),
    dict(session_id='owned',sequence=True,command={'action':'heartbeat'}),
    dict(session_id='owned',sequence=0,command={'action':'heartbeat'}),
    dict(session_id='owned',sequence=1,command={'action':'joint','joint':'waist_yaw_joint','delta_deg':10}),
    dict(session_id='owned',sequence=1,command={'action':'base','direction':'faster'}),
])
def test_invalid_or_stale_manual_request_never_reaches_worker(tmp_path,body):
    m=manager(tmp_path);sink=io.StringIO()
    m.manual.process=SimpleNamespace(poll=lambda:None,stdin=sink)
    m.manual.session_id='owned';m.manual.sequence=0;m.manual.state='ready'
    with pytest.raises(ValueError):m.manual.input(body)
    assert not sink.getvalue()


def test_button_sequence_and_stale_release_isolation(tmp_path):
    m=manager(tmp_path);sink=io.StringIO()
    m.manual.process=SimpleNamespace(poll=lambda:None,stdin=sink)
    m.manual.session_id='owned';m.manual.state='ready'
    m.manual.input(dict(session_id='owned',sequence=2,command=dict(action='base',direction='stop')))
    with pytest.raises(ValueError):m.manual.input(dict(session_id='owned',sequence=1,command=dict(action='base',direction='forward')))
    assert json.loads(sink.getvalue())==dict(action='base',direction='stop')
