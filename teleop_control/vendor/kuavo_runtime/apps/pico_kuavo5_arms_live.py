#!/usr/bin/env python3
"""PICO/Kuavo v63 dual-arm teleoperation. Default: local checks, no device I/O.

The execution profile is the manufacturer's official dual-arm IK followed by
EXTERN=2, quickMode=2, WBC arm interpolation and /kuavo_arm_traj at 100 Hz.
Either Grip controls its corresponding arm; both Grips control both arms.
The legacy ordinary-MPC option is refused before device I/O.
Read docs/kuavo5-arms-manual-entry.md.
"""

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
from queue import Queue, Empty, Full
import stat
import subprocess
import sys
import threading
import time
import uuid

from teleop.robots.kuavo5_live_bundle import build_bundle, build_execution_ssh_command
from teleop.robots.kuavo5_live_protocol import make_input_packet
# Keep the writer available here for callers that inject/patch the existing API.
from teleop.robots.kuavo5_host_channel import SSHSessionChannel, write_packet
from teleop.robots.kuavo5_host_journal import AsyncJournal, AsyncConsole
from teleop.robots.kuavo5_live_agent import PriorityPacketQueue


ROOT = Path(__file__).resolve().parents[1]
VENDOR_INITIALIZATION = (
    ('/wheel_arm_change_arm_ctrl_mode', {'control_mode': 2}),
    ('/enable_lb_arm_quick_mode', {'quickMode': 2}),
    ('/enable_arm_traj_interpolator', {'data': True}),
)


def operator_status_hint(packet):
    tracking = packet.get('tracking_governor') or {}
    joint = tracking.get('stalled_joint')
    if packet.get('status') == 'published' and joint \
            and str(packet.get('reason', '')).startswith('tracking limited without disconnect: '):
        side = '左臂' if str(joint).startswith('zarm_l') else '右臂'
        return (side+'跟随暂时受限（'+str(joint)+'），Grip 会话仍连接；'
                '向反方向收回手柄可恢复，不需要松开再握。')
    return None


def gripper_status_hint(report):
    if report is None:
        return None
    parts = []
    for side, label in (('left', '左'), ('right', '右')):
        state = report.get('motors', {}).get(side, {})
        trigger = state.get('trigger')
        value = '--' if trigger is None else str(round(trigger*100))+'%'
        text = label+' '+str(state.get('state', 'unknown'))+' 手柄扳机 '+value
        error = report.get('owner', {}).get('errors', {}).get(side)
        reason = error or state.get('reason')
        if reason and (error or state.get('state') in ('FAULT', 'WAIT_FEEDBACK', 'WAIT_RELEASE', 'WAIT_RECOVERY')):
            text += ' 原因：'+str(reason)
        parts.append(text)
    label = '夹爪输入预览（不发送 CAN）' if report.get('mode') == 'preview' else '夹爪控制'
    return label+' | '+' | '.join(parts)


def validate_return_home_ready(packet, bundle):
    """Require the explicitly requested return capability before XR delivery."""
    if bundle.get('return_home_config') is None:
        if packet.get('return_home_enabled', False) is not False or packet.get('return_home_config_sha256') is not None:
            raise ValueError('unexpected return-home capability in robot handshake')
        return
    if (packet.get('return_home_enabled') is not True
            or not bundle.get('return_home_config_sha256')
            or packet.get('return_home_config_sha256') != bundle['return_home_config_sha256']
            or packet.get('return_home_buttons') != {'request': 'left_y', 'cancel': 'right_b'}):
        raise ValueError('return-home configuration/button handshake mismatch')


def return_home_status_hint(report):
    if not isinstance(report, dict):
        return None
    text = '回初始姿态：'+str(report.get('state', 'unknown'))
    if report.get('reason'):
        text += ' | '+str(report['reason'])
    return text+' | 左 Y 请求；右 B 取消'


