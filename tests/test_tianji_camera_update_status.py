"""Upgrade diagnostics must never bypass blockers or mutate the controller."""
import json
from copy import deepcopy
from unittest.mock import MagicMock

import pytest
import update_tianji_camera_stream as installer


def first_frame_fault():
    reason = 'Camera did not produce first frame after bounded startup retry; stop'
    error = 'Native helper failed (exit 1): '+reason
    return dict(idle=True, recoverable_camera_fault=True, fault='Stream stopped: '+error,
                last_stream=dict(ok=False, error=error, stop_reason='stream_error',
                                 observation_mode='teleop_observation', native_exit=1,
                                 native_cleanup_timed_out=False, native_termination=None,
                                 native_failure_reason=reason))


def test_reviewed_first_frame_timeout_with_controller_recovery_audit():
    assert installer.reviewed_stream_fault(first_frame_fault())


@pytest.mark.parametrize('change', [None, 'busy','audit','cleanup','forced','wrong_reason','complete','stationary','missing_termination'])
def test_legacy_pipe_upgrade_accepts_only_exact_inspected_failure(change):
    status = first_frame_fault()
    status['version'] = '2.3.2'
    reason = 'Frame pipe closed or blocked for 1 second; stop'
    status['last_stream'].update(native_failure_reason=reason, error='Native helper failed (exit 1): '+reason)
    status['fault'] = 'Stream stopped: '+status['last_stream']['error']
    if change == 'busy': status['idle'] = False
    if change == 'audit': status['last_stream']['error'] += '; Audit changed: modules'
    if change == 'cleanup': status['last_stream']['native_cleanup_timed_out'] = True
    if change == 'forced': status['last_stream']['native_termination'] = 'error_cleanup'
    if change == 'wrong_reason': status['last_stream']['native_failure_reason'] = 'other failure'
    if change == 'complete': status['last_stream']['ok'] = True
    if change == 'stationary': status['last_stream']['observation_mode'] = 'stationary'
    if change == 'missing_termination': del status['last_stream']['native_termination']
    assert installer.reviewed_pipe_fault(status) is (change is None)


@pytest.mark.parametrize('scope,key,value', [
    ('status', 'idle', False),
    ('status', 'recoverable_camera_fault', False),
    ('status', 'recoverable_camera_fault', None),
    ('status', 'fault', 'unknown fault'),
    ('last', 'ok', True),
    ('last', 'observation_mode', 'stationary'),
    ('last', 'stop_reason', 'duration_limit'),
    ('last', 'native_exit', -9),
    ('last', 'native_cleanup_timed_out', True),
    ('last', 'native_termination', 'forced_kill'),
    ('last', 'error', 'Audit changed: dmesg_sha256'),
    ('last', 'native_failure_reason', 'Unknown camera error'),
])
def test_first_frame_failure_never_bypasses_unreviewed_faults(scope, key, value):
    status = deepcopy(first_frame_fault())
    (status if scope == 'status' else status['last_stream'])[key] = value
    assert not installer.reviewed_stream_fault(status)


@pytest.mark.parametrize('idle,fault', [(False, None), (True, 'unknown failure'), (True, None)])
def test_status_only_reads_without_ssh_or_update(monkeypatch, capsys, idle, fault):
    client = MagicMock()
    client.__enter__.return_value = client
    client.status.return_value = dict(idle=idle, fault=fault, token='must not print',
                                     last_stream=dict(error=fault, native_exit=1))
    factory = MagicMock(return_value=client)
    remote = MagicMock(side_effect=AssertionError('No remote mutation allowed'))
    monkeypatch.setattr(installer, 'FusionStreamClient', factory)
    monkeypatch.setattr(installer.subprocess, 'run', remote)
    monkeypatch.setattr('sys.argv', ['update', '--status-only'])
    installer.main()
    output = capsys.readouterr().out
    assert 'must not print' not in output
    assert json.loads(output)['idle'] is idle
    assert json.loads(output)['fault'] == fault
    assert client.method_calls == [('status', (), {})]
    remote.assert_not_called()


@pytest.mark.parametrize('idle,fault,reason', [
    (False, None, '尚未空闲'),
    (True, 'unknown failure', '存在故障记录'),
])
def test_upgrade_blocker_explains_actual_status_and_never_deploys(monkeypatch, idle, fault, reason):
    client = MagicMock()
    client.__enter__.return_value = client
    client.status.return_value = dict(idle=idle, fault=fault)
    remote = MagicMock(side_effect=AssertionError('No remote mutation allowed'))
    monkeypatch.setattr(installer, 'FusionStreamClient', MagicMock(return_value=client))
    monkeypatch.setattr(installer.subprocess, 'run', remote)
    monkeypatch.setattr('sys.argv', ['update'])
    with pytest.raises(RuntimeError, match=reason) as failure:
        installer.main()
    assert '升级尚未执行' in str(failure.value)
    assert '"idle":' in str(failure.value)
    if fault:
        assert fault in str(failure.value)
    remote.assert_not_called()
