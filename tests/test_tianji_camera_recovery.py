"""Real controller finalization/recovery with fake USB, process and socket I/O."""
import base64
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, mock_open

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'fusion_camera_stream/controller'))
sys.path.insert(0, str(ROOT/'scripts'))
import camera_audit
import fix_tianji_camera_recovery as installer

streaming = SimpleNamespace()
exec(compile(base64.b64decode(installer.payload()['stream_source']), 'patched_streaming.py', 'exec'), streaming.__dict__)
BAD = installer.INVALID_FRAME


@pytest.mark.parametrize('change', ['none', 'running', 'recording', 'preview', 'other_fault', 'cleanup'])
def test_installer_requires_stopped_app_and_exact_reviewed_fault(change):
    app = dict(status=dict(state='idle', collection=dict(busy=False, armed=False)), camera_thread_alive=False)
    reason = 'Native helper failed (exit 1): '+BAD
    status = dict(version='2.3.2', idle=True, fault='Stream stopped: '+reason,
                  last_stream=dict(error=reason, native_exit=1, native_cleanup_timed_out=False,
                                   native_failure_reason=BAD))
    if change == 'running': app['status']['state'] = 'running'
    if change == 'recording': app['status']['collection']['busy'] = True
    if change == 'preview': app['camera_thread_alive'] = True
    if change == 'other_fault': status['fault'] += '; Audit changed: modules'
    if change == 'cleanup': status['last_stream']['native_cleanup_timed_out'] = True
    if change == 'none': installer.check_idle(app, status)
    else:
        with pytest.raises(RuntimeError): installer.check_idle(app, status)


def audit():
    result = dict.fromkeys(streaming.AUDIT_KEYS, 'same')
    result.update(camera_audit.dmesg_fingerprint(b'[ 1.000001] baseline\n'))
    result['xenomai'] = '1 2033 100 500 0 00300184 0.4 RTPSI\n1 0 0 500 0 00100084 5.3 FX_RtTask\n'
    return result


