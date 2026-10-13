"""Preview a yaw-only return to zero; motion requires explicit authorization."""

import argparse
import fcntl
import importlib
import math
import os
import signal
import sys
import time

from apps.check_g2_head_direction import execute_bounded_axis
from teleop.robots.g2 import G2Adapter, G2GDKJointCommandSender
from teleop.robots.g2_head import G2_HEAD_JOINTS, G2_HEAD_YAW
from teleop.robots.g2_head_servo import G2HeadFeedbackReader


TARGET_RAD = 0.0
MAX_RECOVERY_OFFSET_RAD = math.radians(15.0)
STABILITY_SAMPLES = 3
STABILITY_PERIOD_S = 0.1
STABILITY_TOLERANCE_RAD = 0.001
MOTION_WINDOW_S = 90.0
SPEED_RAD_S = 0.05


def select_center_target(current, schema):
    if (isinstance(current, bool) or not isinstance(current, (int, float))
            or not math.isfinite(current)):
        raise ValueError('yaw feedback must be finite')
    joint = schema.get(G2_HEAD_YAW)
    if not joint.position_min <= current <= joint.position_max:
        raise ValueError('current yaw is outside the configured joint limits')
    if abs(current) > MAX_RECOVERY_OFFSET_RAD + 1e-12:
        raise ValueError('current yaw is outside the 15 degree recovery envelope')
    if not joint.position_min <= TARGET_RAD <= joint.position_max:
        raise ValueError('zero yaw is outside the configured joint limits')
    return TARGET_RAD


def _stable_anchor(adapter, feedback_reader, *, clock_ns, sleep, stop_requested, output):
    anchor = None
    previous_source_timestamp = None
    schema = adapter.get_joint_schema()
    for sample in range(1, STABILITY_SAMPLES + 1):
        if stop_requested():
            return None
        now = clock_ns()
        state = adapter.read_state(now_ns=now)
        finished = clock_ns()
        if not 0 <= finished - state.timestamp_ns <= 200_000_000:
            raise ValueError('head feedback read exceeded freshness window')
        source_timestamp = feedback_reader.timestamp
        if (previous_source_timestamp is not None
                and source_timestamp <= previous_source_timestamp):
            raise ValueError('GDK source timestamp did not advance between stability samples')
        for name in G2_HEAD_JOINTS:
            position = state.positions[name]
            joint = schema.get(name)
            if (not math.isfinite(position)
                    or not joint.position_min <= position <= joint.position_max):
                raise ValueError(f'head feedback outside joint limits: {name}')
        if anchor is not None:
            for name in G2_HEAD_JOINTS:
                if abs(state.positions[name] - anchor.positions[name]) > STABILITY_TOLERANCE_RAD:
                    raise ValueError(f'head feedback is not stable: {name}')
        anchor = state
        previous_source_timestamp = source_timestamp
        print(
            f'G2_HEAD_CENTER_STABILITY sample={sample}/{STABILITY_SAMPLES} '
            f'yaw_deg={math.degrees(state.positions[G2_HEAD_YAW]):+.3f} '
            'head_errors_checked=True physical_robot_command_sent=False',
            file=output,
            flush=True,
        )
        if sample < STABILITY_SAMPLES:
            sleep(STABILITY_PERIOD_S)
    return anchor


def run_center(*, gdk, enable_motion=False, settle_seconds=2.0,
               clock_ns=time.monotonic_ns, sleep=time.sleep,
               stop_requested=lambda: False, output=sys.stdout):
    initialized = False
    adapter = None
    code = 2
    counts = {'attempts': 0, 'accepted': 0}
    try:
        if not isinstance(enable_motion, bool):
            raise ValueError('enable_motion must be bool')
        if (isinstance(settle_seconds, bool) or not isinstance(settle_seconds, (int, float))
                or not math.isfinite(settle_seconds) or not 0 <= settle_seconds <= 10):
            raise ValueError('settle-seconds must be within [0, 10]')
        if stop_requested():
            return 130
        if gdk.gdk_init() != gdk.GDKRes.kSuccess:
            raise RuntimeError('GDK initialization failed')
        initialized = True
        robot = gdk.Robot()
        sleep(settle_seconds)
        if stop_requested():
            return 130
        reader = G2HeadFeedbackReader(robot, clock_ns=clock_ns)
        sender = G2GDKJointCommandSender(gdk=gdk, robot=robot) if enable_motion else None
        adapter = G2Adapter(
            dry_run=False,
            state_reader=reader,
            command_sender=sender,
            allow_live_commands=enable_motion,
            default_velocity_rad_s=SPEED_RAD_S,
        )
        anchor = _stable_anchor(
            adapter, reader, clock_ns=clock_ns, sleep=sleep,
            stop_requested=stop_requested, output=output,
        )
        if anchor is None:
            return 130
        current = anchor.positions[G2_HEAD_YAW]
        target = select_center_target(current, adapter.get_joint_schema())
        print(
            f'G2_HEAD_CENTER_PLAN mode={"live" if enable_motion else "read-and-preview"} '
            f'joint={G2_HEAD_YAW} current_deg={math.degrees(current):+.3f} '
            f'target_deg={math.degrees(target):+.3f} '
            f'delta_deg={math.degrees(target-current):+.3f} '
            f'max_recovery_offset_deg=15.000 velocity_rad_s={SPEED_RAD_S} '
            'pitch_commanded=False roll_commanded=False automatic_start=False',
            file=output,
            flush=True,
        )
        if enable_motion:
            code = execute_bounded_axis(
                adapter, anchor, G2_HEAD_YAW, target,
                clock_ns=clock_ns, sleep=sleep,
                stop_requested=stop_requested, output=output, counts=counts,
                time_limit_s=MOTION_WINDOW_S,
            )
        else:
            code = 0
    except Exception as error:
        print(f'G2_HEAD_CENTER_ERROR reason={error} action=no_further_commands',
              file=output, flush=True)
        code = 2
    finally:
        if adapter is not None:
            adapter.stop()
        if initialized:
            try:
                if gdk.gdk_release() != gdk.GDKRes.kSuccess:
                    raise RuntimeError('GDK release returned a failure')
            except Exception as error:
                print(f'G2_HEAD_CENTER_RELEASE_ERROR reason={error}', file=output, flush=True)
                code = 2
        print(
            f'G2_HEAD_CENTER_SUMMARY attempts={counts["attempts"]} '
            f'accepted={counts["accepted"]} '
            f'physical_robot_command_attempted={counts["attempts"] > 0}',
            file=output,
            flush=True,
        )
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-motion', action='store_true')
    parser.add_argument('--settle-seconds', type=float, default=2.0)
    args = parser.parse_args()
    stopped = False

    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with open(os.environ.get('SESSION_LOCK_FILE', '/tmp/galaxis-pico-g2-teleop.lock'), 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('G2_HEAD_CENTER_REFUSED: another session is already running', file=sys.stderr)
                return 2
            return run_center(
                gdk=importlib.import_module('agibot_gdk'),
                enable_motion=args.enable_motion,
                settle_seconds=args.settle_seconds,
                stop_requested=lambda: stopped,
            )
    except Exception as error:
        print(f'G2_HEAD_CENTER_ERROR: {error}', file=sys.stderr)
        return 2
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
