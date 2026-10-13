"""Create an isolated, timestamp-instrumented libuvc build. Never run a camera."""
import argparse
from pathlib import Path
import shutil


def replace_once(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise RuntimeError('unexpected vendor source: ' + str(path))
    path.write_text(text.replace(old, new))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('sdk', type=Path)
    p.add_argument('destination', type=Path)
    a = p.parse_args()
    a.destination.mkdir(exist_ok=False)
    shutil.copytree(a.sdk / 'vendor', a.destination / 'vendor')
    for name in ('capture_three.c', 'frame_queue.c', 'frame_queue.h', 'mjpeg_mode.h'):
        shutil.copyfile(Path(__file__).with_name(name), a.destination / name)
    lib = a.destination / 'vendor/libuvc-0.0.6'
    replace_once(lib / 'include/libuvc/libuvc.h', '  struct timeval capture_time;',
                 '  struct timeval capture_time;\n  uint64_t diag_monotonic_ns;\n'
                 '  uint32_t diag_pts, diag_scr;')
    replace_once(lib / 'include/libuvc/libuvc_internal.h', '  uint32_t seq, hold_seq;',
                 '  uint32_t seq, hold_seq;\n  uint64_t diag_hold_monotonic_ns;')
    replace_once(lib / 'src/stream.c', '  strmh->hold_seq = strmh->seq;',
                 '  strmh->hold_seq = strmh->seq;\n'
                 '  struct timespec diag_ts;\n'
                 '  clock_gettime(CLOCK_MONOTONIC, &diag_ts);\n'
                 '  strmh->diag_hold_monotonic_ns = (uint64_t)diag_ts.tv_sec * 1000000000ULL + diag_ts.tv_nsec;')
    replace_once(lib / 'src/stream.c', '  // frame->capture_time',
                 '  frame->diag_monotonic_ns = strmh->diag_hold_monotonic_ns;\n'
                 '  frame->diag_pts = strmh->hold_pts;\n'
                 '  frame->diag_scr = strmh->hold_last_scr;')
    # Require exact UVC control transfer lengths and bounded control requests.
    replace_once(lib / 'src/stream.c', '      buf, len, 0\n', '      buf, len, 1000\n')
    replace_once(lib / 'src/stream.c', '  if (err <= 0) {\n    return err;\n  }',
                 '  if (err < 0) return err;\n  if ((size_t)err != len) return UVC_ERROR_IO;')
    (a.destination / 'build.sh').write_text('''#!/bin/sh
set -eu
cd "$(dirname "$0")"
ulimit -c 0
ulimit -t 20
ulimit -v 262144
test "$(uname -r)" = 3.14.18-ipipe
timeout -k 2 30s nice -n 19 taskset -c 3 gcc -std=gnu99 -O1 -pthread \\
 -Ivendor -Ivendor/libuvc-0.0.6/include capture_three.c frame_queue.c \\
 vendor/libuvc-0.0.6/src/ctrl.c vendor/libuvc-0.0.6/src/ctrl-gen.c \\
 vendor/libuvc-0.0.6/src/device.c vendor/libuvc-0.0.6/src/diag.c \\
 vendor/libuvc-0.0.6/src/frame.c vendor/libuvc-0.0.6/src/init.c \\
 vendor/libuvc-0.0.6/src/stream.c vendor/libuvc-0.0.6/src/misc.c \\
 /lib/i386-linux-gnu/libusb-1.0.so.0 -lrt -o capture-three
''')


if __name__ == '__main__':
    main()
