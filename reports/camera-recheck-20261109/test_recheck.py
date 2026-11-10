import importlib.util,json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
ROOT=Path(__file__).parent;sys.path.insert(0,str(ROOT/'controller'))
import camera_recheck as c
class RecoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  p=Path(self.tmp.name);(p/'stream-manifest.json').write_text(json.dumps({'sha256':{}}))
  self.service=SimpleNamespace(config={'helper':str(p/'capture'),'cooldown_seconds':0,'cameras':[]},runtime=str(p),fault='Invalid MJPEG; Audit changed: dmesg_sha256',last_stream={'error':'old xhci failure'},recovery_audit=None,last_finish=0,recovery_count=0)
  self.api=SimpleNamespace(ACTIVE=None,monotonic=lambda:10)
  self.stream=SimpleNamespace(audit=Mock(side_effect=[{'current':'a'},{'current':'a'}]),unchanged_for_recovery=Mock(return_value=True),sha=Mock(return_value='ok'))
  self.addCleanup(patch.stopall);patch.dict(sys.modules,streaming=self.stream).start();patch.object(c,'cleanup_orphans',return_value=[]).start();patch.object(c.time,'sleep').start();self.usb=patch.object(c,'recover_usb',side_effect=AssertionError('unexpected USB reset')).start();self.inv=patch.object(c,'inventory',return_value=[{'id':'usb-1','available':True}]).start()
 def test_old_lock_without_recovery_audit_is_rechecked(self):
  r=c.recheck(self.service,self.api,True);self.assertTrue(r['ok']);self.assertFalse(r['verified_stream']);self.assertIsNone(self.service.fault);self.assertEqual(self.stream.audit.call_count,2)
  record=json.loads((Path(self.tmp.name)/'camera-recovery-history.jsonl').read_text());self.assertIn('dmesg_sha256',record['previous_fault'])
 def test_startup_fault_runs_usb_recovery_then_checks_fresh_state(self):
  self.service.last_stream={'native_failure_reason':'Camera did not produce first frame after bounded startup retry; stop'}
  self.stream.audit.side_effect=[{'current':'a'},{'current':'b'},{'current':'b'}]
  self.usb.side_effect=None;self.usb.return_value={'ok':True}
  with patch.object(c,'controller_tasks_unchanged',return_value=True):result=c.recheck(self.service,self.api,True)
  self.assertTrue(result['ok']);self.assertTrue(result['usb_controller_reinitialized']);self.usb.assert_called_once()
 def test_usb_guard_refusal_preserves_original_fault(self):
  self.service.last_stream={'native_failure_reason':'Camera did not produce first frame after bounded startup retry; stop'}
  self.usb.side_effect=RuntimeError('Arms must be disabled')
  result=c.recheck(self.service,self.api,True)
  self.assertFalse(result['ok']);self.assertIsNotNone(self.service.fault)
 def test_requires_explicit_action(self):
  self.assertFalse(c.recheck(self.service,self.api,False)['ok']);self.stream.audit.assert_not_called()
 def test_live_capture_not_terminated(self):
  self.api.ACTIVE=Mock();self.api.ACTIVE.poll.return_value=None
  self.assertEqual(c.recheck(self.service,self.api,True)['error'],'camera_capture_active');self.stream.audit.assert_not_called()
 def test_current_audit_failure_preserves_fault(self):
  self.stream.unchanged_for_recovery.return_value=False
  self.assertFalse(c.recheck(self.service,self.api,True)['ok']);self.assertIsNotNone(self.service.fault)
 def test_busy_usb_preserves_fault(self):
  self.inv.side_effect=RuntimeError('Camera USB device is busy')
  self.assertFalse(c.recheck(self.service,self.api,True)['ok']);self.assertIsNotNone(self.service.fault)
 def test_enumeration_must_settle(self):
  self.inv.side_effect=[[{'id':'usb-1','address':2}],[{'id':'usb-1','address':3}]]
  self.assertFalse(c.recheck(self.service,self.api,True)['ok']);self.assertIsNotNone(self.service.fault)
 def test_program_integrity_checked(self):
  (Path(self.tmp.name)/'stream-manifest.json').write_text(json.dumps({'sha256':{'/fake/helper':'expected'}}))
  self.assertFalse(c.recheck(self.service,self.api,True)['ok']);self.inv.assert_not_called();self.assertIsNotNone(self.service.fault)
 def test_missing_camera_does_not_preserve_old_global_lock(self):
  self.inv.return_value=[{'id':'usb-1','available':False}]
  self.assertTrue(c.recheck(self.service,self.api,True)['ok']);self.assertIsNone(self.service.fault)
