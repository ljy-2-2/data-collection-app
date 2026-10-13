"""Fixed +5 degree yaw diagnostic using the continuous head interface; default read-only."""
import argparse
import fcntl
import importlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

from teleop.robots.g2_head_servo_diagnostic import run_diagnostic


class Journal:
    def __init__(self, path):
        # Exclusive creation: never overwrite a previous experiment.
        self.file = open(path, 'x', encoding='utf-8')

    def write(self, event, **data):
        self.file.write(json.dumps(dict(event=event, host_monotonic_ns=time.monotonic_ns(),
                                       **data), ensure_ascii=False, allow_nan=False) + '\n')
        self.file.flush()
        os.fsync(self.file.fileno())

    def close(self):
        self.file.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-motion', action='store_true',
                        help='REAL MOTION: clear area, hardware emergency stop ready; no automatic return')
    parser.add_argument('--log-file', type=Path, help='new JSONL file; an existing file is refused')
    args = parser.parse_args()
    stopped = threading.Event()
    handlers = {sig: signal.signal(sig, lambda *_: stopped.set())
                for sig in (signal.SIGINT, signal.SIGTERM)}
    journal = None
    try:
        with open(os.environ.get('SESSION_LOCK_FILE', '/tmp/galaxis-pico-g2-teleop.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.log_file:
                path = args.log_file
            else:
                folder = Path(__file__).resolve().parents[1] / 'artifacts/head-servo'
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / (time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.jsonl')
            journal = Journal(path)
            journal.write('launch', motion_enabled=args.enable_motion, argv=sys.argv[1:])
            print(f'HEAD_SERVO_DIAGNOSTIC_LOG path={path.resolve()}', flush=True)
            print('Fixed yaw +5deg; target rate <=0.2rad/s; send window <=5s; '
                  'software bounds only; physical emergency stop required.', flush=True)
            if stopped.is_set(): return 130
            # No SDK import before journal creation and exclusive session ownership.
            return run_diagnostic(importlib.import_module('agibot_gdk'), audit=journal.write,
                                  enable_motion=args.enable_motion, stop_requested=stopped.is_set)
    except Exception as error:
        if journal:
            try: journal.write('cli_error', reason=str(error))
            except Exception: pass
        print(f'HEAD_SERVO_DIAGNOSTIC_REFUSED {error}', file=sys.stderr, flush=True)
        return 2
    finally:
        if journal: journal.close()
        for sig, handler in handlers.items(): signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
