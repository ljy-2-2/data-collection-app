"""Standalone fake-only robot-runtime verification. Never connect or start ROS."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'teleop_control/vendor/kuavo_runtime/src'))
from teleop.robots.kuavo5_diagnostics import CollectionBuffer, SuccessTraceSampler
from teleop.robots.kuavo5_gripper_owner import OwnerGripperSession
from teleop.robots.damiao_gripper import MotorProfile, DamiaoGripperDriver
from teleop.robots.kuavo5_live_protocol import encode_robot_packet, decode_robot_packet


def profile():
    return MotorProfile('fake_left',1,0x181,-.5,.5,.02,1.,10.,.1,2.,10.,12.5,30.,10.)


class RecordingTests(unittest.TestCase):
    def test_publications_captured_before_diagnostic_sampling(self):
        now=[1_000_000_000]
        sampler=SuccessTraceSampler(clock=lambda:now[0]);record=CollectionBuffer()
        displayed=[];recorded=[]
        for i in range(100):
            p=dict(event='control_trace',publication_sequence_id=i+1,command_rad=[i]*14,
                publication_completed_monotonic_ns=now[0])
            record.accept(p)
            if sampler.select(p):displayed.append(p)
            if i%5==4:
                batch=record.drain(now_ns=now[0],samples={},dropped_feedback={},motors={})
                recorded.extend(batch['actions'])
            now[0]+=10_000_000
        self.assertEqual(len(displayed),10)
        self.assertEqual(len(recorded),100)
        self.assertEqual([r['publication_sequence_id'] for r in recorded],list(range(1,101)))
        held=record.drain(now_ns=now[0],samples={},dropped_feedback={},motors={})
        self.assertEqual(held['actions'],[])
        self.assertEqual(held['latest_action']['publication_sequence_id'],100)

    def test_overflow_counted_without_blocking_control(self):
        record=CollectionBuffer(capacity=2)
        for i in range(4):record.accept(dict(event='control_trace',command_rad=[i]*14))
        b=record.drain(now_ns=1,samples={},dropped_feedback={},motors={})
        self.assertEqual(b['dropped_actions'],2)
        self.assertEqual(len(b['actions']),2)

    def test_target_reference_only_changes_record_not_feedback_or_commands(self):
        p=profile();submit=Mock();d=DamiaoGripperDriver({'left':p},occupied_ids={p.bus:[]},submit=submit)
        code=32768;raw=bytes((1,code>>8,code&255,0x80,0x08,0,25,25))
        ref=dict(position_rad=.2,source_timestamp_ns=5,source='owner_acknowledged_target')
        d.receive(p.bus,p.master_id,raw,received_ns=10,recording_target=ref)
        samples=d.drain_feedback()['left']
        self.assertEqual(samples[0]['recording_target'],ref)
        self.assertNotEqual(d.feedback('left').position_rad,.2)
        submit.assert_not_called()

    def test_idle_queries_request_100hz_without_enable_or_target(self):
        p=profile();s=OwnerGripperSession('/unused',{'left':p},{p.bus:[]})
        clock=[1.];queries=[];registers=[]
        client=Mock();client.sock=object()
        def refresh(**kw):
            self.assertEqual(kw,{'expected_state':0})
            queries.append(clock[0]);clock[0]+=.002
            if len(queries)>=101:s._closing=True
        def register(*args):registers.append(clock[0]);clock[0]+=.001
        def advance(*args):clock[0]+=.0001;return ([],[],[])
        client.refresh_status.side_effect=refresh;client.register.side_effect=register
        s._clients={'left':client}
        with patch('teleop.robots.kuavo5_gripper_owner.time.monotonic',side_effect=lambda:clock[0]), \
             patch('teleop.robots.kuavo5_gripper_owner.select.select',side_effect=advance), \
             patch.object(s._condition,'wait',side_effect=lambda timeout:set_time(clock,timeout)):
            s._worker('left')
        self.assertFalse(s._errors)
        hz=(len(queries)-1)/(queries[-1]-queries[0])
        self.assertGreater(hz,98)
        self.assertLess(hz,102)
        self.assertLess(len(registers),len(queries)/3)
        client.send.assert_not_called();client.commission.assert_not_called()

    def test_active_motor_never_polled_or_enabled_for_recording(self):
        p=profile();s=OwnerGripperSession('/unused',{'left':p},{p.bus:[]})
        client=Mock();client.sock=object();s._clients={'left':client};s._possibly_enabled['left']=True
        def end(*args):
            # End test without invoking production close/disable semantics.
            s._possibly_enabled['left']=False;s._closing=True
            return ([],[],[])
        with patch('teleop.robots.kuavo5_gripper_owner.select.select',side_effect=end):s._worker('left')
        client.refresh_status.assert_not_called();client.register.assert_not_called();client.send.assert_not_called()

    def test_query_yields_to_new_takeover_without_queueing_old_command(self):
        p=profile();s=OwnerGripperSession('/unused',{'left':p},{p.bus:[]})
        s._started=True;s._reading.add('left')
        with patch('teleop.robots.kuavo5_gripper_owner.time.monotonic',return_value=1.), \
             patch('teleop.robots.kuavo5_gripper_owner.time.monotonic_ns',return_value=1_000_000_000):
            self.assertFalse(s.submit(p.bus,((p.command_can_id,bytes(8)),),1_050_000_000))
        self.assertEqual(s._pending,{})
        self.assertEqual(s._idle_yield_until['left'],1.02)
        s._reading.clear()
        with patch('teleop.robots.kuavo5_gripper_owner.time.monotonic_ns',return_value=1_010_000_000):
            self.assertTrue(s.submit(p.bus,((p.command_can_id,bytes([1])*8),),1_060_000_000))
        self.assertEqual(s._pending['left'][0][0][1],bytes([1])*8)


def set_time(clock,seconds):clock[0]+=seconds


if __name__=='__main__':unittest.main()
