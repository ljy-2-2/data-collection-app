"""Passive telemetry gaps must not kill video or fabricate valid recordings."""
import base64
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'fusion_camera_stream/controller'))
sys.path.insert(0, str(ROOT/'scripts'))
import update_tianji_camera_stream as installer

streaming = SimpleNamespace()
exec(compile(base64.b64decode(installer.source_files()[
    'FusionDataCollector/controller_v22/streaming.py']), 'deployed_streaming.py', 'exec'),
    streaming.__dict__)


class Clock:
    def __init__(self, delayed=False):
        self.now = 10.
        self.delayed = delayed

    def monotonic(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds
        if self.delayed and self.now >= 10.01:
            self.now += .161
            self.delayed = False
        return False


@pytest.mark.parametrize('fault', ['delay', 'stale', 'arm_error', 'torn_snapshot'])
def test_joint_gap_is_explicit_and_fresh_sampling_recovers_without_camera_restart(fault):
    clock = Clock(delayed=fault == 'delay')
    def sample():
        if fault == 'torn_snapshot' and 10.02 < clock.now < 10.3:
            raise RuntimeError('DCSS changed during bounded snapshot reads')
        serial = 1 if fault == 'stale' and clock.now < 10.3 else round(clock.now*1000)
        arm_error = int(fault == 'arm_error' and 10.02 < clock.now < 10.3)
        return dict(frame_serial=[serial,serial], velocity_deg_s=[20.]*14,
                    arm_state=[0,0,arm_error,0,0,0], timestamp_ns=round(clock.now*1e9))
    sampler = streaming.JointSampler(clock, SimpleNamespace(sample=sample), 1,
                                    stationary_only=False, isolate_faults=True)
    sampler.done = clock
    sampler.run()
    assert sampler.error is None and sampler.feedback_error is None
    events = list(sampler.queue)
    health = [x for x in events if x['type'] == 'health']
    assert [x['joint_feedback_ok'] for x in health] == [False, True]
    assert sampler.gap_count == 1
    joints = [x for x in events if x['type'] == 'joints']
    assert len(joints) == sampler.count
    assert all(a['timestamp_ns'] < b['timestamp_ns'] for a,b in zip(joints,joints[1:]))
    bad, good = health
    assert not any(bad['timestamp_ns'] <= j['timestamp_ns'] < good['timestamp_ns'] for j in joints)
    if fault == 'delay':
        assert sampler.max_lateness_ns >= 160_000_000
        assert sampler.skipped >= 16


def test_stationary_streams_keep_strict_delay_checks():
    for stationary,isolate in [(True,False),(True,True)]:
        clock = Clock(delayed=True)
        reader = SimpleNamespace(sample=lambda:dict(
            frame_serial=[1,1],velocity_deg_s=[0.]*14,arm_state=[0]*6))
        sampler = streaming.JointSampler(clock,reader,1,stationary_only=stationary,isolate_faults=isolate)
        sampler.done = clock
        sampler.run()
        assert sampler.error == 'Joint sampler delayed over 100 ms'


def test_upgrade_exception_rejects_other_or_unclean_faults():
    error = 'Joint sampler delayed over 100 ms; Audit changed: dmesg_sha256'
    last = dict(error=error,ok=False,native_exit=0,native_cleanup_timed_out=False,
                native_termination='error_cleanup',native_failure_reason='',
                observation_mode='teleop_observation',joint_max_lateness_ns=160942443)
    assert installer.reviewed_joint_fault(dict(fault=error,last_stream=last))
    for change in [dict(native_exit=1),dict(native_cleanup_timed_out=True),
                   dict(native_failure_reason='USB failure'),dict(joint_max_lateness_ns=1),
                   dict(error=error+'; Audit changed: modules')]:
        assert not installer.reviewed_joint_fault(dict(fault=error,last_stream=dict(last,**change)))
