"""Offline target matching must choose nearest time on either side of a frame."""
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'fusion_camera_stream'), str(ROOT/'fusion_data_collection')]
from FusionDataCollector.collector import attach_actions


class ActionAlignmentTests(unittest.TestCase):
    def align(self, command_ms, frame_ms, before=None, after=None):
        rows = [dict(pc_monotonic_ns=round(t*1e6), joints_deg={'left':[i]*7},
                     gripper_targets_rad={'left':i/10},
                     gripper_target_sources={'left':{'sequence':i}})
                for i, t in enumerate(command_ms)]
        bundles = [dict(timestamp_ns=round(t*1e6), tensors={}) for t in frame_ms]
        before = before or dict(boot_id='test', pc_midpoint_ns=0, offset_ns=0, uncertainty_ns=1)
        after = after or dict(boot_id='test', pc_midpoint_ns=10_000_000_000, offset_ns=0, uncertainty_ns=1)
        attach_actions(rows, bundles, before, after, ('left',))
        return rows, bundles

    def test_nearest_both_sides_exact_match_and_tie(self):
        rows, bundles = self.align([0, 100], [40, 50, 60, 100])
        self.assertEqual([b['action_sample_index'] for b in bundles], [0, 0, 1, 1])
        self.assertEqual([b['action_delta_ms'] for b in bundles], [-40, -50, 40, 0])
        for b in bundles:
            i=b['action_sample_index']
            self.assertEqual(b['action_timestamp_ns'], rows[i]['timestamp_ns'])
            self.assertEqual(b['action_alignment_kind'], 'nearest_timestamp')
            self.assertEqual(b['tensors']['action__joint__position'], [math.radians(i)]*7)
            self.assertEqual(b['tensors']['action__left_effector__position'], [i/10])
            self.assertEqual(b['gripper_target_sources'], {'left':{'sequence':i}})

    def test_first_last_and_symmetric_tolerance_boundaries(self):
        _, bundles = self.align([0, 100], [-150.001, -150, -1, 101, 250, 250.001])
        self.assertEqual([b.get('action_sample_index') for b in bundles], [None, 0, 0, 1, 1, None])
        for b in (bundles[0], bundles[-1]):
            self.assertEqual(b['tensors'], {})
            self.assertNotIn('action_timestamp_ns', b)

    def test_empty_commands_stay_missing(self):
        _, bundles = self.align([], [0, 100])
        self.assertTrue(all(b['tensors'] == {} for b in bundles))

    def test_exact_duplicate_keeps_latest_record(self):
        _, bundles = self.align([0, 100, 100, 200], [100])
        self.assertEqual(bundles[0]['action_sample_index'], 2)

    def test_real_recording_boundary_selects_slightly_later_command(self):
        # Episode 6: the old prior-only rule chose 17.67 ms old data even though
        # an SDK-accepted target was only 0.57 ms later than this frame.
        _, bundles = self.align([7825.176192, 7840.821690, 7859.068028, 7867.291023,
                                 7874.664036, 7884.684011],
                                [7827.794827, 7858.494531, 7890.447025])
        self.assertEqual([b['action_sample_index'] for b in bundles], [0, 2, 5])
        self.assertAlmostEqual(bundles[1]['action_delta_ms'], .573497)

    def test_matching_uses_clock_mapping_with_offset_and_drift(self):
        before=dict(boot_id='test', pc_midpoint_ns=0, offset_ns=100_000_000, uncertainty_ns=1)
        after=dict(boot_id='test', pc_midpoint_ns=1_000_000_000, offset_ns=200_000_000, uncertainty_ns=2)
        rows, bundles = self.align([100, 300], [400], before, after)
        self.assertEqual([r['timestamp_ns'] for r in rows], [210_000_000, 430_000_000])
        self.assertEqual(bundles[0]['action_sample_index'], 1)
        self.assertEqual(bundles[0]['action_delta_ms'], 30)

    def test_invalid_clock_anchors_still_rejected(self):
        before=dict(boot_id='test', pc_midpoint_ns=0, offset_ns=0, uncertainty_ns=1)
        for after in (dict(before, boot_id='other', pc_midpoint_ns=10), before):
            with self.assertRaises(ValueError):
                self.align([0], [0], before, after)
