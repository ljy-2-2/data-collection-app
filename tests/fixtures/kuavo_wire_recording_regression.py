"""Captured burst regression. Pure functions and injected owner; no hardware."""
from dataclasses import replace
import time
import unittest
from unittest.mock import Mock, patch

from kuavo_recording_runtime_fake import profile
from teleop.robots.damiao_gripper import encode_position, DISABLE
from teleop.robots.kuavo5_gripper_live import OwnerExpired
from teleop.robots.kuavo5_gripper_owner import (
    OwnerGripperSession, limit_wire_target, wire_position)


def measured_profile(side='left'):
    return replace(profile(), bus='canbus0' if side == 'left' else 'canbus1',
                   motor_id=8 if side == 'left' else 1, master_id=0,
                   open_rad=-2.7803845273517975 if side == 'left' else -2.3195620660715655,
                   closed_rad=-1.3502327000839252 if side == 'left' else -.9622720683604182,
                   speed_rad_s=.2, accel_rad_s2=1., imax_a=20.522388458251953,
                   vmax=50., control_mode=1, kp=2., kd=.1, max_torque_nm=.2)


class WireRateTests(unittest.TestCase):
    def test_captured_bilateral_bursts_violate_native_speed_bound_before_fix(self):
        # owner_ack_ns timestamps from 20261014-135414-5af1f4ff.jsonl.
        for side, previous, requested, elapsed in (
            ('left', '6f427ff0100517ff', '6f477ff0100517ff', 2051433),
            ('right', '72077ff0100517ff', '72047ff0100517ff', 2053411)):
            with self.subTest(side=side):
                p = measured_profile(side)
                previous, requested = bytes.fromhex(previous), bytes.fromhex(requested)
                old, wanted = wire_position(p, previous), wire_position(p, requested)
                native_bound = p.speed_rad_s*elapsed/1e9+2*p.pmax/65535+.00001
                self.assertGreater(abs(wanted-old), native_bound)
                actual = limit_wire_target(p, requested, previous, elapsed)
                self.assertLessEqual(abs(wire_position(p, actual)-old), p.speed_rad_s*elapsed/1e9+1e-12)
                self.assertEqual(actual[2:], requested[2:])
                self.assertLessEqual(p.bounds[0], wire_position(p, actual))
                self.assertGreaterEqual(p.bounds[1], wire_position(p, actual))

    def test_bursty_stream_and_reversal_never_bank_dropped_targets(self):
        p = measured_profile()
        previous = encode_position(p, -2.)
        total_ns = 0
        distance = 0.
        for i in range(1000):
            elapsed = (30_000_000, 2_000_000, 2_000_000, 10_000_000)[i % 4]
            requested = encode_position(p, p.bounds[i//25 % 2])
            actual = limit_wire_target(p, requested, previous, elapsed)
            step = abs(wire_position(p, actual)-wire_position(p, previous))
            self.assertLessEqual(step, p.speed_rad_s*elapsed/1e9+1e-12)
            distance += step; total_ns += elapsed
            previous = actual
        self.assertGreater(distance, .5)  # Pacing still allows progress.
        self.assertLessEqual(distance, p.speed_rad_s*total_ns/1e9)

    def test_zero_elapsed_missing_seed_and_mode4_fields(self):
        p = measured_profile()
        old, wanted = encode_position(p, -2.), encode_position(p, -1.8)
        self.assertEqual(limit_wire_target(p, wanted, old, 0), old)
        with self.assertRaisesRegex(RuntimeError, 'acknowledged seed'):
            limit_wire_target(p, wanted, None, 1_000_000)
        p = profile()
        old, wanted = encode_position(p, -.2), encode_position(p, .2)
        actual = limit_wire_target(p, wanted, old, 2_000_000)
        self.assertEqual(actual[4:], wanted[4:])
        self.assertLessEqual(abs(wire_position(p, actual)-wire_position(p, old)),
                             p.speed_rad_s*.002+1e-7)

    def test_worker_limits_at_send_and_keeps_original_deadline(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                p = measured_profile()
                s = OwnerGripperSession('/unused', {'left': p}, {p.bus: []})
                c = Mock(); s._clients['left'] = c
                s._possibly_enabled['left'] = True
                old = bytes.fromhex('6f427ff0100517ff')
                wanted = bytes.fromhex('6f477ff0100517ff')
                s._wire_payload['left'] = old
                now = time.monotonic_ns()
                s._wire_ack_ns['left'] = now-2_000_000
                deadline = now+50_000_000
                s._pending['left'] = (((p.command_can_id, wanted),), deadline, 0)
                calls = []
                def send(cid, payload, **kw):
                    calls.append((payload, kw['deadline_ns']))
                    s._closing = True
                    if payload != DISABLE and expired:
                        raise OwnerExpired('explicitly unsent')
                    return now
                c.send.side_effect = send
                # Exercise limiting independently; the pacing gate is covered
                # below and by the real native fake serial integration tests.
                s._wait_target_slot = Mock(return_value=True)
                with patch('teleop.robots.kuavo5_gripper_owner.time.monotonic_ns', return_value=now):
                    s._worker('left')
                self.assertEqual(s.status()['errors'], {})
                self.assertEqual(calls[0][1], deadline)
                self.assertEqual(calls[0][0], limit_wire_target(p, wanted, old, 2_000_000))
                self.assertEqual(calls[1][0], DISABLE)
                self.assertEqual(len(calls), 2)
                self.assertEqual(s._wire_ack_ns['left'], now-2_000_000 if expired else now)
                self.assertEqual(s.status()['dropped_unsent_targets']['left'], int(expired))

    def test_slot_wait_is_bounded_by_original_lease_and_revocation(self):
        p = measured_profile()
        s = OwnerGripperSession('/unused', {'left': p}, {p.bus: []})
        now = 1_000_000_000
        s._wire_ack_ns['left'] = now
        clock = [now]
        def wait(seconds):
            clock[0] += round(seconds*1e9)
        with patch('teleop.robots.kuavo5_gripper_owner.time.monotonic_ns', side_effect=lambda: clock[0]), \
                patch.object(s._condition, 'wait', side_effect=wait):
            self.assertFalse(s._wait_target_slot('left', now+5_000_000, 0))
            self.assertEqual(clock[0], now+5_000_000)
            self.assertTrue(s._wait_target_slot('left', now+50_000_000, 0))
            self.assertEqual(clock[0], now+10_000_000)
            s._generation['left'] += 1
            self.assertFalse(s._wait_target_slot('left', now+50_000_000, 0))
            s._closing = True
            self.assertFalse(s._wait_target_slot('left', now+50_000_000, 1))


if __name__ == '__main__':
    unittest.main()
