"""Read-only UMI hand speed audit; these are NOT robot joint velocities."""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from mcap.reader import make_reader

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/umi_replay_sim'))
from umi_data import CDR

DATA = Path('/tmp/umi-speed-43-133-20261102')
OUT = Path(__file__).resolve().parent


def angular_steps(q):
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    # Sign-invariant shortest rotation, avoiding Euler wrap-around.
    return np.degrees(2 * np.arccos(np.clip(np.abs(np.sum(q[1:] * q[:-1], axis=1)), 0, 1)))


def read(path):
    streams = {'left': [], 'right': []}
    joint_names = set()
    with path.open('rb') as f:
        for schema, channel, msg in make_reader(f, validate_crcs=True).iter_messages(log_time_order=False):
            topic = channel.topic
            if topic not in ['/pico/controller/left/state', '/pico/controller/right/state', '/joint_states']:
                continue
            assert channel.message_encoding == 'cdr'
            r = CDR(msg.data)
            stamp, frame = r.header()
            if topic == '/joint_states':
                assert schema.name == 'sensor_msgs/msg/JointState'
                joint_names.update(r.string() for _ in range(r.read('I', 4)))
                continue
            assert schema.name == 'galaxis_pico_msgs/msg/PicoController'
            device, mono, seq, device_id = r.read('Q', 8), r.read('Q', 8), r.read('I', 4), r.string()
            values = [r.read('d', 8) for _ in range(7)]
            tracked, valid = bool(r.read('B', 1)), bool(r.read('B', 1))
            streams[topic.split('/')[3]].append((stamp, device, mono, seq, device_id, frame, values, tracked, valid))
    return streams, sorted(joint_names)


def analyze(rows):
    assert len(rows) >= 2
    t = np.array([r[1] for r in rows], dtype=np.int64)
    mono = np.array([r[2] for r in rows], dtype=np.int64)
    dt = np.diff(t) / 1e9
    v = np.array([r[6] for r in rows])
    assert np.isfinite(v).all()
    norms = np.linalg.norm(v[:, 3:], axis=1)
    assert np.all((norms > .95) & (norms < 1.05))
    tracking = np.array([r[7] and r[8] for r in rows])
    good = (dt > 0) & tracking[1:] & tracking[:-1]
    angles = angular_steps(v[:, 3:])
    angular = angles[good] / dt[good]
    linear = np.linalg.norm(np.diff(v[:, :3], axis=0), axis=1)[good] / dt[good]
    indices = np.flatnonzero(good)
    offsets = [int(a) - int(b) for a, b in zip(t, mono)]
    clock_issues = []
    if np.any(t <= 0) or np.any(mono <= 0): clock_issues.append('missing_timestamp')
    if np.any(dt <= 0): clock_issues.append('device_time_not_increasing')
    if np.any(np.diff(mono) <= 0): clock_issues.append('receive_time_not_increasing')
    if max(offsets) - min(offsets) > 100_000_000: clock_issues.append('clock_offset_spread_over_100ms')
    if any(b[3] <= a[3] for a, b in zip(rows, rows[1:])): clock_issues.append('sequence_not_increasing')
    if len({r[4] for r in rows}) != 1 or not rows[0][4]: clock_issues.append('device_id_inconsistent')
    if len({r[5] for r in rows}) != 1 or not rows[0][5]: clock_issues.append('frame_inconsistent')
    ai, li = int(np.argmax(angular)), int(np.argmax(linear))
    peak = int(indices[ai])
    over = angular > 180
    # A separate 50-ms-or-longer window check reduces single-interval timing jitter.
    smooth = []
    if not clock_issues and tracking.all():
        cumulative = np.r_[0, np.cumsum(angles)]
        for i in range(len(t) - 1):
            j = int(np.searchsorted(t, int(t[i]) + 50_000_000))
            if j < len(t) and np.max(dt[i:j]) <= .1:
                smooth.append((cumulative[j] - cumulative[i]) / ((int(t[j]) - int(t[i])) / 1e9))
    return dict(samples=len(rows), interval_count=len(dt), measured_intervals=len(angular),
        excluded_intervals=int((~good).sum()), invalid_tracking_samples=int((~tracking).sum()),
        source_duration_s=float((int(t[-1]) - int(t[0])) / 1e9),
        measured_duration_s=float(dt[good].sum()),
        clock_issues=clock_issues, clock_offset_spread_ms=(max(offsets)-min(offsets))/1e6,
        min_interval_ms=float(dt.min()*1000), median_interval_ms=float(np.median(dt)*1000),
        max_interval_ms=float(dt.max()*1000), gap_over_100ms_count=int((dt>.1).sum()),
        max_angular_deg_s=float(angular[ai]), p99_angular_deg_s=float(np.percentile(angular, 99)),
        max_linear_m_s=float(linear[li]), p99_linear_m_s=float(np.percentile(linear,99)),
        over_180_intervals=int(over.sum()), over_180_duration_s=float(dt[good][over].sum()),
        over_180_time_percent=float(100 * dt[good][over].sum() / dt[good].sum()),
        max_50ms_mean_angular_deg_s=float(max(smooth)) if smooth else None,
        angular_peak=dict(from_frame_1based=peak+1,to_frame_1based=peak+2,
            source_elapsed_s=(int(t[peak+1])-int(t[0]))/1e9,
            angle_deg=float(angles[peak]), interval_ms=float(dt[peak]*1000)),
        max_rotation_step_deg=float(angles.max()))


