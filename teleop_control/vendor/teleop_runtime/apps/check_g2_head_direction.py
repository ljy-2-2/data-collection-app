"""Bounded G2 head-axis direction check; preview unless --enable-motion is given."""

import argparse
import fcntl
import importlib
import math
import os
import signal
import sys
import time

from teleop.backends.direct_joint import DirectJointBackend
from teleop.contracts.intent import JointPositionIntent
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.robots.g2 import G2Adapter, G2GDKJointCommandSender
from teleop.robots.g2_head_servo import (
    G2_HEAD_OPERATIONAL_YAW_MAX_RAD,
    G2_HEAD_OPERATIONAL_YAW_MIN_RAD,
)


AXES = {'yaw': 'idx11_head_joint1', 'pitch': 'idx13_head_joint3'}
SPEED = 0.05
STEP = 0.004
PERIOD = 0.1
TIMEOUT = 45.0
NO_PROGRESS_TIMEOUT = 4.0
PROGRESS_RAD = 0.0005
MAX_DELTA_DEG = 5.0
TOLERANCE = 0.001


def _validate_selection(axis, delta_deg):
    if axis not in AXES:
        raise ValueError('axis must be yaw or pitch; roll is not controlled')
    if (isinstance(delta_deg, bool) or not isinstance(delta_deg, (int, float))
            or not math.isfinite(delta_deg) or not 0 < abs(delta_deg) <= MAX_DELTA_DEG):
        raise ValueError('delta must be finite, nonzero, and at most 5 degrees')


def select_target(axis, delta_deg, current, schema):
    _validate_selection(axis, delta_deg)
    if not math.isfinite(current):
        raise ValueError('head feedback must be finite')
    name = AXES[axis]
    joint = schema.get(name)
    target = current + math.radians(delta_deg)
    if not joint.position_min <= current <= joint.position_max:
        raise ValueError('initial head angle is outside the configured joint limits')
    if not joint.position_min <= target <= joint.position_max:
        raise ValueError('requested direction check exceeds joint limits; no motion sent')
    if axis == 'yaw' and not (
            G2_HEAD_OPERATIONAL_YAW_MIN_RAD <= current
            <= G2_HEAD_OPERATIONAL_YAW_MAX_RAD
            and G2_HEAD_OPERATIONAL_YAW_MIN_RAD <= target
            <= G2_HEAD_OPERATIONAL_YAW_MAX_RAD):
        raise ValueError(
            'requested direction check exceeds operational yaw envelope '
            '[-8, +45] degrees; no motion sent')
    return name, target


def execute_bounded_axis(adapter, anchor, name, target, *, clock_ns, sleep,
                         stop_requested, output, counts, time_limit_s=TIMEOUT):
    router = ControlRouter(DirectJointBackend())
    safety = SafetyGateway()
    schema = adapter.get_joint_schema()
    start = clock_ns()
    if (isinstance(time_limit_s, bool) or not isinstance(time_limit_s, (int, float))
            or not math.isfinite(time_limit_s) or time_limit_s <= 0):
        raise ValueError('time_limit_s must be finite and positive')
    deadline = start + int(time_limit_s * 1e9)
    previous = anchor.positions[name]
    low, high = sorted((previous, target))
    best_error, last_progress = abs(target - previous), start
    while clock_ns() < deadline:
        if stop_requested():
            return 130
        read_start = clock_ns()
        state = adapter.read_state(now_ns=read_start)
        now = clock_ns()
        if stop_requested():
            return 130
        if now >= deadline:
            break
        if not 0 <= now - state.timestamp_ns <= 200_000_000:
            raise ValueError('head feedback read exceeded freshness window')
        current = state.positions[name]
        # A concurrent external move or an unexpected feedback jump stops this check.
        if not low - TOLERANCE <= current <= high + TOLERANCE:
            raise ValueError('head feedback left the bounded calibration interval')
        if abs(current - previous) > 0.01:
            raise ValueError('head feedback jumped by more than 0.01 rad')
        for other in ('idx11_head_joint1', 'idx12_head_joint2', 'idx13_head_joint3'):
            if other != name and abs(state.positions[other] - anchor.positions[other]) > 0.005:
                raise ValueError(f'unselected head joint moved: {other}')
        print(f'G2_HEAD_DIRECTION_FEEDBACK joint={name} measured_rad={current:+.6f} '
              f'goal_rad={target:+.6f} remaining_deg={math.degrees(target-current):+.3f} '
              f'read_ms={(now-read_start)/1e6:.3f}', file=output, flush=True)
        if abs(target - current) <= TOLERANCE:
            print('G2_HEAD_DIRECTION_REACHED reached=True '
                  'camera_direction_verified=False observer_confirmation_required=True',
                  file=output, flush=True)
            return 0
        error = abs(target - current)
        if best_error - error >= PROGRESS_RAD:
            best_error, last_progress = error, now
        elif now - last_progress >= int(NO_PROGRESS_TIMEOUT * 1e9):
            raise TimeoutError('head feedback made no measurable progress for 4 seconds')
        next_target = current + max(-STEP, min(STEP, target - current))
        command = router.route(JointPositionIntent({name: next_target},
                                                   source='g2_head_direction_check'),
                               state, now_ns=now)
        validated = safety.validate(command, state, schema, now_ns=clock_ns())
        if stop_requested():
            return 130
        if clock_ns() >= deadline:
            break
        counts['attempts'] += 1
        print(f'G2_HEAD_DIRECTION_SEND_BEGIN attempt={counts["attempts"]} '
              f'joint={name} target_rad={next_target:+.6f} velocity_rad_s={SPEED}',
              file=output, flush=True)
        call_start = clock_ns()
        # Logging or scheduling must not allow a new call after the total window.
        if call_start >= deadline or stop_requested():
            counts['attempts'] -= 1
            if stop_requested():
                return 130
            break
        try:
            adapter.send_joint_command(validated, now_ns=call_start)
        except Exception:
            print(f'G2_HEAD_DIRECTION_SEND_ERROR attempt={counts["attempts"]} '
                  f'call_ms={(clock_ns()-call_start)/1e6:.3f}', file=output, flush=True)
            raise
        counts['accepted'] += 1
        print(f'G2_HEAD_DIRECTION_SEND_RETURN attempt={counts["attempts"]} '
              f'call_ms={(clock_ns()-call_start)/1e6:.3f} sdk_returned=True '
              'physical_direction_verified=False', file=output, flush=True)
        previous = current
        if stop_requested():
            return 130
        # Wait AFTER the vendor call, so attempts can never exceed 10 Hz.
        sleep(PERIOD)
    raise TimeoutError('head did not reach the requested angle within the motion window')


