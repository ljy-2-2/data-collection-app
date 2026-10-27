"""Diagnostic data stays bounded and survives stop without hardware access."""
import json
from teleop_control.tianji_motion_trace import MotionTrace


def row(i, running=True):
    return dict(session='fixture',sequence=i,teleop_state='running' if running else 'idle',
                motion_debug={'arms':{'left':{'stationary_joint_hold':True}}} if running else None,
                joints_deg={'left':[i]*7},joint_command={'joints_deg':{'left':[i+1]*7}},publisher_dropped=2)


def test_stop_persists_bounded_raw_command_feedback_and_flags(tmp_path):
    path=tmp_path/'trace.json';trace=MotionTrace(path,capacity=3)
    for i in range(5):trace.accept(row(i))
    assert not path.exists(), 'No file writes during active control'
    trace.accept(row(5,False))
    data=json.loads(path.read_text())
    assert [r['sequence'] for r in data['rows']]==[2,3,4]
    assert data['rows'][-1]['joints_deg']['left']==[4]*7
    assert data['rows'][-1]['joint_command']['joints_deg']['left']==[5]*7
    assert trace.status()['publisher_dropped']==2
    assert data['reason']=='control_stopped'
    trace.accept(row(10));trace.flush()
    assert [r['sequence'] for r in json.loads(path.read_text())['rows']]==[10]


def test_trace_write_failure_is_reported_without_failing_control(tmp_path):
    obstruction=tmp_path/'file';obstruction.write_text('not a directory')
    trace=MotionTrace(obstruction/'trace.json');trace.accept(row(1));trace.flush()
    assert trace.status()['error'] and trace.dirty


def test_worker_telemetry_transports_motion_evidence_without_socket_access(monkeypatch):
    from types import SimpleNamespace
    from teleop_control import tianji_worker
    import collection_telemetry as telemetry
    monkeypatch.setattr(telemetry.threading,'Thread',lambda **kw:SimpleNamespace(start=lambda:None,join=lambda **kw:None))
    delivered=[]
    publisher=telemetry.Publisher(sink=lambda value:delivered.append(value) or True)
    debug={'hold_policy':'joint_latch_v2','arms':{'left':{'stationary_joint_hold':True}}}
    publisher.submit(dict(mode='live',state='running',joints_deg={},robot_feedback_details={},
                          grippers=None,motion_debug=debug),None,False)
    packet=publisher.queue.get_nowait()
    publisher._send(None,packet)
    assert delivered[0]['motion_debug']==debug
    publisher.close()
