"""Read-only raw GDK head diagnostics. Never issues motion or enable requests."""

import argparse
from collections.abc import Mapping
import importlib
import json
import math
import signal
import sys
import time
from teleop.robots.g2 import make_g2_joint_schema

HEAD_NAMES = ('idx11_head_joint1', 'idx12_head_joint2', 'idx13_head_joint3')
FIELDS = ('motor_position', 'motor_velocity', 'motor_current', 'mode', 'error_code')


def inspect_sample(payload, previous_timestamp):
    issues, heads = [], []
    schema = make_g2_joint_schema()
    raw_payload_repr = repr(payload)
    if not isinstance(payload, Mapping):
        payload = {}
        issues.append('invalid_payload')
    timestamp = payload.get('timestamp')
    if type(timestamp) is not int or timestamp <= 0:
        timestamp, progress = None, 'unknown'
        issues.append('missing_or_invalid_source_timestamp')
    elif previous_timestamp is None:
        progress = 'first'
    else:
        progress = ('advanced' if timestamp > previous_timestamp else
                    'unchanged' if timestamp == previous_timestamp else 'regressed')
        if progress != 'advanced':
            issues.append('source_timestamp_' + progress)
    rows = payload.get('states')
    if not isinstance(rows, (list, tuple)):
        rows = []
        issues.append('invalid_states')
    for name in HEAD_NAMES:
        matches = [r for r in rows if isinstance(r, Mapping) and r.get('name') == name]
        if len(matches) != 1:
            issues.append(f'{name}:expected_one_row_got_{len(matches)}')
        raw = matches[0] if len(matches) == 1 else {}
        head = {'name': name}
        for field in FIELDS:
            value = raw.get(field)
            valid = (type(value) is int and value >= 0) if field in ('mode', 'error_code') else (
                type(value) in (int, float) and math.isfinite(value))
            head[field] = value if valid else None
            if not valid:
                issues.append(f'{name}:invalid_{field}')
        if head['error_code'] not in (None, 0):
            issues.append(f'{name}:motor_error_{head["error_code"]}')
        head['motor_position_deg'] = (math.degrees(head['motor_position'])
                                     if head['motor_position'] is not None else None)
        joint = schema.get(name)
        head['joint_limits_rad'] = [joint.position_min, joint.position_max]
        if (head['motor_position'] is not None and
                not joint.position_min <= head['motor_position'] <= joint.position_max):
            issues.append(f'{name}:motor_position_outside_joint_limits')
        # Optional comparison fields; never substitute them for motor_position.
        for field in ('position', 'velocity'):
            value = raw.get(field)
            head[field] = (value if type(value) in (int, float) and math.isfinite(value)
                           else None)
        head['position_minus_motor_rad'] = (
            head['position'] - head['motor_position']
            if head['position'] is not None and head['motor_position'] is not None else None)
        heads.append(head)
    raw_heads = [r for r in rows if isinstance(r, Mapping) and r.get('name') in HEAD_NAMES]
    return dict(source_timestamp_ns=timestamp, source_progress=progress,
                raw_source_timestamp_repr=repr(payload.get('timestamp')),
                raw_head_rows_repr=repr(raw_heads),
                raw_joint_rows_repr=repr(rows),
                malformed_payload_repr=raw_payload_repr if not raw_heads else None,
                heads=heads, issues=issues)


def emit(output, event, **fields):
    print('G2_HEAD_DIAGNOSTIC ' + json.dumps(dict(event=event, **fields,
          physical_robot_command_sent=False), ensure_ascii=False, default=str),
          file=output, flush=True)


def wait_interruptibly(seconds, *, clock, sleep, stop_requested):
    end = clock() + max(0, seconds)
    while not stop_requested():
        remaining = end - clock()
        if remaining <= 0:
            return
        sleep(min(0.05, remaining))


def run_diagnostics(gdk, *, duration=10.0, hz=2.0, settle_seconds=2.0,
                    clock=time.monotonic, sleep=time.sleep, output=sys.stdout,
                    stop_requested=lambda: False):
    initialized, code, samples, warnings = False, 2, 0, 0
    try:
        for name, value, low, high in (('duration', duration, 0.1, 60),
                                      ('hz', hz, 0.1, 10),
                                      ('settle_seconds', settle_seconds, 0, 10)):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{name} must be finite within [{low}, {high}]')
        if stop_requested():
            return 130
        if gdk.gdk_init() != gdk.GDKRes.kSuccess:
            raise RuntimeError('GDK initialization failed')
        initialized = True
        robot = gdk.Robot()
        wait_interruptibly(settle_seconds, clock=clock, sleep=sleep,
                           stop_requested=stop_requested)
        # Optional status is raw evidence, not proof that control is enabled/safe.
        for method in ('get_whole_body_status', 'get_motion_control_status'):
            if stop_requested():
                return 130
            start = clock()
            try:
                value = getattr(robot, method)()
                emit(output, 'status', method=method, raw=value,
                     read_ms=(clock() - start) * 1000)
            except Exception as error:
                emit(output, 'status', method=method, unavailable=str(error))
        deadline, previous = clock() + duration, None
        while clock() < deadline and not stop_requested():
            start = clock()
            payload = robot.get_joint_states()
            end = clock()
            report = inspect_sample(payload, previous)
            if report['source_timestamp_ns'] is not None:
                previous = report['source_timestamp_ns']
            samples += 1
            warnings += bool(report['issues'])
            emit(output, 'sample', sample=samples, read_ms=(end - start) * 1000,
                 host_received_monotonic_ns=int(end * 1e9), **report)
            wait_interruptibly(min(1 / hz - (clock() - start), deadline - clock()),
                               clock=clock, sleep=sleep, stop_requested=stop_requested)
        code = 130 if stop_requested() else (0 if samples and not warnings else 2)
    except Exception as error:
        emit(output, 'error', reason=str(error))
    finally:
        if initialized:
            try:
                if gdk.gdk_release() != gdk.GDKRes.kSuccess:
                    raise RuntimeError('GDK release failed')
            except Exception as error:
                emit(output, 'release_error', reason=str(error))
                code = 2
        emit(output, 'summary', samples=samples, samples_with_issues=warnings,
             physical_direction_verified=False)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=10)
    parser.add_argument('--hz', type=float, default=2)
    parser.add_argument('--settle-seconds', type=float, default=2)
    args = parser.parse_args()
    stopped = False

    def stop(_sig, _frame):
        nonlocal stopped
        stopped = True

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return run_diagnostics(importlib.import_module('agibot_gdk'),
                               duration=args.duration, hz=args.hz,
                               settle_seconds=args.settle_seconds,
                               stop_requested=lambda: stopped)
    except Exception as error:
        emit(sys.stdout, 'error', reason=str(error))
        return 2
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