def main():
    # Analytical cross-check: 20 degrees in 0.1 sec = 200 deg/sec; q/-q is static.
    q = np.array([[0,0,0,1],[0,0,np.sin(np.radians(10)),np.cos(np.radians(10))]])
    assert np.isclose(angular_steps(q)[0] / .1, 200)
    assert np.isclose(angular_steps(np.array([[0,0,0,1],[0,0,0,-1]]))[0], 0)
    manifest = json.loads((OUT/'manifest.json').read_text())
    results = []
    for row in manifest['episodes']:
        path = DATA/row['id']/'signals.mcap'
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['files']['signals.mcap']
        streams, joints = read(path)
        results.append(dict(row=row['row'], id=row['id'], folder=row['folder'],
                            joint_names=joints, sides={s:analyze(values) for s,values in streams.items()}))
    stats = {}
    for side in ('left','right'):
        entries = [(r['row'],r['sides'][side]) for r in results]
        peakrow, peak = max(entries, key=lambda x:x[1]['max_angular_deg_s'])
        linrow, lin = max(entries, key=lambda x:x[1]['max_linear_m_s'])
        stats[side] = dict(episodes_over_180=sum(x['over_180_intervals']>0 for _,x in entries),
            episodes_50ms_mean_over_180=sum((x['max_50ms_mean_angular_deg_s'] or 0)>180 for _,x in entries),
            max_angular_deg_s=peak['max_angular_deg_s'], peak_row=peakrow, peak_detail=peak['angular_peak'],
            max_linear_m_s=lin['max_linear_m_s'], linear_peak_row=linrow,
            measured_intervals=sum(x['measured_intervals'] for _,x in entries),
            over_180_intervals=sum(x['over_180_intervals'] for _,x in entries),
            over_180_duration_s=sum(x['over_180_duration_s'] for _,x in entries),
            measured_duration_s=sum(x['measured_duration_s'] for _,x in entries),
            clock_issue_rows=[n for n,x in entries if x['clock_issues']],
            invalid_tracking_rows=[n for n,x in entries if x['invalid_tracking_samples']],
            gap_rows=[n for n,x in entries if x['gap_over_100ms_count']])
    summary = dict(row_range=[43,133], episode_count=len(results),
        episodes_either_hand_over_180=sum(any(x['over_180_intervals'] for x in r['sides'].values()) for r in results),
        episodes_neither_hand_over_180=[r['row'] for r in results if not any(x['over_180_intervals'] for x in r['sides'].values())],
        joint_names=sorted({n for r in results for n in r['joint_names']}), sides=stats)
    report = dict(method='Consecutive quaternion shortest rotation / PICO device_time_ns delta; raw unsmoothed controller pose; NOT robot joint speed. Position differences in metres / same delta. Both endpoints must be tracked+valid; positive time intervals only. Clock issues and gaps reported separately.',
                  summary=summary, episodes=results)
    (OUT/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print('TOP:',sorted([(r['row'],s,x['max_angular_deg_s']) for r in results for s,x in r['sides'].items()],key=lambda x:-x[2])[:10])


if __name__ == '__main__': main()
