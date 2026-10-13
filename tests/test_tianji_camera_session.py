"""A adopts the existing transport; B must receive its real audited end."""
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'teleop_control/src'))
sys.path.insert(0,str(ROOT/'fusion_camera_stream'))
from fusion_camera import CameraError, StreamEvent
from teleop_control.tianji_camera_session import CameraSession


def ready():
    transport = Mock()
    clock = dict(boot_id='test-boot', pc_midpoint_ns=100, offset_ns=200, uncertainty_ns=10)
    factory = Mock()
    factory.return_value.clock_sync.return_value = dict(clock, pc_midpoint_ns=1000)
    session = CameraSession(transport, clock, factory)
    session.publish(StreamEvent(dict(type='start',boot_id='test-boot',observation_mode='teleop_observation',graceful_stop=True)))
    for camera in ('usb-1','usb-2','usb-4'):
        session.publish(StreamEvent(dict(type='image',camera_id=camera),b'pre-A-frame'))
    session.publish(StreamEvent(dict(type='joints')))
    return session, transport, factory


@pytest.mark.parametrize('ok',[True,False])
def test_adoption_does_not_restart_cameras_and_preserves_real_end(ok):
    session, transport, factory = ready()
    adopted = session.subscribe()
    adopted.validate_stream()
    assert adopted.clock_sync() == session.clock
    transport.assert_not_called()
    assert transport.method_calls == []  # A does not open, stop, or query live service.
    factory.assert_not_called()
    stream = adopted.stream()
    start = next(stream)
    assert start.metadata['adopted_preview_stream']
    assert start.metadata['episode_count_baseline'] == dict(
        frames=dict.fromkeys(('usb-1','usb-2','usb-4'),1), joint_samples=1,
        source='received_before_subscription')
    assert 'adopted_preview_stream' not in session.start.metadata
    assert not adopted.queue  # Pre-A images were not recorded.
    image = StreamEvent(dict(type='image',camera_id='usb-1',sequence=101),b'post-A-frame')
    session.publish(image)
    assert start.metadata['episode_count_baseline']['frames']['usb-1'] == 1
    assert next(stream) is image
    adopted.request_stop()
    adopted.request_stop()
    transport.request_stop.assert_called_once()
    end = StreamEvent(dict(type='end',ok=ok,error=None if ok else 'USB failure',native_exit=0 if ok else 1,
                           audit_before={'actual':'before'},audit_after={'actual':'after'}))
    session.publish(end)
    session.finish()
    assert next(stream) is end
    with pytest.raises(StopIteration if ok else CameraError): next(stream)
    assert adopted.clock_sync()['pc_midpoint_ns'] == 1000
    factory.assert_called_once()
    adopted.close()
    transport.request_stop.assert_called_once()


@pytest.mark.parametrize('problem',['camera','joints','boot','ended','duplicate'])
def test_rejects_unready_or_already_owned_preview(problem):
    session, _, _ = ready()
    if problem == 'camera':session.fresh['usb-2'] = time.monotonic()-3
    if problem == 'joints':session.joint_time = time.monotonic()-1
    if problem == 'boot':session.clock['boot_id'] = 'other'
    if problem == 'ended':session.finish('Stream failed')
    if problem == 'duplicate':session.subscribe()
    with pytest.raises(RuntimeError):session.subscribe()


def test_queue_overflow_fails_episode_and_stops_real_stream(monkeypatch):
    session, transport, _ = ready()
    adopted = session.subscribe()
    monkeypatch.setattr(adopted,'MAX_BYTES',4)
    session.publish(StreamEvent(dict(type='image',camera_id='usb-1'),b'12345'))
    transport.request_stop.assert_called_once()
    with pytest.raises(RuntimeError,match='队列已满'):next(adopted.stream())
    assert session.end is None  # No synthetic success for an overflow.
    session.finish('Actual connection closed')
    adopted.close()


def test_missing_audited_end_never_becomes_success():
    session, _, _ = ready()
    adopted = session.subscribe()
    session.finish()
    with pytest.raises(RuntimeError,match='真实审计'):next(adopted.stream())
    with pytest.raises(RuntimeError,match='真实停止'):adopted.clock_sync();adopted.clock_sync()
    adopted.close()
