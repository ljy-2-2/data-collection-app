"""Receive diagnostic stdout on the laptop; save all frames and original timestamps.

Does not connect, start services, or change cameras. A successful receive alone
is not a successful robot safety or exposure-synchronization test.
"""
import argparse
import csv
import json
import io
import os
from pathlib import Path
import select
import shutil
import struct
import sys
import time

HEADER = struct.Struct('<4sIIQIIIII')
FRAME_BYTES = 640 * 480 * 2
MAX_FRAME_BYTES = 2 * 1024 * 1024


def percentiles(values):
    values = sorted(values)
    if not values:
        return None
    return {name: values[round((len(values)-1)*q)]
            for name, q in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1.)]}


def pair_frames(rows, tolerance_ns=20_000_000):
    """Greedy ordered, one-to-one pairing; drop oldest until span is acceptable.

    This does not rewrite timestamps, duplicate frames, or claim optimal matching.
    """
    cursor = [0, 0, 0]
    pairs = []
    while all(cursor[i] < len(rows[i]) for i in range(3)):
        times = [rows[i][cursor[i]]['controller_monotonic_ns'] for i in range(3)]
        span = max(times) - min(times)
        if span <= tolerance_ns:
            pairs.append({'indices': cursor.copy(), 'timestamps_ns': times, 'span_ms': span/1e6})
            cursor = [n+1 for n in cursor]
        else:
            cursor[times.index(min(times))] += 1
    return pairs


def summary(rows):
    stats = []
    for records in rows:
        timestamps = [r['controller_monotonic_ns'] for r in records]
        intervals = [(b-a)/1e6 for a, b in zip(timestamps, timestamps[1:])]
        gaps = sum((b['sequence']-a['sequence']-1) % 2**32 for a, b in zip(records, records[1:]))
        steady = [t for t in timestamps if t >= timestamps[0]+2_000_000_000] if timestamps else []
        fps = (len(steady)-1)*1e9/(steady[-1]-steady[0]) if len(steady)>1 else None
        stats.append({'frames': len(records), 'steady_fps_after_2s': fps,
                      'recorded_span_seconds': (timestamps[-1]-timestamps[0])/1e9 if len(timestamps)>1 else 0,
                      'sequence_gaps': gaps, 'frame_interval_ms': percentiles(intervals)})
    pairs = pair_frames(rows)
    return {'cameras': stats, 'pair_tolerance_ms': 20, 'paired_triplets': len(pairs),
            'unpaired_frames': [len(r)-len(pairs) for r in rows],
            'paired_span_ms': percentiles([p['span_ms'] for p in pairs]),
            'timestamp_meaning': 'controller CLOCK_MONOTONIC at libuvc frame-buffer completion; not exposure time',
            'hardware_synchronized': False}, pairs


