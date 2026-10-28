"""Exercise the actual copied read-only decoder and app transport without SSH."""
import hashlib
import io
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock, patch
from teleop_control.kuavo_worker import client_module
client_module()
socket.socket.connect = lambda *_: (_ for _ in ()).throw(AssertionError('network forbidden'))
from teleop.robots.kuavo5 import Kuavo5Adapter, v6_candidate_schema
from teleop.robots.kuavo5_state_receiver import Kuavo5StateReceiver
from teleop.robots.kuavo5_readonly_stream import KuavoReadonlyFeed
from teleop_control.kuavo_preview import ASSETS
from teleop_control.kuavo_readonly import monitor

xml=(ASSETS/'full_robot.urdf').read_bytes()
schema=v6_candidate_schema(xml.decode())
clock=[1_000_000_000]
receiver=Kuavo5StateReceiver(Kuavo5Adapter(schema,robot_version='63'),joint_names=schema.names,
                            robot_version='63',clock_ns=lambda:clock[0])
feed=KuavoReadonlyFeed(receiver,version='63',names=schema.names,urdf_sha256=hashlib.sha256(xml).hexdigest())
ready=dict(event='ready',robot_version='63',joint_names=list(schema.names),urdf_sha256=hashlib.sha256(xml).hexdigest())
positions=[max(j.position_min,min(j.position_max,0.)) for j in schema.joints]
frame=dict(event='frame',robot_version='63',sensor_secs=10,sensor_nsecs=1,joint_q=positions,source_age_ms=0.)
heartbeat=dict(event='heartbeat',robot_version='63')
feed.accept_line(json.dumps(ready));feed.accept_line(json.dumps(frame))
assert receiver.snapshot()['status']=='fresh' and len(receiver.snapshot()['positions_rad'])==20
clock[0]+=400_000_000;feed.accept_line(json.dumps(heartbeat))
assert receiver.snapshot()['status']=='stale'
try:feed.accept_line(json.dumps({**ready,'urdf_sha256':'0'*64}))
except ValueError:pass
else:raise AssertionError('mismatched robot model accepted')

feed.accept_line(json.dumps(ready))
late={**frame,'sensor_secs':20,'source_age_ms':60_000.}
try:feed.accept_line(json.dumps(late))
except ValueError:pass
else:raise AssertionError('default freshness gate changed')
recording_feed=KuavoReadonlyFeed(receiver,version='63',names=schema.names,
    urdf_sha256=hashlib.sha256(xml).hexdigest(),maximum_source_age_ms=None)
recording_feed.accept_line(json.dumps(ready))
assert recording_feed.accept_line(json.dumps(late)) and recording_feed.last_source_age_ms==60_000.
try:recording_feed.accept_line(json.dumps({**late,'source_age_ms':float('nan')}))
except ValueError:pass
else:raise AssertionError('invalid age accepted')

process=SimpleNamespace(stdin=io.BytesIO(),stdout=Mock(),stderr=io.BytesIO(),poll=Mock(return_value=None),
                        terminate=Mock(),kill=Mock(),wait=Mock())
selector=Mock();selector.select.return_value=[(SimpleNamespace(fd=123),None)]
messages=[(json.dumps(ready)+'\n'+json.dumps({**frame,'source_age_ms':60_000.})+'\n').encode(),(json.dumps(heartbeat)+'\n').encode(),b'']
events=[]
with patch('teleop_control.kuavo_network_client.profile',return_value={'url':'https://192.0.2.1:7444'}), \
     patch('teleop_control.kuavo_network_diagnostics.inspect_route',return_value={'blocked':False}), \
     patch('subprocess.Popen',return_value=process) as popen, patch('signal.signal'), \
     patch('threading.Thread.start'), patch('selectors.DefaultSelector',return_value=selector), \
     patch('os.read',side_effect=messages):
    try:monitor({'network_host':'192.0.2.1'},{'config':'/tmp/fake/teleop_control/runtime/kuavo/connection.json'},events.append)
    except RuntimeError as exc:assert '数据流断开' in str(exc)
    else:raise AssertionError('EOF must end the monitor')
    samples=[e for e in events if 'collection_events' in e]
    poses=[e for e in events if 'telemetry' in e]
    assert len(samples[0]['collection_events'][0]['position_rad'])==14
    assert poses[0]['pose_status']=='stale' and len(poses[0]['telemetry']['joints'])==20
    assert samples[0]['collection_events'][0]['source_age_ms']==60_000.
    assert poses[0]['telemetry']['age_s']>=60.
    assert 'kuavo_network_client.py' in popen.call_args.args[0][2]
    assert '/joints' in popen.call_args.args[0] and 'ssh' not in popen.call_args.args[0]
    process.terminate.assert_called_once();process.kill.assert_not_called()
print('Read-only model handshake, 20 joints, heartbeat freshness, disconnect and owned transport cleanup passed')
