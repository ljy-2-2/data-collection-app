import unittest
from receive import pair_frames, summary


def records(times, seqs=None):
    return [{'controller_monotonic_ns': t, 'sequence': s}
            for t,s in zip(times, seqs if seqs is not None else range(len(times)))]


class AlignmentTests(unittest.TestCase):
    def test_no_frame_reuse_or_timestamp_rewriting(self):
        rows = [records([1_000_000_000+j*33_333_333+i*2_000_000 for j in range(100)]) for i in range(3)]
        pairs = pair_frames(rows)
        self.assertEqual(len(pairs), 100)
        for i in range(3):
            self.assertEqual(len({p['indices'][i] for p in pairs}), 100)
            for p in pairs:
                self.assertEqual(p['timestamps_ns'][i], rows[i][p['indices'][i]]['controller_monotonic_ns'])
        self.assertEqual(pairs[0]['span_ms'], 4)

    def test_missing_frames_remain_unpaired(self):
        rows = [records([1_000_000_000+j*33_333_333 for j in range(10)]) for _ in range(3)]
        del rows[1][4]
        result, _ = summary(rows)
        self.assertEqual(result['paired_triplets'], 9)
        self.assertEqual(result['unpaired_frames'], [1,0,1])
        self.assertEqual(result['cameras'][1]['sequence_gaps'], 1)

    def test_excessive_skew_rejected(self):
        rows = [records([1_000_000_000+i*11_000_000]) for i in range(3)]
        self.assertEqual(pair_frames(rows), [])

    def test_empty_stream_reported(self):
        result, _ = summary([[],[],[]])
        self.assertIsNone(result['paired_span_ms'])
        self.assertIsNone(result['cameras'][0]['steady_fps_after_2s'])


if __name__ == '__main__':
    unittest.main()