def run_check(*, gdk, axis, delta_deg, enable_motion=False, settle_seconds=2.0,
              clock_ns=time.monotonic_ns, sleep=time.sleep,
              stop_requested=lambda: False, output=sys.stdout):
    initialized = False
    adapter = None
    code = 2
    counts = {'attempts': 0, 'accepted': 0}
    try:
        # Reject invalid requests before initializing the vendor runtime.
        _validate_selection(axis, delta_deg)
        if not math.isfinite(settle_seconds) or not 0 <= settle_seconds <= 10:
            raise ValueError('settle-seconds must be within [0, 10]')
        if not isinstance(enable_motion, bool):
            raise ValueError('enable_motion must be bool')
        if stop_requested():
            return 130
        if gdk.gdk_init() != gdk.GDKRes.kSuccess:
            raise RuntimeError('GDK initialization failed')
        initialized = True
        robot = gdk.Robot()
        sleep(settle_seconds)
        if stop_requested():
            return 130
        sender = G2GDKJointCommandSender(gdk=gdk, robot=robot) if enable_motion else None
        adapter = G2Adapter(dry_run=False, state_reader=robot, command_sender=sender,
                            allow_live_commands=enable_motion, default_velocity_rad_s=SPEED)
        anchor = adapter.read_state(now_ns=clock_ns())
        name, target = select_target(axis, delta_deg, anchor.positions[AXES[axis]],
                                     adapter.get_joint_schema())
        print(f'G2_HEAD_DIRECTION_PLAN mode={"live" if enable_motion else "read-and-preview"} '
              f'joint={name} anchor_rad={anchor.positions[name]:+.6f} '
              f'target_rad={target:+.6f} delta_deg={delta_deg:+.2f} '
              f'velocity_rad_s={SPEED} max_step_rad={STEP} '
              f'motion_window_s={TIMEOUT} no_progress_timeout_s={NO_PROGRESS_TIMEOUT} '
              'roll_commanded=False automatic_return=False', file=output, flush=True)
        if not 0 <= clock_ns() - anchor.timestamp_ns <= 200_000_000:
            raise ValueError('initial head feedback read exceeded freshness window')
        if enable_motion:
            code = execute_bounded_axis(
                adapter, anchor, name, target, clock_ns=clock_ns, sleep=sleep,
                stop_requested=stop_requested, output=output, counts=counts,
            )
        else:
            code = 0
    except Exception as error:
        print(f'G2_HEAD_DIRECTION_ERROR reason={error} action=no_further_commands',
              file=output, flush=True)
        code = 2
    finally:
        if adapter is not None:
            # Clears local state only: this is not a physical emergency stop.
            adapter.stop()
        if initialized:
            try:
                if gdk.gdk_release() != gdk.GDKRes.kSuccess:
                    raise RuntimeError('GDK release returned a failure')
            except Exception as error:
                print(f'G2_HEAD_DIRECTION_RELEASE_ERROR reason={error}', file=output, flush=True)
                code = 2
        print(f'G2_HEAD_DIRECTION_SUMMARY attempts={counts["attempts"]} '
              f'accepted={counts["accepted"]} '
              f'physical_robot_command_attempted={counts["attempts"] > 0} '
              'direction_calibrated=False', file=output, flush=True)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axis', choices=AXES, required=True)
    parser.add_argument('--delta-deg', type=float,
                        help='joint delta, max +/-5 degrees; yaw default +2, pitch default -2')
    parser.add_argument('--enable-motion', action='store_true')
    parser.add_argument('--settle-seconds', type=float, default=2.0)
    args = parser.parse_args()
    delta = args.delta_deg if args.delta_deg is not None else (2.0 if args.axis == 'yaw' else -2.0)
    if not math.isfinite(delta) or not 0 < abs(delta) <= MAX_DELTA_DEG:
        parser.error('delta-deg must be nonzero and within [-5, 5]')
    stopped = False

    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        # open() descriptors are non-inheritable: GDK background execs cannot keep this lock.
        with open(os.environ.get('SESSION_LOCK_FILE', '/tmp/galaxis-pico-g2-teleop.lock'), 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('G2_HEAD_DIRECTION_REFUSED: another session is already running', file=sys.stderr)
                return 2
            gdk = importlib.import_module('agibot_gdk')
            return run_check(gdk=gdk, axis=args.axis, delta_deg=delta,
                             enable_motion=args.enable_motion, settle_seconds=args.settle_seconds,
                             stop_requested=lambda: stopped)
    except Exception as error:
        print(f'G2_HEAD_DIRECTION_ERROR: {error}', file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