@pytest.mark.parametrize('native_reason', [BAD, 'Frame pipe closed or blocked for 1 second; stop'])
@pytest.mark.parametrize('fault', ['none', 'hotplug', 'inventory_stop', 'usb', 'rt', 'cleanup', 'unknown', 'interface'])
def test_bad_frame_stops_round_and_only_clean_camera_fault_offers_recovery(monkeypatch, fault, native_reason):
    cameras = [{'id':name} for name in ('usb-1', 'usb-2', 'usb-4')]
    service = SimpleNamespace(config=dict(helper='/unused/capture', cameras=cameras),
                              runtime='/unused', fault=None, last_stream=None)
    process = Mock(pid=43210, returncode=1)
    process.poll.return_value = 1
    if fault == 'inventory_stop':
        process.returncode = 0; process.poll.return_value = 0
    process.stdout.fileno.return_value = 10
    rows = [dict(event=event, task=task, cpu=1, pid=2, msw=0, csw=1, pf=0, process_start_ticks=123)
            for event in ('rt_baseline', 'rt_final') for task in ('RTPSI','FX_RtTask')]
    if fault == 'rt': rows[-1]['pf'] = 1
    reason = native_reason if fault != 'unknown' else 'Unknown native error; stop'
    logs = (reason+'\n'+'\n'.join(json.dumps(row) for row in rows)).encode()
    before, after = audit(), audit()
    if fault == 'usb': after.update(camera_audit.dmesg_fingerprint(b'[ 1.000001] baseline\n[ 2.000001] usb disconnected\n'))
    if fault in ('hotplug', 'inventory_stop'): after.update(camera_audit.dmesg_fingerprint(
        b'[ 1.000001] baseline\n[ 2.000001] usb 1-2: USB disconnect, device number 3\n'))
    replies = []
    now = iter(i/10 for i in range(1000))
    api = SimpleNamespace(ACTIVE=None, STOP=False, monotonic=lambda:next(now),
                          interface_idle=lambda c:fault != 'interface',
                          device_info=lambda c:dict(c, available=True, bus=1, address=2),
                          read=lambda p:'boot', reply=lambda sock, row, *args:replies.append(row))
    # Preflight must pass; a failed post-capture interface check blocks recovery.
    checks = iter([True]*3+[False]*3) if fault == 'interface' else None
    if checks: api.interface_idle = lambda c:next(checks)
    monkeypatch.setitem(streaming.__dict__, 'open', mock_open(read_data=json.dumps({'sha256':{'/unused/capture-three-teleop':'hash'}})))
    for key, value in dict(sha=lambda p:'hash', audit=Mock(side_effect=[before,after]), JointReader=Mock()).items():
        monkeypatch.setitem(streaming.__dict__, key, value)
    monkeypatch.setattr(streaming.subprocess, 'call', lambda *a, **kw:1)
    monkeypatch.setattr(streaming.subprocess, 'Popen', lambda *a, **kw:process)
    monkeypatch.setattr(streaming.tempfile, 'TemporaryFile', lambda **kw:io.BytesIO(logs))
    monkeypatch.setattr(streaming.fcntl, 'fcntl', lambda *a:0)
    monkeypatch.setattr(streaming.select, 'select', lambda *a:([10],[],[]))
    monkeypatch.setattr(streaming.os, 'read', lambda *a:b'')
    if fault == 'cleanup':
        process.poll.side_effect = [None]*100
        def kill(pid, sig):
            if sig == 9: process.returncode = -9; process.poll.side_effect = None; process.poll.return_value = -9
        monkeypatch.setattr(streaming.os, 'killpg', kill)
        monkeypatch.setattr(streaming.time, 'sleep', lambda n:None)
        # Force the stream to terminate first, then exercise timed-out cleanup.
        api.STOP = True
    sock = Mock()
    if fault == 'inventory_stop':
        sock.recv.return_value = b'S'
        monkeypatch.setattr(streaming.select, 'select', lambda *a:([sock],[],[]))
    streaming.run(service, sock, dict(seconds=60, observation_mode='teleop_observation'), api)
    end = replies[-1]
    assert not end['ok']
    assert service.fault
    allowed = fault in ('none', 'inventory_stop') or (fault == 'hotplug' and native_reason == BAD)
    assert end['recoverable_camera_fault'] is allowed
    assert (service.recovery_audit is not None) is allowed
    assert end['camera_recovery_requires_manual'] is (native_reason != BAD and fault == 'none')
    assert api.ACTIVE is None
    process.stdout.close.assert_called_once()


@pytest.mark.parametrize('manual_required', [False, True])
def test_recovery_allows_known_teleop_transitions_but_rejects_other_changes(monkeypatch, manual_required):
    before, after = audit(), audit()
    after.update(camera_audit.dmesg_fingerprint(b'[ 1.000001] baseline\n[ 2.000001] Mot[0][0] process disable\n'))
    assert streaming.unchanged_for_recovery(before, after, 'teleop_observation')
    assert not streaming.unchanged_for_recovery(before, after, 'stationary')
    for key in streaming.AUDIT_KEYS:
        changed = deepcopy(after)
        changed[key] = 'bad'
        if key == 'dmesg_sha256': changed['dmesg_nonservo_sha256'] = 'unknown'
        assert not streaming.unchanged_for_recovery(before, changed, 'teleop_observation')
    service = SimpleNamespace(recovery_audit=before, fault='bad frame', last_finish=0,
                              last_stream=dict(ok=False, observation_mode='teleop_observation',
                                               camera_recovery_requires_manual=manual_required),
                              config=dict(helper='/unused/capture', cameras=[dict(id='usb-1')], cooldown_seconds=2))
    api = SimpleNamespace(ACTIVE=None, monotonic=lambda:10, interface_idle=lambda c:True,
                          device_info=lambda c:dict(available=True, bus=1, address=2))
    monkeypatch.setitem(streaming.__dict__, 'audit', lambda:after)
    monkeypatch.setitem(streaming.__dict__, 'open', mock_open(read_data=json.dumps({'sha256':{'helper':'hash'}})))
    monkeypatch.setitem(streaming.__dict__, 'sha', lambda p:'hash')
    monkeypatch.setattr(streaming.subprocess, 'call', lambda *a, **kw:0)
    if manual_required:
        assert streaming.recover(service, api)['error'] == 'manual_recovery_required'
    assert not streaming.recover(service, api, manual=manual_required)['ok']  # busy USB
    assert service.fault
    monkeypatch.setattr(streaming.subprocess, 'call', lambda *a, **kw:1)
    assert streaming.recover(service, api, manual=manual_required)['recovered']
    assert service.fault is None
    assert service.last_stream['ok'] is False  # Failed dataset stays failed.
    assert not streaming.recover(service, api)['ok']


