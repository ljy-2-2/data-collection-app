"""All I/O is fake. Never load ROS, SDKs, SSH, or robot configuration."""
import json
import math
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'teleop_control/vendor/kuavo_runtime/src'))
from teleop.robots.kuavo5_manual_control import (ManualController,JOINTS,ARMS,BODY,HEAD,BASE,
    validate_command,joint_target)
from teleop.robots.kuavo5_session_service import OwnerLedger,SessionSupervisor

class IO:
    def __init__(self):
        self.q={j['name']:math.radians((j['min_deg']+j['max_deg'])/2) for j in JOINTS}
        self.calls=[];self.fresh=True;self.grip=False
    def feedback(self):
        if not self.fresh: raise ValueError('stale')
        return dict(self.q)
    def base(self,v):self.calls.append(('base',v))
    def joints(self,names,target):self.calls.append(('joint',names,target))
    def head_delta(self,n,d):self.calls.append(('head',n,d))
    def gripper(self,s,c):self.calls.append(('gripper',s,c))
    def tick_grippers(self):pass
    def gripper_done(self,s):return self.grip
    def hold(self):self.calls.append(('hold',))
    def close(self):self.calls.append(('close',))


def refused(fn):
    try:fn()
    except (ValueError,TimeoutError,RuntimeError):return
    raise AssertionError('expected refusal')

count=0
for value in [None,{},dict(action='exec',code='x'),dict(action='joint',joint=ARMS[0],delta_deg=float('nan')),
    dict(action='joint',joint=ARMS[0],delta_deg=True),dict(action='joint',joint=ARMS[0],delta_deg=11),
    dict(action='gripper',side='left',closed=1),dict(action='base',direction='unknown'),
    dict(action='heartbeat',ignored=True)]:
    refused(lambda:validate_command(value));count+=1
for name,names in [(ARMS[0],ARMS),(ARMS[7],ARMS),(BODY[0],BODY),(HEAD[1],HEAD)]:
    io=IO();before=dict(io.q)
    group,target=joint_target(dict(action='joint',joint=name,delta_deg=.5),before)
    assert group==names and math.isclose(target[name],math.degrees(before[name])+.5)
    assert all(math.isclose(v,math.degrees(before[n])) for n,v in target.items() if n!=name)
    assert io.q==before;count+=1
for joint in JOINTS:
    io=IO();io.q[joint['name']]=math.radians(joint['max_deg'])
    refused(lambda:joint_target(dict(action='joint',joint=joint['name'],delta_deg=1),io.q));count+=1
clock=[0.];io=IO();control=ManualController(io,clock=lambda:clock[0])
control.command(dict(action='base',direction='forward'));assert io.calls[-1]==('base',BASE['forward'])
clock[0]=.15;control.command(dict(action='heartbeat'));control.tick()
clock[0]=.21;control.tick();assert io.calls[-1]==('base',BASE['stop'])
assert control.base_direction=='stop';count+=1 # heartbeat never renews a held button
clock[0]=.8;refused(control.tick);count+=1
control=ManualController(io,clock=lambda:clock[0]);control.command(dict(action='joint',joint=ARMS[0],delta_deg=1))
assert control.pending is not None
refused(lambda:control.command(dict(action='joint',joint=ARMS[1],delta_deg=1)));count+=1
control.command(dict(action='stop'));assert control.pending is None and io.calls[-1]==('hold',);count+=1
io.fresh=False;before=list(io.calls);refused(lambda:control.command(dict(action='joint',joint=ARMS[0],delta_deg=1)))
assert io.calls==before;count+=1

class Worker:
    cleanup_error=None;cleanup_confirmed=False;output_drained=False;never_spawned=False
    def __init__(self):self.events=[];self.inputs=[];self.exited=None
    def start(self):self.events.append(dict(event='ready'))
    def poll_events(self):events,self.events=self.events,[];return events
    def poll(self):return self.exited
    def send_input(self,value,**kw):self.inputs.append(value)
    def request_close(self):
        self.cleanup_confirmed=True;self.output_drained=True;self.exited=0
        self.events.append(dict(event='closed',cleanup_confirmed=True,exit_code=0))
    def cleanup_diagnostics(self):return {}

