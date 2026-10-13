"""Independent head servo worker; read-and-validate unless explicitly armed."""
import argparse
import importlib
import json
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

from apps.pico_g2_head_preview import _LatestMailbox, emit_session, _positive_float, _nonnegative_float, _file_descriptor
from teleop.xr import XRoboToolkitInputAdapter
from teleop.xr.wire import decode_xr_frame_message, message_age_ns
from teleop.robots.g2 import G2Adapter
from teleop.robots.g2_head import G2HeadJointMapper, G2_HEAD_ROLL
from teleop.robots.g2_head_servo import G2HeadFeedbackReader, G2HeadServoSender, G2NormalHeadSender, normal_head_limits
from teleop.control.head_follow_pipeline import HeadFollowSession
from teleop.recording.head_journal import AsyncHeadJournal


def _head_message_fresh(message, now_ns, last_input_ns=None):
    if message is None:
        return False
    try:
        message_age_ns(message, now_ns)
    except ValueError:
        return False
    frame = message.frame
    if frame.stale or not 0 <= now_ns-frame.received_monotonic_ns <= 300_000_000:
        return False
    return last_input_ns is None or 0 <= now_ns-last_input_ns <= 300_000_000


def run_worker(gdk, input_stream, *, enable_motion=False, settle_seconds=2,
               stop_requested=lambda: False, output=sys.stdout, normal_mode=False,
               audit=lambda *args, **kwargs: None, journal_health=lambda: None):
    mailbox = _LatestMailbox()
    invalid = threading.Event()
    input_closed = threading.Event()
    message = None
    reader, session = None, None
    input_error = []
    initialized, adapter, count, result = False, None, 0, 0
    def read():
        try:
            for line in input_stream:
                if line.strip():
                    mailbox.publish(decode_xr_frame_message(line.strip()))
        except Exception as error:
            input_error.append(str(error))
            invalid.set()
            mailbox.invalidate()
        finally:
            input_closed.set()
            mailbox.close()
    threading.Thread(target=read, daemon=True, name='head-follow-input').start()
    try:
        journal_health()
        audit('precheck', motion_enabled=enable_motion, normal_mode=normal_mode,
              target_rate_rad_s=.3 if normal_mode else .05)
        if stop_requested(): raise InterruptedError('stopped before GDK initialization')
        if gdk.gdk_init() != gdk.GDKRes.kSuccess:
            raise RuntimeError('GDK initialization failed')
        initialized = True
        robot = gdk.Robot()
        time.sleep(settle_seconds)
        reader = G2HeadFeedbackReader(robot)
        def input_unusable():
            if stop_requested() or invalid.is_set() or input_closed.is_set() or message is None:
                return True
            return not _head_message_fresh(message, time.monotonic_ns(),
                                           session.last_input_ns if session else None)
        def guard():
            journal_health()
            if input_unusable(): raise ValueError('input/stop changed before head SDK send')
        rate, limits = (.3 if normal_mode else .05), None
        if normal_mode:
            # Stable real robot anchor before enabling any sender; no PICO prerequisite.
            probe = G2Adapter(dry_run=False, state_reader=reader)
            samples, timestamps = [], []
            for _ in range(3):
                if stop_requested(): raise InterruptedError('stopped during startup')
                sample = probe.read_state(now_ns=time.monotonic_ns())
                audit('startup_feedback', raw=reader.diagnostics())
                journal_health()
                samples.append(sample)
                timestamps.append(reader.timestamp)
                time.sleep(.03)
            if not timestamps[0] < timestamps[1] < timestamps[2]:
                raise ValueError('initial head source timestamps must advance')
            limits = normal_head_limits(samples[-1])
            for name in limits:
                values = [s.positions[name] for s in samples]
                if max(values)-min(values) > .001: raise ValueError('initial head feedback unstable')
            audit('fixed_envelope', limits_rad=dict(limits), rate_rad_s=rate)
            sender = G2NormalHeadSender(robot, reader=reader, limits=limits,
                      roll_hold=limits[G2_HEAD_ROLL][0], guard=guard, audit=audit) if enable_motion else None
        else:
            sender = G2HeadServoSender(robot) if enable_motion else None
        adapter = G2Adapter(dry_run=False, state_reader=reader, command_sender=sender,
                            allow_live_commands=enable_motion, default_velocity_rad_s=min(rate, .1))
        def candidate(command, state, frame):
            audit('candidate', xr_sequence=frame.sequence_id, device_timestamp_ns=frame.device_timestamp_ns,
                  head_quaternion=list(frame.head_pose.orientation_xyzw),
                  mapping=session.last_mapping,
                  targets_rad=dict(command.targets), measured_rad={n: state.positions[n] for n in command.targets},
                  feedback=reader.diagnostics(), motion_enabled=enable_motion)
            journal_health()
        def anchored(state):
            audit('anchor', head_quaternion=session.anchor_head_quaternion,
                  positions_rad={n: state.positions[n] for n in (limits or session.previous)})
            journal_health()
            if sender: sender.reanchor(state)
        session = HeadFollowSession(adapter, G2HeadJointMapper(adapter.get_joint_schema()),
                 roll_joint=G2_HEAD_ROLL, send_enabled=enable_motion,
                 on_anchor=anchored,
                 stop_requested=input_unusable, target_rate_rad_s=rate, target_limits=limits,
                 before_send=candidate)
        print(f'PICO_G2_HEAD_FOLLOW_READY motion_enabled={enable_motion} '
              f'target_rate_rad_s={rate} control_hz=100 low_latency=False '
              'yaw_operational_limits_deg=-8,45 roll=hold_initial '
              'first_fresh_frame_sets_anchor=True', file=output, flush=True)
        next_tick, next_log, last_status = time.monotonic(), 0, None
        while not stop_requested():
            journal_health()
            if invalid.is_set():
                raise ValueError(f'invalid/replayed input; stop without retry: {input_error}')
            message, closed = mailbox.wait_for_update(-1, timeout_s=0)
            if closed:
                break  # Do not execute queued frames after emitter exit.
            command = None
            if message is None:
                session.reset()
            else:
                try:
                    message_age_ns(message, time.monotonic_ns())
                except ValueError:
                    session.reset()
                    session.status = 'paused_transport_stale'
                else:
                    command = session.step(message.frame)
            if command is not None:
                count += 1
            now = time.monotonic()
            if session.status != last_status:
                audit('status', status=session.status, validated=count)
            if now >= next_log or (session.status != last_status and session.status == 'anchored_no_motion'):
                print(f'PICO_G2_HEAD_FOLLOW status={session.status} validated={count} '
                      f'targets={dict(command.targets) if command else {}} '
                      f'physical_robot_command_sent={bool(command and enable_motion)}', file=output, flush=True)
                print('PICO_G2_HEAD_FEEDBACK ' + json.dumps({
                    'xr_sequence': message.frame.sequence_id if message else None,
                    'measured_rad': session.last_feedback,
                    'last_targets_rad': session.previous,
                    'target_kind': 'last_sent' if enable_motion else 'preview',
                    'feedback': reader.diagnostics(),
                }, ensure_ascii=False, default=str), file=output, flush=True)
                next_log = now + 1
            last_status = session.status
            # Never catch up by bursting obsolete ticks after a slow call.
            next_tick = max(next_tick + 0.01, now)
            time.sleep(max(0, next_tick - time.monotonic()))
        result = 130 if stop_requested() else (0 if count else 1)
    except Exception as error:
        try: audit('error', reason=str(error), feedback=reader.diagnostics() if reader else {})
        except Exception: pass
        print(f'PICO_G2_HEAD_FOLLOW_ERROR reason={error} action=no_further_commands', file=output, flush=True)
        print('PICO_G2_HEAD_FAILURE_CONTEXT ' + json.dumps({
            'xr_sequence': message.frame.sequence_id if message else None,
            'last_targets_rad': session.previous if session else None,
            'feedback': reader.diagnostics() if reader else None,
        }, ensure_ascii=False, default=str), file=output, flush=True)
        result = 130 if isinstance(error, InterruptedError) else 2
    finally:
        if adapter is not None:
            adapter.stop()  # Local state only, not hardware emergency stop.
        if initialized:
            try:
                if gdk.gdk_release() != gdk.GDKRes.kSuccess:
                    raise RuntimeError('GDK release failed')
            except Exception as error:
                print(f'PICO_G2_HEAD_FOLLOW_RELEASE_ERROR reason={error}', file=output, flush=True)
                result = 2
        print(f'PICO_G2_HEAD_FOLLOW_SUMMARY validated={count} motion_enabled={enable_motion} '
              'physical_continuity_verified=False', file=output, flush=True)
        try: audit('summary', validated=count, code=result, motion_enabled=enable_motion,
                   physical_continuity_verified=False)
        except Exception: result = 2
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='role', required=True)
    emit = sub.add_parser('emit')
    emit.add_argument('--duration', type=_positive_float, default=60)
    emit.add_argument('--hz', type=float, choices=[100.0], default=100)
    emit.add_argument('--intent-fd', dest='frame_fd', type=_file_descriptor)
    worker = sub.add_parser('worker')
    worker.add_argument('--settle-seconds', type=_nonnegative_float, default=2)
    worker.add_argument('--enable-motion', action='store_true')
    worker.add_argument('--log-file', type=Path, help='new JSONL path; never overwrites a previous log')
    args = parser.parse_args()
    # Parent owns fd9 session lock. Neither XR nor GDK background children inherit it.
    try:
        os.close(9)
    except OSError:
        pass
    stopped = threading.Event()
    handlers = {s: signal.signal(s, lambda *_: stopped.set()) for s in (signal.SIGINT, signal.SIGTERM)}
    journal = None
    try:
        if args.role == 'emit':
            output = os.fdopen(args.frame_fd, 'w', buffering=1, closefd=False) if args.frame_fd else sys.stdout
            return emit_session(args, adapter=XRoboToolkitInputAdapter(), output=output,
                                diagnostics=sys.stderr, stop_requested=stopped.is_set)
        path = args.log_file
        if path is None:
            folder = Path(__file__).resolve().parents[1] / 'artifacts/head-follow'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.jsonl')
        journal = AsyncHeadJournal(path)
        journal.write('launch', argv=sys.argv[1:], motion_enabled=args.enable_motion)
        print(f'PICO_G2_HEAD_FOLLOW_LOG path={path.resolve()}', flush=True)
        code = run_worker(importlib.import_module('agibot_gdk'), sys.stdin,
                          enable_motion=args.enable_motion, settle_seconds=args.settle_seconds,
                          stop_requested=stopped.is_set, normal_mode=True,
                          audit=journal.write, journal_health=journal.check_health)
        journal.close()
        journal = None
        return code
    except Exception as error:
        print(f'HEAD_FOLLOW_ERROR: {error}', file=sys.stderr, flush=True)
        return 2
    finally:
        if journal:
            try: journal.close()
            except Exception as error: print(f'HEAD_FOLLOW_LOG_ERROR: {error}', file=sys.stderr)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