@pytest.mark.parametrize('blocked', ['active','cooldown','audit','integrity','interface','owner','missing','invalid_request'])
def test_manual_pipe_recovery_never_bypasses_current_checks(monkeypatch, blocked):
    baseline = audit()
    current = deepcopy(baseline)
    if blocked == 'audit':
        current['robot_pids'] = ['changed']
    last = dict(ok=False, error='pipe timeout', camera_recovery_requires_manual=True,
                cameras=['usb-2'], observation_mode='teleop_observation')
    service = SimpleNamespace(recovery_audit=baseline, fault='pipe timeout', last_finish=0,
        last_stream=last, config=dict(helper='/unused/capture', cooldown_seconds=2,
                                    cameras=[dict(id='usb-2')]))
    api = SimpleNamespace(ACTIVE=object() if blocked == 'active' else None,
        monotonic=lambda: 1 if blocked == 'cooldown' else 10,
        interface_idle=lambda c: blocked != 'interface',
        device_info=lambda c:dict(available=blocked != 'missing', bus=1, address=2))
    monkeypatch.setitem(streaming.__dict__, 'audit', lambda:current)
    monkeypatch.setitem(streaming.__dict__, 'open', mock_open(read_data=json.dumps({'sha256':{'helper':'hash'}})))
    monkeypatch.setitem(streaming.__dict__, 'sha', lambda p:'bad' if blocked == 'integrity' else 'hash')
    monkeypatch.setattr(streaming.subprocess, 'call', lambda *a, **kw:0 if blocked == 'owner' else 1)
    assert not streaming.recover(service, api, manual='true' if blocked == 'invalid_request' else True)['ok']
    assert service.fault == 'pipe timeout' and service.recovery_audit is baseline
    assert service.last_stream is last and not last['ok']


def test_manual_recovery_protocol_requires_authentication_and_preserves_default(monkeypatch):
    source = ROOT/'fusion_data_collection/FusionDataCollector/controller_v22/server.py'
    spec = importlib.util.spec_from_file_location('manual_recovery_server', source)
    server = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, server)
    spec.loader.exec_module(server)
    service = server.Service(dict(token='test-token', cooldown_seconds=2))
    recover = Mock(return_value=dict(ok=True))
    monkeypatch.setitem(sys.modules, 'streaming', SimpleNamespace(recover=recover))
    assert not service.dispatch(dict(method='recover', manual=True, token='wrong'))[0]['ok']
    recover.assert_not_called()
    service.dispatch(dict(method='recover', manual=True, token='test-token'))
    recover.assert_called_once_with(service, server, manual=True)
    recover.reset_mock()
    service.dispatch(dict(method='recover', token='test-token'))
    recover.assert_called_once_with(service, server, manual=False)
    service.recovery_audit = audit()
    service.last_stream = dict(ok=False, camera_recovery_requires_manual=True)
    status = service.dispatch(dict(method='status', token='test-token'))[0]
    assert status['stream_transport_recovery'] and status['camera_recovery_requires_manual']


def test_client_only_sends_manual_flag_for_explicit_request():
    from fusion_camera import FusionStreamClient
    client = object.__new__(FusionStreamClient)
    client._request = Mock(return_value=(dict(ok=True), b''))
    client.recover_camera()
    client._request.assert_called_once_with('recover')
    client._request.reset_mock()
    client.recover_camera(manual=True)
    client._request.assert_called_once_with('recover', manual=True)
    with pytest.raises(ValueError):
        client.recover_camera(manual='true')