with tempfile.TemporaryDirectory() as tmp:
    ledger=OwnerLedger(Path(tmp)/'owner.json').open();worker=Worker()
    service=SessionSupervisor('a'*64,backend='fake',worker_factory=lambda:Worker(),ledger=ledger,installation_sha256='b'*64)
    service.manual_factory=lambda settings:worker
    service.settings_snapshot={'revision':0}
    packet=dict(event='start_manual_session',protocol='kuavo_manual_v1',confirm_motion=True,
        expected_service_instance_id=service.service_instance_id,expected_installation_sha256='b'*64)
    assert service.handle('owner',{**packet,'confirm_motion':False})['event']=='refused';assert not ledger.blocked;count+=1
    assert service.handle('owner',{**packet,'expected_installation_sha256':'c'*64})['event']=='refused';assert not ledger.blocked;count+=1
    reply=service.handle('owner',packet);assert reply['event']=='manual_session_started'
    assert ledger.state['operation']=='manual' and ledger.blocked
    service.active['started'].wait(1);service.tick()
    assert service.active['phase']=='running' and not service.status()['ready_to_start'];count+=1
    assert service.handle('second',packet)['event']=='refused';count+=1
    command=dict(event='manual_input',session_id=reply['session_id'],packet=dict(event='manual_input',challenge_id='c'*32,command={'action':'heartbeat'}))
    assert service.handle('second',command)['event']=='refused';assert not worker.inputs;count+=1
    assert service.handle('owner',command) is None and len(worker.inputs)==1;count+=1
    service.disconnect('owner');service.tick();assert not ledger.blocked and service.active is None;count+=1
    ledger.close()
assert 'rospy' not in sys.modules
print('manual offline checks:',count)

# Exercise the real manual run loop with a fake device and in-memory channel.
# The robot challenge, acknowledgement and cleanup framing are production code.
import contextlib
import io as text_io
import queue
from unittest.mock import patch
from teleop.robots import kuavo5_manual_runtime as runtime

class RuntimeIO(IO):
    def __init__(self,bundle):super().__init__();self.started=False
    def start(self):self.started=True
    def initial_pose(self):return dict(joints=dict(self.q),grippers={"left":.5,"right":.5})
    def joints(self,names,target):
        super().joints(names,target)
        self.q.update({n:math.radians(v) for n,v in target.items()})
    def gripper_feedback(self,side):
        return SimpleNamespace(open_rad=-2.,closed_rad=-1.),SimpleNamespace(position_rad=-1.5)

incoming=queue.Queue();instances=[];packets=[];commands=iter([
    dict(action='base',direction='forward'),dict(action='base',direction='stop'),
    dict(action='joint',joint=ARMS[0],delta_deg=.5)])
class Input:
    def __iter__(self):return self
    def __next__(self):
        value=incoming.get(timeout=2)
        if value is None:raise StopIteration
        return json.dumps(value)+'\n'
class Output(text_io.StringIO):
    def write(self,value):
        result=super().write(value)
        if value=='\n':
            packet=json.loads(self.getvalue().splitlines()[-1]);packets.append(packet)
            if packet['event']=='manual_challenge':
                command=next(commands,dict(action='heartbeat'))
                incoming.put(dict(event='manual_input',challenge_id=packet['challenge_id'],command=command))
            if len([p for p in packets if p['event']=='manual_ack'])==3:incoming.put(None)
        return result

def factory(bundle):
    instance=RuntimeIO(bundle);instances.append(instance);return instance
with patch.object(runtime,'RosManualIO',factory),patch.object(sys,'stdin',Input()),contextlib.redirect_stdout(Output()):
    result=runtime.run_manual({})
assert result==0 and packets[-1]['event']=='closed' and packets[-1]['cleanup_confirmed'] is True
assert any(call[0]=='joint' for call in instances[0].calls)
assert ('base',BASE['forward']) in instances[0].calls and ('base',BASE['stop']) in instances[0].calls
assert instances[0].calls[-1]==('close',)
assert 'rospy' not in sys.modules
print('manual production loop framing/cleanup: passed')