class PersistenceTests(unittest.TestCase):
 def test_failure_survives_service_restart_and_clear_removes_it(self):
  with tempfile.TemporaryDirectory() as runtime:
   first=SimpleNamespace(runtime=runtime,fault='first-frame failure',last_stream={'native_failure_reason':'Camera did not produce first frame after bounded startup retry; stop'})
   c.remember_failure(first)
   second=SimpleNamespace(runtime=runtime,fault=None,last_stream={})
   c.restore_failure(second)
   self.assertEqual(second.fault,first.fault);self.assertEqual(second.last_stream,first.last_stream)
   second.fault=None;c.remember_failure(second)
   self.assertFalse((Path(runtime)/'camera-failure-context.json').exists())
 def test_corrupt_context_does_not_break_service_start(self):
  with tempfile.TemporaryDirectory() as runtime:
   (Path(runtime)/'camera-failure-context.json').write_text('{broken')
   s=SimpleNamespace(runtime=runtime,fault=None,last_stream={});c.restore_failure(s)
   self.assertIsNone(s.fault)
class UsbPolicyTests(unittest.TestCase):
 def test_healthy_or_transport_fault_does_not_reset_bus(self):
  self.assertFalse(c.needs_usb_recovery(SimpleNamespace(fault=None,last_stream={'native_failure_reason':'Camera did not produce first frame after bounded startup retry; stop'})))
  self.assertFalse(c.needs_usb_recovery(SimpleNamespace(fault='pipe',last_stream={'native_failure_reason':'Frame pipe closed or blocked for 1 second; stop'})))
 def test_persistent_startup_fault_enters_bounded_recovery(self):
  self.assertTrue(c.needs_usb_recovery(SimpleNamespace(fault='no frames',last_stream={'native_failure_reason':'Camera did not produce first frame after bounded startup retry; stop'})))
 def load_helper(self,states):
  reader=Mock();reader.sample.return_value={'arm_state':states}
  spec=importlib.util.spec_from_file_location('usb_recovery_test',ROOT/'controller/camera_usb_recovery.py');mod=importlib.util.module_from_spec(spec)
  with patch.dict(sys.modules,server=SimpleNamespace(monotonic=lambda:0),joints=SimpleNamespace(JointReader=Mock(return_value=reader))):spec.loader.exec_module(mod)
  return mod
 def test_enabled_arms_refuse_before_usb_access(self):
  mod=self.load_helper([1,-1,0,0,-1,0])
  with patch.object(mod.glob,'glob') as paths:
   with self.assertRaisesRegex(AssertionError,'disabled'):mod.guard()
   paths.assert_not_called()
 def test_non_camera_usb_device_refuses_recovery(self):
  mod=self.load_helper([0,-1,0,0,-1,0])
  with patch.object(mod.os.path,'realpath',return_value=mod.DRIVER),patch.object(mod.os.path,'exists',return_value=True),patch.object(mod.glob,'glob',return_value=['/sys/bus/usb/devices/1-3']),patch.object(mod,'text',side_effect=['1234','abcd']):
   with self.assertRaisesRegex(AssertionError,'Non-camera'):mod.guard()
class FreshFrameTests(unittest.TestCase):
 def test_check_waits_for_frames_and_detects_startup_failure(self):
  from unittest.mock import MagicMock
  spec=importlib.util.spec_from_file_location('preview_wait',ROOT/'board/teleop_control/src/teleop_control/tianji_camera_recovery.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
  m=SimpleNamespace(camera_recovery_cancel=Mock(wait=Mock(return_value=False)),last_access=10,lock=MagicMock(),camera_inventory={'usb_1':'usb-1'},frames={},camera_frame_ttl=2,camera_thread=Mock(is_alive=Mock(return_value=False)),camera_error='Camera did not produce first frame after bounded startup retry; stop')
  with patch.object(mod.time,'monotonic',return_value=10):
   self.assertFalse(mod.wait_for_preview(m));self.assertTrue(mod.startup_retry_needed(m))
   m.frames={'usb_1':(b'frame',10)};self.assertTrue(mod.wait_for_preview(m))
class AppTests(unittest.TestCase):
 def setUp(self):
  p=ROOT/'board/teleop_control/src/teleop_control/tianji_camera_recovery.py';spec=importlib.util.spec_from_file_location('app_recovery',p);self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)
 def test_button_calls_fresh_recheck_even_when_old_flag_false(self):
  from unittest.mock import MagicMock
  client=MagicMock();client.__enter__.return_value=client;client.status.side_effect=[{'idle':True,'fault':'old xhci','recoverable_camera_fault':False,'camera_manual_recheck':True},{'idle':True,'fault':None}]
  m=SimpleNamespace(snapshot=lambda **kw:{'state':'idle','collection':{}},controller=SimpleNamespace(hardware_enabled=True),require_other_idle=Mock(),camera_session=None,camera_thread=None,camera_client=None,camera_recovery_cancel=Mock(is_set=Mock(return_value=False)),camera_stop=Mock(),lock=MagicMock(),frames={'usb_1':b'old'},new_camera_client=Mock(return_value=client))
  self.mod.restart_local_preview(m);client.recheck_cameras.assert_called_once();client.recover_camera.assert_not_called();self.assertEqual(m.frames,{})
 def test_active_collection_still_blocks(self):
  m=SimpleNamespace(snapshot=lambda **kw:{'state':'idle','collection':{'recording':True}},controller=SimpleNamespace(hardware_enabled=True),camera_session=None)
  with self.assertRaises(RuntimeError):self.mod.require_idle(m)
if __name__=='__main__':unittest.main()