@pytest.mark.parametrize('isolate', [False, True])
def test_live_camera_stream_survives_only_negotiated_joint_gap(monkeypatch, isolate):
    cameras = [dict(id=name) for name in ('usb-1','usb-2','usb-4')]
    service = SimpleNamespace(config=dict(helper='/unused/capture',cameras=cameras),
                              runtime='/unused',fault=None,last_stream=None)
    process = Mock(pid=43210,returncode=0)
    process.poll.return_value = 0
    process.stdout.fileno.return_value = 10
    rows = [dict(event=e,task=t,cpu=1,pid=2,msw=0,csw=1,pf=0,process_start_ticks=123)
            for e in ('rt_baseline','rt_final') for t in ('RTPSI','FX_RtTask')]
    logs = '\n'.join(json.dumps(r) for r in rows).encode()
    before,after = audit(),audit()
    after.update(camera_audit.dmesg_fingerprint(
        b'[ 1.000001] baseline\n[ 2.000001] Mot[0][0] process reset\n'
        b'[ 2.000002] _FX_ServoFsmFaultResetRising 0x80\n'
        b'[ 2.000003] _FX_ServoFsmFaultResetFalling 0x0\n'))
    replies = []
    now = iter(i/100 for i in range(1000))
    api = SimpleNamespace(ACTIVE=None,STOP=False,monotonic=lambda:next(now),
                          interface_idle=lambda c:True,
                          device_info=lambda c:dict(c,available=True,bus=1,address=2),
                          read=lambda p:'boot',reply=lambda sock,row,*args:replies.append(row))
    class Sampler:
        def __init__(self,*args):
            assert args[-1] == isolate
            self.error = None if isolate else 'Joint sampler delayed over 100 ms'
            self.feedback_error = 'Joint sampler delayed over 100 ms'
            self.gap_count = 1
            self.count = 0
            self.skipped = 16
            self.max_lateness_ns = 161000000
            self.done = Mock()
            self.events = [dict(type='health',ok=True,joint_feedback_ok=False,
                                joint_feedback_error=self.feedback_error)]
        def start(self): pass
        def is_alive(self): return False
        def join(self,*args): pass
        def pop(self): return self.events.pop(0) if self.events else None
    monkeypatch.setitem(streaming.__dict__,'open',mock_open(read_data=json.dumps(
        {'sha256':{'/unused/capture-three-teleop':'hash'}})))
    for key,value in dict(sha=lambda p:'hash',audit=Mock(side_effect=[before,after]),
                          JointReader=Mock(),JointSampler=Sampler).items():
        monkeypatch.setitem(streaming.__dict__,key,value)
    monkeypatch.setattr(streaming.subprocess,'call',lambda *a,**kw:1)
    monkeypatch.setattr(streaming.subprocess,'Popen',lambda *a,**kw:process)
    monkeypatch.setattr(streaming.tempfile,'TemporaryFile',lambda **kw:io.BytesIO(logs))
    monkeypatch.setattr(streaming.fcntl,'fcntl',lambda *a:0)
    monkeypatch.setattr(streaming.select,'select',lambda *a:([10],[],[]))
    def frame(camera):
        return streaming.HEADER.pack(b'FJM1',camera,1,100,128,1024,768,0,0)+b'x'*128
    chunks = iter([frame(1)+frame(2),b''])
    monkeypatch.setattr(streaming.os,'read',lambda *a:next(chunks))
    streaming.run(service,Mock(),dict(seconds=1,observation_mode='teleop_observation',
                                     camera_ids=['usb-2','usb-4'],isolate_joint_feedback=isolate),api)
    assert len([r for r in replies if r['type']=='image']) == 2
    end = replies[-1]
    assert end['ok'] is isolate
    assert end['joint_samples'] == 0  # Health events are never counted as samples.
    assert end['joint_gap_count'] == 1
    if isolate:
        assert service.fault is None
        assert any(r.get('joint_feedback_ok') is False for r in replies)
    else:
        assert 'Joint sampler delayed' in service.fault