# Native callback rejects stale acquisition headers; receipt alone is not fresh.
robot_io=runtime.RosManualIO({})
robot_io.ros=SimpleNamespace(Time=SimpleNamespace(now=lambda:SimpleNamespace(to_nsec=lambda:1000000000)))
message=SimpleNamespace(joint_data=SimpleNamespace(joint_q=[0.]*20,joint_v=[0.]*20),
    sensor_time=SimpleNamespace(secs=1,nsecs=0),header=SimpleNamespace(stamp=SimpleNamespace(to_nsec=lambda:1)))
robot_io.on_sensor(message)
assert robot_io.sensor is None and robot_io.fault
print('manual sensor acquisition freshness: passed')
assert JOINTS == json.loads((ROOT/'teleop_control/assets/kuavo/manual-joints.json').read_text())

incoming=queue.Queue();instances=[]
class WrongChallenge(text_io.StringIO):
    def write(self,value):
        result=super().write(value)
        if value=='\n':
            packet=json.loads(self.getvalue().splitlines()[-1])
            if packet['event']=='manual_challenge':incoming.put(dict(event='manual_input',challenge_id='invalid',command={'action':'base','direction':'forward'}))
            if packet['event']=='closed':incoming.put(None)
        return result
output=WrongChallenge()
with patch.object(runtime,'RosManualIO',factory),patch.object(sys,'stdin',Input()),contextlib.redirect_stdout(output):
    runtime.run_manual({})
assert instances[0].calls==[('close',)]
assert 'expired or replayed' in json.loads(output.getvalue().splitlines()[-1])['error']
print('manual challenge mismatch never sends motion: passed')

class UnconfirmedWorker(Worker):
    def request_close(self):
        self.cleanup_confirmed=False;self.output_drained=True;self.exited=1
        self.events.append(dict(event='closed',cleanup_confirmed=False,exit_code=1))
with tempfile.TemporaryDirectory() as tmp:
    ledger=OwnerLedger(Path(tmp)/'owner.json').open();worker=UnconfirmedWorker()
    service=SessionSupervisor('a'*64,backend='fake',worker_factory=lambda:worker,ledger=ledger,installation_sha256='b'*64)
    service.manual_factory=lambda settings:worker;service.settings_snapshot={'revision':0}
    packet=dict(event='start_manual_session',protocol='kuavo_manual_v1',confirm_motion=True,
        expected_service_instance_id=service.service_instance_id,expected_installation_sha256='b'*64)
    service.handle('owner',packet);service.active['started'].wait(1);service.tick()
    service.disconnect('owner');service.tick()
    assert ledger.blocked and service.active is not None and not service.status()['ready_to_start']
    ledger.close()
print('manual unconfirmed cleanup preserves owner: passed')

# Saved poses follow measured one-degree steps; stopping/disconnect cancels them.
from teleop.robots.kuavo5_manual_control import validate_pose
class PoseIO(IO):
    def __init__(self):
        super().__init__();self.grip=True
    def joints(self,names,target):
        assert max(abs(target[n]-math.degrees(self.q[n])) for n in names)<=1.000001
        super().joints(names,target)
        self.q.update({n:math.radians(v) for n,v in target.items()})
    def head_delta(self,n,d):
        assert abs(d)<=1
        super().head_delta(n,d);self.q[n]+=math.radians(d)
    def gripper_fraction(self,side,fraction):self.calls.append(('fraction',side,fraction))

clock=[0.];io=PoseIO();control=ManualController(io,clock=lambda:clock[0])
pose=dict(joints=dict(io.q),grippers={'left':.3,'right':.8})
for n in (ARMS[0],ARMS[7],BODY[0],HEAD[0]):pose['joints'][n]+=math.radians(2.2)
control.command(dict(action='restore',pose=pose))
refused(lambda:control.command(dict(action='base',direction='forward')))
for _ in range(200):
    clock[0]+=.05;control.command(dict(action='heartbeat'));control.tick()
    if control.restoring is None and control.pending is None:break