def read_exact(fd, count, deadline, allow_eof=False, idle_timeout=3.):
    data = bytearray()
    while len(data) < count:
        left = min(idle_timeout, deadline-time.monotonic())
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise TimeoutError('stream timeout')
        chunk = os.read(fd, count-len(data))
        if not chunk:
            if allow_eof and not data:
                return None
            raise EOFError('truncated frame or header')
        data.extend(chunk)
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, required=True, choices=range(1, 1801))
    parser.add_argument('--format', choices=('yuyv','mjpeg'), default='yuyv')
    parser.add_argument('--width',type=int,default=640)
    parser.add_argument('--height',type=int,default=480)
    parser.add_argument('--decode',action='store_true',help='Decode JPEG and resize to 640x480 on laptop')
    parser.add_argument('--output', type=Path, required=True)
    a = parser.parse_args()
    if (a.width,a.height) not in ((640,480),(1024,768)) or (a.format=='yuyv' and (a.width,a.height)!=(640,480)):
        parser.error('unsupported dimensions')
    if a.decode:
        if a.format!='mjpeg': parser.error('--decode requires mjpeg')
        from PIL import Image
    a.output.mkdir(parents=True, exist_ok=False)
    # Reserve the worst case even when compressed frames are expected.
    max_bytes = MAX_FRAME_BYTES if a.format == 'mjpeg' else FRAME_BYTES
    # Full raw recordings: 5 min ~16.6 GB; never silently fall back to discarding.
    if shutil.disk_usage(a.output).free < a.seconds*max_bytes*96 + 2_000_000_000:
        raise RuntimeError('insufficient free space for bounded raw recording')
    rows = [[], [], []]
    files = [open(a.output / ('usb-%d.%s' % (port,a.format)), 'xb') for port in (1,2,4)]
    offsets = [0, 0, 0]
    decoded = [0, 0, 0]
    decode_seconds = 0.0
    error = None
    deadline = time.monotonic()+a.seconds+30
    last_progress = time.monotonic()
    try:
        with open(a.output / 'frames.csv', 'x', newline='') as index:
            fields = ['camera', 'sequence', 'controller_monotonic_ns', 'laptop_receive_monotonic_ns',
                      'device_pts_raw', 'device_scr_raw', 'file_offset', 'bytes', 'format', 'width', 'height']
            writer = csv.DictWriter(index, fieldnames=fields)
            writer.writeheader()
            while True:
                header = read_exact(0, HEADER.size, deadline, True, 30. if not any(rows) else 3.)
                if header is None:
                    break
                magic, cam, seq, stamp, size, w, h, pts, scr = HEADER.unpack(header)
                expected_magic = b'FJM1' if a.format == 'mjpeg' else b'FUV1'
                valid_size = 4 <= size <= MAX_FRAME_BYTES if a.format == 'mjpeg' else size == FRAME_BYTES
                if magic != expected_magic or cam > 2 or not valid_size or (w,h) != (a.width,a.height) or not stamp:
                    raise ValueError('invalid frame header')
                if len(rows[cam]) > (a.seconds+3)*32:
                    raise ValueError('frame count exceeds 30 Hz test allowance')
                if rows[cam] and stamp <= rows[cam][-1]['controller_monotonic_ns']:
                    raise ValueError('timestamp is not strictly increasing')
                frame = read_exact(0,size,deadline)
                if a.format == 'mjpeg' and frame[:2] != b'\xff\xd8':
                    raise ValueError('missing JPEG start marker')
                received = time.monotonic_ns()
                if a.decode:
                    decode_start=time.monotonic()
                    with Image.open(io.BytesIO(frame)) as im:
                        if im.format!='JPEG' or im.size!=(w,h): raise ValueError('JPEG dimensions disagree with header')
                        im.load()
                        output=im.convert('RGB').resize((640,480),Image.Resampling.BILINEAR)
                    decoded[cam]+=1
                    decode_seconds+=time.monotonic()-decode_start
                    if decoded[cam] in (1,900,1790):
                        output.save(a.output/('usb-%d-output-%d.png'%((1,2,4)[cam],decoded[cam])))
                files[cam].write(frame)
                record = dict(zip(fields, [cam,seq,stamp,received,pts,scr,offsets[cam],size,a.format,w,h]))
                writer.writerow(record)
                rows[cam].append(record)
                offsets[cam] += size
                if time.monotonic()-last_progress > 5:
                    print(json.dumps({'received': [len(r) for r in rows], 'bytes': sum(offsets)}), flush=True)
                    index.flush()
                    last_progress = time.monotonic()
    except Exception as exc:
        error = str(exc)
    finally:
        for f in files:
            f.close()
        report, pairs = summary(rows)
        report['native_dimensions'] = [a.width,a.height]
        report['output_dimensions'] = [640,480] if a.decode else None
        report['decoded_frames'] = decoded
        report['decode_and_resize_seconds'] = decode_seconds
        report['format'] = a.format
        report['receive_error'] = error
        report['requested_seconds'] = a.seconds
        report['all_streams_cover_requested_window_with_2s_margin'] = all(
            c['recorded_span_seconds'] >= max(.5, a.seconds-2) for c in report['cameras'])
        report['remote_exit_and_rt_health_must_be_checked_separately'] = True
        (a.output / 'summary.json').write_text(json.dumps(report, indent=2)+'\n')
        with open(a.output / 'pairs.jsonl', 'x') as out:
            for pair in pairs:
                out.write(json.dumps(pair)+'\n')
        print(json.dumps(report, indent=2), flush=True)
    return 1 if error or any(not r for r in rows) else 0


if __name__ == '__main__':
    sys.exit(main())
