import sys,time,threading,importlib.util,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
from test_tianji_camera_session import ready
from fusion_camera import StreamEvent
ROOT=Path(__file__).resolve().parents[1]

def live():
 s,t,f=ready();s.start.metadata.update(stream_checkpoints=True,stream_live_clock=True)
 return s,t,f

def seal(s,ident=1,**kw):
 m=dict(type='checkpoint',ok=True,boot_id='test-boot',transport_continues=True,
  boundary_kind='controller_checkpoint',checkpoint_id=ident,native_exit=None,native_cleanup_performed=False,
  frames=[s.frame_counts[k] for k in ('usb-1','usb-2','usb-4')],joint_samples=s.joint_count,
  audit_before={'real':'before'},audit_after={'real':'after'})
 m.update(kw);s.publish(StreamEvent(m))

def test_two_disjoint_episodes_do_not_stop_or_reopen_transport():
 s,t,f=live();a=s.subscribe();it=a.stream();start=next(it).metadata
 s.publish(StreamEvent(dict(type='image',camera_id='usb-1',sequence=2),b'episode1'))
 assert next(it).data==b'episode1'
 a.request_stop();a.request_stop();t.request_checkpoint.assert_called_once();seal(s)
 end=next(it).metadata
 assert end['source_event_type']=='checkpoint' and end['native_exit'] is None
 assert end['frames'][0]-start['episode_count_baseline']['frames']['usb-1']==1
 with pytest.raises(StopIteration):next(it)
 assert s.end is None and not s.finished.is_set() and not s.attached
 s.publish(StreamEvent(dict(type='image',camera_id='usb-1',sequence=3),b'between'))
 b=s.subscribe();second=b.stream();start2=next(second).metadata
 a.close() # Closing the previous episode must not detach or stop the new one.
 assert s.consumer is b and s.attached
 s.publish(StreamEvent(dict(type='image',camera_id='usb-1',sequence=4),b'episode2'))
 assert next(second).data==b'episode2'
 b.request_stop();seal(s,2);end2=next(second).metadata
 assert end2['frames'][0]-start2['episode_count_baseline']['frames']['usb-1']==1
 b.close();t.request_stop.assert_not_called();f.assert_not_called()

def test_live_clock_has_fresh_independent_episode_anchors():
 s,t,f=live()
 def reply():
  now=time.monotonic_ns();s.publish(StreamEvent(dict(type='clock',boot_id='test-boot',controller_monotonic_ns=now+1000,pc_recv_monotonic_ns=now+2000)))
 t.request_live_clock.side_effect=reply
 a=s.subscribe();before=a.clock_sync(3);time.sleep(.001)
 a.request_stop();seal(s);after=a.clock_sync(3)
 assert after['pc_midpoint_ns']>before['pc_midpoint_ns']
 assert len(before['measurements'])==3 and before['boot_id']==after['boot_id']
 a.close();f.assert_not_called();t.request_stop.assert_not_called()

def test_checkpoint_count_or_boot_mismatch_cannot_mark_success():
 for changes in ({'frames':[999,1,1]},{'boot_id':'wrong'}):
  s,t,f=live();a=s.subscribe();a.request_stop()
  with pytest.raises(RuntimeError):seal(s,**changes)
  assert not a.boundary_received.is_set()

def test_overflow_aborts_only_episode_and_boundary_releases_it():
 s,t,f=live();a=s.subscribe();a.MAX_BYTES=1
 s.publish(StreamEvent(dict(type='image',camera_id='usb-1'),b'overflow'))
 assert a.stop_requested
 with pytest.raises(RuntimeError,match='队列已满'):next(a.stream())
 seal(s);a.close();assert not s.attached and s.failure is None
 t.request_stop.assert_not_called()

def test_service_failure_does_not_become_successful_boundary():
 s,t,f=live();a=s.subscribe();s.finish('connection lost')
 with pytest.raises(RuntimeError,match='connection lost'):next(a.stream())
 a.close();assert s.failure=='connection lost'

def test_checkpoint_log_snapshot_does_not_seek_native_writer():
 spec=importlib.util.spec_from_file_location('cp',ROOT/'fusion_data_collection/FusionDataCollector/controller_v22/stream_checkpoint.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 with tempfile.TemporaryFile() as f:
  f.write(b'first\n');f.flush();pos=f.tell();assert m.native_snapshot(f)==['first'];assert f.tell()==pos
  f.write(b'second\n');f.flush();assert m.native_snapshot(f)==['first','second']

def test_controller_checkpoint_uses_current_audit_and_rejects_faults():
 spec=importlib.util.spec_from_file_location('cp2',ROOT/'fusion_data_collection/FusionDataCollector/controller_v22/stream_checkpoint.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 api=SimpleNamespace(monotonic=lambda:1,read=lambda p:'boot');proc=Mock(poll=Mock(return_value=None))
 sampler=SimpleNamespace(error=None,feedback_error=None,gap_count=0,skipped=0,max_lateness_ns=0)
 backend=SimpleNamespace(audit=Mock(return_value={'state':'after'}),unchanged_for_recovery=Mock(return_value=True),native_queue_overflow=Mock(return_value={}))
 with tempfile.TemporaryFile() as f,patch.dict(sys.modules,streaming=backend):
  f.write(b'unsupported descriptor subtype VS_COLORFORMAT\nattempt to claim already-claimed interface 1\n');f.flush()
  result,_=m.checkpoint(api,{},proc,f,[3,4,5],100,sampler,['usb-1'],{'usb-1':999999999},1)
  assert result['ok'] and result['native_exit'] is None and result['audit_after']=={'state':'after'}
  backend.unchanged_for_recovery.return_value=False
  with pytest.raises(RuntimeError,match='audit changed'):m.checkpoint(api,{},proc,f,[3,4,5],100,sampler,['usb-1'],{'usb-1':999999999},2)

@pytest.mark.parametrize('changes', [
 {'checkpoint_id':0}, {'checkpoint_id':True}, {'ok':False},
 {'native_exit':0}, {'native_cleanup_performed':True},
])
def test_invalid_boundary_never_releases_recording(changes):
 s,transport,_=live();a=s.subscribe();a.request_stop()
 with pytest.raises(RuntimeError):seal(s,**changes)
 assert s.attached and not a.boundary_received.is_set()
 transport.request_stop.assert_not_called()


def test_replayed_boundary_is_rejected_for_next_episode():
 s,_,_=live();a=s.subscribe();a.request_stop();seal(s,1);a.close()
 b=s.subscribe();b.request_stop()
 with pytest.raises(RuntimeError):seal(s,1)
 assert s.consumer is b and s.attached


def test_failed_clock_requires_reconnect_before_new_recording():
 s,_,_=live();s.clock_failure='clock timeout'
 with pytest.raises(RuntimeError):s.subscribe()