else:raise AssertionError('pose never completed')
assert all(abs(io.q[n]-v)<math.radians(.1) for n,v in pose['joints'].items())
assert ('fraction','left',.3) in io.calls and ('fraction','right',.8) in io.calls
assert not any(c[0]=='base' for c in io.calls)
# Invalid full target is rejected before any operation.
for bad in [dict(joints={},grippers=pose['grippers']),dict(joints=pose['joints'],grippers={'left':True,'right':0}),
            dict(joints={**pose['joints'],ARMS[0]:float('nan')},grippers=pose['grippers'])]:
    before=list(io.calls);refused(lambda:control.command(dict(action='restore',pose=bad)))
    assert before==io.calls
pose['joints'][ARMS[0]]+=math.radians(4)
control.command(dict(action='restore',pose=pose));control.tick()
control.command(dict(action='stop'));assert control.restoring is None and control.pending is None
before=list(io.calls);clock[0]+=.3;control.command(dict(action='heartbeat'));control.tick();assert io.calls==before
control.command(dict(action='restore',pose=pose));clock[0]+=1
before=list(io.calls);refused(control.tick);assert io.calls==before
io.fresh=False;refused(control.tick);assert io.calls==before
print('manual measured pose restore/save targets/stop/stale checks: passed')

# Startup default uses manufacturer initialization, not the measured entry pose.
from teleop.robots.kuavo5_manual_control import vendor_initial_pose,SENSOR_NAMES
from teleop.robots.kuavo5_return_config import resolve_vendor_return,DEFAULT_RETURN_CONFIG
measured=IO().q
initial=[0.,0.,0.,0.,0.,0.,1.]+[measured[n] for n in SENSOR_NAMES]
initial[11]+=math.radians(10)
pose=vendor_initial_pose(initial,{'left':.2,'right':.7})
assert pose['joints'][ARMS[0]]!=measured[ARMS[0]]
assert set(pose['joints'])==set(SENSOR_NAMES) and len(pose['joints'])==20
schema=SimpleNamespace(names=ARMS,joints=[SimpleNamespace(name=n,position_min=math.radians(j['min_deg']),position_max=math.radians(j['max_deg'])) for n in ARMS for j in JOINTS if j['name']==n])
xr=resolve_vendor_return(DEFAULT_RETURN_CONFIG,initial,schema)
assert {n:pose['joints'][n] for n in ARMS}==xr['targets']
for bad in [initial[:-1], [float('nan')]+initial[1:],initial[:7]+[100.]+initial[8:]]:
    refused(lambda:vendor_initial_pose(bad,{'left':.2,'right':.7}))
# Real binding only reads the pinned shared config, parameter and gripper feedback.
robot_io=runtime.RosManualIO({'return_home_config':DEFAULT_RETURN_CONFIG})
robot_io.ros=SimpleNamespace(get_param=lambda name:initial if name=='/robot_init_state_param' else None)
robot_io.gripper_feedback=lambda side:(SimpleNamespace(open_rad=-2.,closed_rad=-1.),SimpleNamespace(position_rad=-1.5))
assert robot_io.initial_pose()==vendor_initial_pose(initial,{'left':.5,'right':.5})
print('manual default shares XR manufacturer initialization targets: passed')

# 1/5/10 degree jogs reach the requested target via bounded measured steps.
for amount in (1,5,10):
    clock=[0.];io=PoseIO();control=ManualController(io,clock=lambda:clock[0])
    before=dict(io.q);target=ARMS[0]
    control.command(dict(action='joint',joint=target,delta_deg=amount))
    for _ in range(300):
        clock[0]+=.05;control.command(dict(action='heartbeat'));control.tick()
        if control.pending is None and control.restoring is None:break
    else:raise AssertionError('jog never completed')
    assert abs(math.degrees(io.q[target]-before[target])-amount)<.1
    assert all(io.q[n]==q for n,q in before.items() if n!=target)
    assert not any(c[0] in ('base','gripper','fraction') for c in io.calls)
print('manual 1/5/10 degree measured jogs: passed')
