"""Loopback HTTPS with fake camera inventory; no robot hosts or hardware."""
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from teleop_control import kuavo_network_agent as agent,kuavo_network_client as client
from teleop_control.kuavo_joint_remote import SensorSampler

@pytest.fixture
def service(tmp_path,monkeypatch):
    key,cert=tmp_path/'key',tmp_path/'cert'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-keyout',str(key),
        '-out',str(cert),'-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True)
    token=tmp_path/'token';token.write_text('a'*64)
    inventory=Mock(return_value={'sources':[{'id':'usb:/dev/v4l/by-path/fake','path':'/dev/v4l/by-path/fake','kind':'usb',
        'capabilities':{'resolutions':[{'width':640,'height':480}],'brightness':{'min':-64,'max':64,'step':1}}}]})
    monkeypatch.setitem(sys.modules,'kuavo_camera_remote',SimpleNamespace(inventory=inventory))
    server=agent.AgentServer(dict(bind='127.0.0.1',port=0,cert_file=str(cert),key_file=str(key),token_file=str(token),joint_feedback=False))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    profile=dict(url='https://127.0.0.1:'+str(server.server_port),ca_file=str(cert),token_file=str(token))
    try:yield server,profile,inventory
    finally:server.shutdown();server.server_close();thread.join(3)

def fetch(profile,path,body=None):
    conn,response=client.request(profile,path,body)
    try:return json.loads(response.read())
    finally:conn.close()

def test_boot_status_auth_and_discovery_never_start_capture(service,monkeypatch,tmp_path):
    server,profile,inventory=service
    popen=Mock(side_effect=AssertionError('hardware opened'))
    monkeypatch.setattr(agent.subprocess,'Popen',popen)
    assert not inventory.called
    assert fetch(profile,'/status')==dict(version=1,capabilities=['usb_camera'],motion_control=False)
    assert not inventory.called
    bad=tmp_path/'bad';bad.write_text('b'*64)
    with pytest.raises(RuntimeError,match='未授权'):fetch({**profile,'token_file':str(bad)},'/discover')
    assert not inventory.called
    assert len(fetch(profile,'/discover')['sources'])==1
    with pytest.raises(RuntimeError,match='不提供关节反馈'):fetch(profile,'/joints')
    with pytest.raises(RuntimeError,match='未知接口'):fetch(profile,'/start')
    popen.assert_not_called()

@pytest.mark.parametrize('body',[{'source_id':'/bin/sh'},
    {'source_id':'usb:/dev/v4l/by-path/fake','profile':{'width':999,'height':480}},
    {'source_id':'usb:/dev/v4l/by-path/fake','profile':{'brightness':65}},
    {'source_id':'usb:/dev/v4l/by-path/fake','profile':{'command':'rm'}}])
def test_invalid_camera_requests_have_no_side_effects(service,monkeypatch,body):
    _,profile,_=service
    popen=Mock(side_effect=AssertionError('hardware opened'));monkeypatch.setattr(agent.subprocess,'Popen',popen)
    with pytest.raises(RuntimeError):fetch(profile,'/camera',body)
    popen.assert_not_called()

def test_single_owner_refuses_second_reader_without_starting_child(service,monkeypatch):
    server,profile,_=service;server.owners.add('/dev/v4l/by-path/fake')
    popen=Mock();monkeypatch.setattr(agent.subprocess,'Popen',popen)
    with pytest.raises(RuntimeError,match='已有连接'):fetch(profile,'/camera',{'source_id':'usb:/dev/v4l/by-path/fake'})
    popen.assert_not_called()

def test_sensor_sampler_preserves_real_100hz_and_fails_clock_rewind():
    sampler=SensorSampler('63')
    def message(ns):return SimpleNamespace(sensor_time=SimpleNamespace(secs=ns//10**9,nsecs=ns%10**9),
        joint_data=SimpleNamespace(joint_q=[ns],joint_v=[1]))
    samples=[row for ns in range(0,1_000_000_000,2_000_000) if (row:=sampler.receive(message(ns))) is not None]
    assert len(samples)==100 and [r['sequence'] for r in samples]==list(range(1,101))
    assert samples[1]['joint_q']==[10_000_000]
    assert sampler.receive(message(998_000_000)) is None
    with pytest.raises(ValueError,match='backwards'):sampler.receive(message(1))
    # A gap does not create replacement measurements.
    assert sampler.receive(message(2_000_000_000))['sequence']==101


def test_stream_disconnect_releases_only_owned_reader(service,monkeypatch):
    import time
    server,profile,_=service;real_popen=subprocess.Popen;children=[]
    def fake_capture(command,**kwargs):
        assert command[1:3]==['-u','-c'] and 'kuavo_camera_remote' in command[3]
        child=real_popen([sys.executable,'-u','-c','import time\nwhile True:\n print("fakeframe",flush=True)\n time.sleep(.01)'],**kwargs)
        children.append(child);return child
    monkeypatch.setattr(agent.subprocess,'Popen',fake_capture)
    conn,response=client.request(profile,'/camera',{'source_id':'usb:/dev/v4l/by-path/fake'})
    assert response.read1(100).startswith(b'fakeframe')
    response.close();conn.close()
    deadline=time.monotonic()+8
    while server.owners and time.monotonic()<deadline:time.sleep(.02)
    assert not server.owners and len(children)==1 and children[0].poll() is not None


def test_busy_stream_waits_for_old_owner_but_does_not_retry_other_failures(monkeypatch):
    clock=[0.];monkeypatch.setattr(client.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(client.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    result=(object(),object())
    request=Mock(side_effect=[client.StreamBusy('busy'),client.StreamBusy('busy'),result]);monkeypatch.setattr(client,'request',request)
    assert client.open_stream({},'/camera',{})==result and request.call_count==3
    request.reset_mock(side_effect=True);request.side_effect=RuntimeError('wrong device')
    with pytest.raises(RuntimeError,match='wrong device'):client.open_stream({},'/camera',{})
    assert request.call_count==1
    request.reset_mock(side_effect=True);request.side_effect=client.StreamBusy('busy')
    start=clock[0]
    with pytest.raises(client.StreamBusy):client.open_stream({},'/camera',{},wait_s=1)
    assert clock[0]-start==1 and request.call_count==5