def run_host(command, bundle, *, duration, journal,
             xr_factory=None, process_factory=subprocess.Popen, channel_factory=None, session_control=None,
             service_contract=None):
    """Run the existing host policy with a channel from a zero-argument factory.

    The default channel keeps the SSH command/process factory API. Alternative
    channels can be explicitly selected at startup. SSH remains the default;
    direct sessions never invoke the SSH bootstrap or upload Python sources.
    """
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) \
            or not math.isfinite(duration) or duration < 0:
        raise ValueError('duration must be finite and nonnegative; 0 means until stopped')
    if service_contract is not None:
        from teleop.robots.kuavo5_service_contract import host_bundle
        if channel_factory is None or bundle != host_bundle(service_contract):
            raise ValueError('contract host requires its verified metadata and an explicit network channel')
    from teleop.robots.kuavo5_pc_session import SessionControl
    control = session_control or SessionControl()
    if control.stop_event.is_set():
        return 0
    standalone = bundle.get("standalone_return", False)
    completed, request_sent = False, False
    if type(standalone) is not bool or (standalone and bundle.get("return_home_config") is None):
        raise ValueError("invalid standalone return capability")
    if xr_factory is None and not standalone:
        from teleop.xr import XRoboToolkitInputAdapter
        xr_factory = lambda: XRoboToolkitInputAdapter(stale_after_s=.1)
    stop, lock = threading.Event(), threading.Lock()
    last_operator_hint = None
    candidate = bundle.get('target_recovery') == 'bounded_candidate'
    events = PriorityPacketQueue(max_control=64, max_diagnostic=128,
                                 **({'coalesce_challenges': True} if candidate else {}))
    async_journal = AsyncJournal(journal) if candidate else None
    console = AsyncConsole(sys.stdout)
    stderr_console = AsyncConsole(sys.stderr)
    latest, errors = [None], []
    xr = channel = None
    pico_thread = None
    closed = None
    return_code = 1
    def record(packet):
        if async_journal is not None:
            async_journal.record(packet)
        else:
            journal.write(json.dumps(dict(local_monotonic_ns=time.monotonic_ns(), packet=packet), allow_nan=False)+'\n')
            journal.flush()
    def poll_pico():
        try:
            while not stop.is_set():
                frame = xr.read_frame()
                if frame is not None:
                    with lock:
                        latest[0] = frame
                stop.wait(.005)
        except Exception as exc:
            errors.append('PICO: '+str(exc))
            stop.set()
    def read_remote():
        try:
            while True:
                packet = channel.receive()
                if packet is None:
                    break
                events.put_nowait(packet)
        except Exception as exc:
            errors.append('robot stream: '+str(exc))
        finally:
            try:
                events.put_nowait({'event': 'eof'})
            except Full:
                stop.set()
    def read_stderr():
        for line in channel.stderr_lines():
            stderr_console.write('Kuavo: '+line.rstrip()[:600])
    try:
        if not standalone:
            xr = xr_factory()
            xr.start()
            pico_thread = threading.Thread(target=poll_pico, name='pico-input', daemon=True)
            pico_thread.start()
        channel = (SSHSessionChannel(command, process_factory=process_factory,
                                     packet_writer=write_packet)
                   if channel_factory is None else channel_factory())
        if control.stop_event.is_set():
            channel = None  # construction is inert; no session was requested
            raise KeyboardInterrupt
        if service_contract is None:
            channel.open(bundle)
        else:
            channel.open_contract(service_contract)
        threading.Thread(target=read_remote, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()
        started = time.monotonic()
        # Candidate builds its private math process before any control profile.
        # Give this bounded preflight its own budget; the user's duration begins
        # at READY. Preserve the historical baseline's hard overall deadline.
        candidate = bundle.get('target_recovery') == 'bounded_candidate'
        session_deadline = started+(90. if candidate or duration == 0 else duration)
        ready, last_device = False, 0
        while not stop.is_set() and not control.stop_event.is_set():
            if async_journal is not None:
                async_journal.check()
            remaining = session_deadline-time.monotonic()
            if remaining <= 0:
                break
            try:
                packet = events.get(timeout=min(.05, remaining))
            except Empty:
                continue
            # events.get() may return after the hard session deadline.  Do
            # not answer a queued challenge merely because the preceding loop
            # condition was still inside the window.
            if time.monotonic() >= session_deadline:
                break
            record(packet)
            kind = packet.get('event')
            if kind == 'ready':
                if ready:
                    raise ValueError('duplicate robot execution handshake')
                if service_contract is not None:
                    from teleop.robots.kuavo5_service_contract import validate_ready
                    validate_ready(packet, service_contract)
                else:
                    from teleop.robots.kuavo5_live_agent import URDF_SHA
                    if ready or packet.get('robot_version') != '63' or packet.get('urdf_sha256') != URDF_SHA \
                            or packet.get('profile_configured') is not True:
                        raise ValueError('robot execution handshake mismatch')
                    expected_gripper_send = bundle.get('gripper_mode', 'off') == 'damiao'
                    if (packet.get('gripper_mode', 'off') != bundle.get('gripper_mode', 'off')
                            or packet.get('gripper_can_send_enabled', False) is not expected_gripper_send
                            or packet.get('gripper_config_sha256') != bundle.get('gripper_config_sha256')):
                        raise ValueError('gripper configuration handshake mismatch')
                    validate_return_home_ready(packet, bundle)
                    if packet.get("standalone_return", False) is not standalone:
                        raise ValueError("standalone return handshake mismatch")
                    from teleop.robots.kuavo5_posture import validate_posture_ready
                    validate_posture_ready(packet, bundle.get('arm_posture', 'baseline'),
                                           bundle.get('target_recovery', 'baseline'))
                    if candidate:
                        from teleop.robots.kuavo5_bounded_profile import validate_ready
                        validate_ready(packet, bundle.get('arm_posture', 'baseline'))
                        from teleop.robots.kuavo5_live_agent import motion_profile, tracking_soft_lead
                        expected_velocity, expected_acceleration = motion_profile(
                            'bounded_candidate', bundle.get('speed_profile', 'steady'))
                        if (packet.get('speed_profile') != bundle.get('speed_profile', 'steady') or
                                packet.get('max_velocity_rad_s') != expected_velocity or
                                packet.get('planning_acceleration_rad_s2') != expected_acceleration or
                                packet.get('tracking_soft_lead_rad') != tracking_soft_lead(
                                    'bounded_candidate', bundle.get('speed_profile', 'steady'))):
                            raise ValueError('candidate speed handshake mismatch')
                if candidate or duration == 0:
                    session_deadline = float("inf") if duration == 0 else time.monotonic()+duration
                if standalone:
                    session_deadline = time.monotonic()+bundle["return_home_config"]["timeout_s"]+5.
                ready = True
                control.ready()
                if standalone:
                    console.write('独立回位已就绪；无需 PICO。开始一次回位，到位后自动退出；Ctrl+C 取消。')
                else:
                    console.write('姿态方法：'+bundle.get('arm_posture', 'baseline'))
                    console.write('双臂会话已就绪：先松开两侧 Grip；按左/右 Grip 控对应手臂，同时按可控双臂；Ctrl+C 退出。')
                    if bundle.get('gripper_mode') == 'preview':
                        console.write('夹爪输入预览已加入同一会话：先松开左右食指扳机，再按下预览；当前不发送 CAN。')
                    elif bundle.get('gripper_mode') == 'damiao':
                        console.write('夹爪已加入同一遥操会话：先松开食指扳机，再按下接管；0=张开，1=闭合，与握把键独立。')
                    if bundle.get('return_home_config') is not None:
                        console.write('回初始姿态已启用：松开两侧 Grip 和扳机，再按左 Y 请求双臂回厂家初始姿态、夹爪回本次会话初始开度；右 B 取消。')
            elif kind == 'compute_preparing':
                if ready or not candidate:
                    raise ValueError('unexpected private compute preflight')
                console.write('正在准备独立官方计算进程；尚未配置控制模式。')
            elif kind == 'challenge':
                if not ready:
                    raise ValueError('challenge before robot identity handshake')
                if standalone:
                    if type(packet.get('challenge_id')) is not int:
                        raise ValueError('invalid standalone return challenge')
                    response = dict(event='return_command', challenge_id=packet['challenge_id'],
                                    action='heartbeat' if request_sent else 'request')
                else:
                    with lock:
                        frame = latest[0]
                    if frame is None or frame.device_timestamp_ns == last_device:
                        continue
                    if frame.stale:
                        raise ValueError('PICO tracking stale; session closing')
                    response = make_input_packet(packet, frame, now_ns=time.monotonic_ns())
                # Pose conversion and serialization also consume time.  A hard
                # session duration must bound the final write, not only the
                # event wait that preceded it.
                remaining = session_deadline-time.monotonic()
                if remaining <= 0 or control.stop_event.is_set():
                    break
                try:
                    channel.send_input(response, timeout_s=min(.1, remaining),
                                       absolute_deadline=min(session_deadline, time.monotonic()+.1))
                except TimeoutError:
                    if time.monotonic() >= session_deadline:
                        break
                    raise
                if standalone:
                    request_sent = True
                else:
                    last_device = frame.device_timestamp_ns
                record(response)
            elif kind == 'status':
                if bundle.get('return_home_config') is not None:
                    hint = ('独立回位：'+str(packet.get('return_home', {}).get('state', 'unknown'))+
                            ' | Ctrl+C 取消；到位后自动退出' if standalone else
                            return_home_status_hint(packet.get('return_home')))
                    if hint is not None:
                        console.write(hint)
                if not standalone and bundle.get('gripper_mode') in ('preview', 'damiao'):
                    hint = gripper_status_hint(packet.get('grippers'))
                    if hint is not None:
                        console.write(hint)
                # Full evidence is in the journal; avoid rendering thousands
                # of JSON characters in the input response thread every second.
                if candidate:
                    console.write('双臂 '+str(packet.get('status'))+' | '+str(packet.get('reason')))
                else:
                    console.write(json.dumps(packet, ensure_ascii=False))
                hint = operator_status_hint(packet)
                if hint is not None and hint != last_operator_hint:
                    console.write(hint)
                last_operator_hint = hint
            elif kind in ('guard_pause', 'guard_ready'):
                console.write('独立回位状态检查：'+str(packet.get('reason', kind)) if standalone else
                      ('底座运动检查触发：已暂停双臂，会话保持连接；等待底座稳定。'
                       if kind == 'guard_pause' else
                       '底座已稳定：松开两侧 Grip，再握住需要控制的手柄恢复。'))
            elif kind in ('return_started', 'return_refused', 'return_cancelled', 'return_completed', 'return_trace'):
                if not ready or bundle.get('return_home_config') is None:
                    raise ValueError('unexpected return-home event before capability handshake')
                # These are robot reports, persisted above, never host-generated
                # movement commands or a reason to replay a previous Y press.
                if standalone and kind == 'return_completed':
                    if not request_sent:
                        raise ValueError('return completion before request')
                    completed = True
                if standalone and kind in ('return_refused', 'return_cancelled'):
                    raise ValueError('independent return failed: '+str(packet.get('reason', '')))
                if kind != 'return_trace':
                    labels = {'return_started': '回位已开始', 'return_refused': '回位请求被拒绝',
                              'return_cancelled': '回位已取消', 'return_completed': '回位已完成'}
                    console.write(labels[kind]+'：'+str(packet.get('reason', '')))
            elif kind in ('ik_failure', 'guard_failure', 'ik_trace', 'control_trace',
                          'input_drop', 'guard_trace', 'reference_not_sent', 'gripper_trace'):
                # Already persisted by record(packet). Evidence is never a
                # command or permission to re-arm.
                pass
            elif kind == 'refused':
                raise ValueError('机器人端拒绝启动/继续：'+str(packet.get('reason')))
            elif kind == 'closed':
                closed = packet
                if candidate and not ready:
                    raise ValueError('candidate closed before READY; no control session established')
                return_code = packet.get('exit_code', 1)
                break
            elif kind == 'eof':
                raise ValueError('robot stream closed without a final report')
            else:
                raise ValueError('unexpected robot event')
        if closed is None and not errors:
            if candidate and not ready and not control.stop_event.is_set():
                raise ValueError('candidate preflight ended before READY; no control session established')
            return_code = 0
    except KeyboardInterrupt:
        control.request_stop()
        return_code = 130
        console.write('正在结束输入，等待机器人端保持请求回执。')
    except Exception as exc:
        errors.append(str(exc))
        # Surface refusal before SDK shutdown, which may block in native code.
        console.write(('独立回位已拒绝/中止：' if standalone else '遥操作已拒绝/中止：')+str(exc))
        return_code = 1
    finally:
        control.request_stop()
        stop.set()
        if channel is not None:
            # EOF asks our robot-side session to close. Never terminate the SSH
            # master, manufacturer controller, or a pending late-mode fallback.
            try:
                channel.close_input()
            except (OSError, ValueError):
                pass
            deadline = time.monotonic()+3.
            while closed is None and time.monotonic() < deadline:
                try:
                    packet = events.get(timeout=.05)
                except Empty:
                    if channel.poll() is not None:
                        break
                    continue
                try:
                    record(packet)
                except Exception as exc:
                    if 'journal cleanup: '+str(exc) not in errors:
                        errors.append('journal cleanup: '+str(exc))
                if packet.get('event') == 'closed':
                    closed = packet
                elif packet.get('event') == 'eof':
                    break
            if closed is None:
                errors.append('未收到退出确认；物理停止未验证，不能据此再次启动')
            else:
                from teleop.robots.kuavo5_live_agent import close_report_failed
                if closed.get('exit_code') != 0 or close_report_failed(closed.get('transport')):
                    errors.append('robot-side session/hold failed: '+str(closed.get('transport')))
                if bundle.get('gripper_mode') == 'damiao':
                    gripper_close = closed.get('gripper_transport')
                    if not isinstance(gripper_close, dict) or gripper_close.get('closed') is not True or gripper_close.get('errors'):
                        errors.append('gripper shutdown unconfirmed: '+str(gripper_close))
            try:
                channel.finish()
            except Exception as exc:
                errors.append('channel cleanup: '+str(exc))
        if pico_thread is not None:
            pico_thread.join(timeout=.5)
        if xr is not None and (pico_thread is None or not pico_thread.is_alive()):
            try:
                xr.close()
            except Exception as exc:
                errors.append('PICO close: '+str(exc))
        elif xr is not None:
            errors.append('PICO read thread blocked; SDK close deferred')
        if standalone and return_code == 0 and not completed:
            errors.append('独立回位未取得到位确认；不能报告成功')
        if errors:
            return_code = 1
        events.discard_diagnostics()
        summary = dict(event='host_closed', exit_code=return_code, errors=errors,
                       robot_report=closed,
                       console=console.close(), stderr_console=stderr_console.close(),
                       host_diagnostics_dropped=events.dropped_diagnostics_total,
                       physical_stop_verified=False)
        if standalone:
            summary.update(standalone_return=True, return_completed=completed,
                           pico_sdk_started=False)
        if candidate:
            summary['host_challenges_coalesced'] = events.coalesced_challenges_total
        try:
            record(dict(summary, errors=list(errors)))
        except Exception as exc:
            errors.append('journal final record: '+str(exc))
        finally:
            if async_journal is not None:
                try:
                    async_journal.close()
                except Exception as exc:
                    errors.append('journal close: '+str(exc))
        if errors:
            return_code = summary['exit_code'] = 1
        control.set_report(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return return_code


def main(argv=None, *, session_control=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--configure-mpc-profile', action='store_true',
        help='legacy option: execution refused; ordinary MPC is not the requested vendor profile')
    parser.add_argument('--configure-vendor-profile', action='store_true',
        help='select vendor EXTERN=2/quick2/interpolation')
    parser.add_argument('--ssh-control-path', default='/tmp/kuavo5-ssh-master')
    parser.add_argument('--session-transport', choices=('ssh', 'network'), default='ssh',
        help='startup-only choice; SSH remains the saved default')
    parser.add_argument('--network-host', help='explicit numeric IPv4 address; no device discovery')
    parser.add_argument('--network-port', type=int, default=7443)
    parser.add_argument('--network-namespace', choices=('current', 'kuavons'), default='current',
        help='kuavons borrows only a TCP socket; PICO remains on host; requires sudo -v')
    parser.add_argument('--network-token-file', type=Path,
        help='owner-only 64-hex authentication token file; not read in dry run')
    parser.add_argument('--network-cert-sha256',
        help='64-hex SHA256 pin of the service TLS certificate; required outside loopback')
    parser.add_argument('--duration', type=float, default=60., help='0: until stopped; positive: seconds')
    parser.add_argument('--target-recovery', choices=('baseline', 'fixed_anchor', 'bounded_candidate'), default='bounded_candidate',
        help='startup-only target policy; default: user-selected bounded_candidate')
    parser.add_argument('--speed-profile', choices=('steady','responsive','responsive_1p0'), default=None,
        help='default: responsive (0.7 rad/s,5 rad/s2) for bounded_candidate, steady otherwise; responsive_1p0=1.0 rad/s,10 rad/s2')
    parser.add_argument('--arm-posture', choices=('baseline', 'elbow_preference', 'elbow_preference_v2'), default=None,
        help='default: original elbow_preference for bounded_candidate, baseline otherwise; elbow weight 0.1; v2 is opt-in')
    parser.add_argument('--log-file', type=Path)
    parser.add_argument('--grippers', choices=('off', 'preview', 'damiao'), default='off',
        help='off / trigger preview / calibrated Damiao control through the shared CAN owner')
    parser.add_argument('--gripper-config', type=Path, help='calibrated configuration; required for damiao')
    parser.add_argument('--standalone-return', action='store_true',
        help='one return request without PICO; ends after completion and cleanup')
    parser.add_argument('--return-home-config', type=Path,
        help='opt in to left Y return / right B cancel with calibrated Damiao grippers; network requires a matching installed profile')
    parser.add_argument('--allow-gripper-control', action='store_true',
        help='explicitly authorize gripper enable, control and cleanup in this execution session')
    parser.add_argument('--accept-unverified-gripper-bus', action='store_true',
        help='accept supervised testing when shared-bus compatibility is explicitly unverified')
    args = parser.parse_args(argv)
    # User-selected 2026-10-10 baseline. Explicit historical target routes keep
    # compatible defaults; explicitly incompatible combinations still fail validation.
    if args.speed_profile is None:
        args.speed_profile = 'responsive' if args.target_recovery == 'bounded_candidate' else 'steady'
    if args.arm_posture is None:
        args.arm_posture = 'elbow_preference' if args.target_recovery == 'bounded_candidate' else 'baseline'
    if args.configure_mpc_profile and args.configure_vendor_profile:
        parser.error('choose one profile; no fallback between profiles')
    if args.execute != (args.configure_mpc_profile or args.configure_vendor_profile):
        parser.error('--execute and one profile option must be supplied together')
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error('--duration must be finite and nonnegative; 0 means until stopped')
    try:
        from teleop.control.kuavo5_gripper_teleop import validate_gripper_mode
        validate_gripper_mode(args.grippers)
        if args.grippers == 'damiao':
            if args.gripper_config is None:
                raise ValueError('damiao requires --gripper-config with this robot\'s measured endpoints')
            if args.execute and not args.allow_gripper_control:
                raise ValueError('real gripper execution requires --allow-gripper-control')
        elif args.gripper_config is not None or args.allow_gripper_control:
            raise ValueError('gripper configuration/permission requires --grippers damiao')
        if args.standalone_return and args.return_home_config is None:
            raise ValueError('--standalone-return requires --return-home-config')
        if args.return_home_config is not None and args.grippers != 'damiao':
            raise ValueError('--return-home-config requires --grippers damiao and --gripper-config')
        if args.configure_mpc_profile:
            raise ValueError('ordinary MPC is not an execution fallback; use --configure-vendor-profile')
        network_endpoint = None
        if args.session_transport == 'network':
            if args.standalone_return:
                raise ValueError('standalone return-home requires --session-transport ssh; network sessions accept PICO input only')
            from teleop.robots.kuavo5_network_config import validate_endpoint
            if args.network_host is None or args.network_token_file is None:
                raise ValueError('network transport requires --network-host and --network-token-file')
            network_endpoint = validate_endpoint(args.network_host, args.network_port,
                                                 args.network_cert_sha256)
            if ((args.target_recovery, args.speed_profile, args.arm_posture) != (
                    'bounded_candidate', 'responsive', 'elbow_preference')
                    or args.grippers not in ('off', 'damiao')):
                raise ValueError('network requires saved 0.7 / elbow_preference with off or calibrated damiao grippers')
        elif args.network_host is not None or args.network_token_file is not None or args.network_cert_sha256 is not None or args.network_namespace != 'current':
            raise ValueError('network options require --session-transport network')
        # Manual acceptance of the revised seed and reference-planning path.
        # The historical incident is not a claim of physical verification;
        # feedback, model, lease and publication guards remain mandatory.
        bundle = build_bundle(ROOT/'src', mode='execute' if args.execute else 'validate',
                              target_recovery=args.target_recovery, speed_profile=args.speed_profile,
                              gripper_mode=args.grippers, arm_posture=args.arm_posture,
                              gripper_config=args.gripper_config,
                              return_home_config=args.return_home_config,
                              **({"standalone_return": True} if args.standalone_return else {}))
        if args.execute and args.grippers == 'damiao':
            preparation = bundle['gripper_config'].get('preparation', {})
            if (preparation.get('shared_bus_compatibility_verified') is False
                    and not args.accept_unverified_gripper_bus):
                raise ValueError('该配置仅用于现场测试，共享总线兼容性未验证；需要显式 '
                                 '--accept-unverified-gripper-bus，不能视为厂家验收通过')
        if not args.execute:
            from teleop.robots.kuavo5_posture import posture_profile
            from teleop.robots.kuavo5_live_agent import (
                LIVE_MAX_VELOCITY_RAD_S, LIVE_PLANNING_ACCELERATION_RAD_S2,
                LIVE_TRACKING_SOFT_LEAD_RAD,
                LIVE_IK_FALLBACK_CONSTRAINT_MODE,
                target_progress_profile,
                LIVE_IK_PRIMARY_MAX_SEED_DELTA_RAD, LIVE_CANDIDATE_JOINT_MARGIN_RAD,
                LIVE_IK_FALLBACK_MAX_SEED_DELTA_RAD, motion_profile, tracking_soft_lead)
            velocity, acceleration = motion_profile(args.target_recovery, args.speed_profile)
            print(json.dumps(dict(event='KUAVO_ARMS_DRY_RUN', bundle_sha256=bundle['sha256'],
                modules=len(bundle['sources']), duration_s=args.duration,
                **({'standalone_return': True, 'automatic_exit_after_return': True,
                    'execution_lock_shared_with_teleop': True,
                    'return_max_velocity_rad_s': bundle['return_home_config']['max_velocity_rad_s'],
                    'return_timeout_s': bundle['return_home_config']['timeout_s']}
                   if args.standalone_return else {}),
                continuous_session=args.duration == 0 and not args.standalone_return,
                ssh_started=False, pico_sdk_started=False,
                session_transport=args.session_transport, network_connected=False,
                network_namespace=args.network_namespace, namespace_helper_started=False,
                authentication_token_read=False,
                network_tls_pinned=bool(args.network_cert_sha256),
                control_services_called=False, physical_robot_command_sent=False,
                gripper_mode=args.grippers, gripper_can_send_enabled=False,
                gripper_bridge_status='runtime_checks_pending' if args.grippers == 'damiao' else 'not_requested',
                gripper_config_sha256=bundle.get('gripper_config_sha256'),
                max_velocity_rad_s=velocity, speed_profile=args.speed_profile,
                planning_acceleration_rad_s2=acceleration,
                tracking_soft_lead_rad=tracking_soft_lead(args.target_recovery, args.speed_profile),
                tracking_stall_policy='limit_affected_arm_keep_grip_reverse_to_recover',
                candidate_limit_failure_policy='discard_and_retry_fresh_input',
                candidate_limit_retry_modes=[0, 3, 0],
                ik_primary_constraint_mode=0,
                ik_fallback_constraint_mode=LIVE_IK_FALLBACK_CONSTRAINT_MODE,
                ik_fallback_max_seed_delta_rad=LIVE_IK_FALLBACK_MAX_SEED_DELTA_RAD,
                ik_primary_max_seed_delta_rad=LIVE_IK_PRIMARY_MAX_SEED_DELTA_RAD,
                candidate_joint_margin_deg=math.degrees(LIVE_CANDIDATE_JOINT_MARGIN_RAD),
                ik_fallback_restores_mode0=True,
                acceleration_limit_scope='joint_reference_planning_only',
                motion_range='v63_urdf_joint_limits', input_ttl_ms=100,
                control_profile='vendor_fast', profile_status='manual_acceptance_pending',
                physical_execution_verified=False,
                incident_root_cause_status='not_fully_determined',
                quick_mode=2, arm_interpolator_enabled=True,
                send_rate_hz=100, vendor_fast_profile_supported=True,
                vendor_initialization=VENDOR_INITIALIZATION,
                blockers=[],
                candidate_ttl_ms=200, robot_runtime='Python 3.8 / vendor ROS Noetic',
                **({'return_home_enabled': True,
                    'return_home_config_sha256': bundle['return_home_config_sha256'],
                    'return_home_buttons': {'request': 'left_y', 'cancel': 'right_b'}}
                   if args.return_home_config is not None else {}),
                **target_progress_profile(args.target_recovery),
                **posture_profile(args.arm_posture, args.target_recovery))))
            return 0
        channel_factory = None
        command = None
        if args.session_transport == 'ssh':
            socket = Path(args.ssh_control_path)
            if not stat.S_ISSOCK(socket.stat().st_mode):
                raise ValueError('SSH master socket missing; no fallback connection attempted')
            command = build_execution_ssh_command(control_path=args.ssh_control_path,
                                                 gripper_control=args.grippers == 'damiao')
        else:
            from teleop.robots.kuavo5_network_config import read_auth_key
            from teleop.robots.kuavo5_direct_channel import DirectSessionChannel
            key = read_auth_key(args.network_token_file)
            host, port, cert_pin = network_endpoint
            socket_factory = None
            if args.network_namespace == 'kuavons':
                from teleop.robots.kuavo5_session_tcp_socket import namespace_socket
                socket_factory = lambda: namespace_socket(ROOT)
            channel_factory = lambda: DirectSessionChannel(
                (host, port), key, duration_s=args.duration,
                cert_sha256=cert_pin, expected_backend='robot', socket_factory=socket_factory)
        if not args.standalone_return:
            check = subprocess.run(['ss', '-Htn', '( sport = :60061 or dport = :60061 )'],
                                   capture_output=True, text=True, timeout=3, check=True)
            if check.stdout.strip():
                raise ValueError('PICO feedback client already connected; close that preview first')
        path = args.log_file or ROOT/'artifacts/kuavo5-arms-live'/(
            datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as journal:
            print('本次日志：'+str(path), flush=True)
            if channel_factory is None:
                return run_host(command, bundle, duration=args.duration, journal=journal, session_control=session_control)
            return run_host(command, bundle, duration=args.duration, journal=journal,
                            channel_factory=channel_factory, session_control=session_control)
    except Exception as exc:
        print('拒绝启动：'+str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
