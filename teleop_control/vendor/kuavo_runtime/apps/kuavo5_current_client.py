#!/usr/bin/env python3
"""PC input client for the robot's current compatible formal release.

Default is local validation only. No source bundle, robot model, gripper
calibration or return configuration is uploaded or selected by this client.
"""
import argparse
from contextlib import nullcontext
import importlib.util
import json
import math
from pathlib import Path
import sys
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / '.local/kuavo-network/client-current.json'
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))


def load_config(path):
    from teleop.robots.kuavo5_service_client import load_request_config
    config = load_request_config(path)
    if config.get('connection_policy') != 'compatible_service_v1':
        raise ValueError('需要兼容服务配置；原固定版本配置请继续使用原入口')
    return config


def channel_factory(config, namespace, duration=0., *, allow_base_control=False):
    # Called only for explicit --status/--execute; construction stays inert.
    from teleop.robots.kuavo5_direct_channel import DirectSessionChannel
    from teleop.robots.kuavo5_network_config import read_auth_key
    factory = None
    if namespace == 'kuavons':
        from teleop.robots.kuavo5_session_tcp_socket import namespace_socket
        factory = lambda: namespace_socket(ROOT)
    return DirectSessionChannel((config['network_host'], config['network_port']),
        read_auth_key(config['network_token_file']),
        cert_sha256=config['network_cert_sha256'], socket_factory=factory, duration_s=duration,
        **({'allow_base_control': True} if allow_base_control else {}))


def select_session(config, namespace, *, expected_identity=None):
    from teleop.robots.kuavo5_service_contract import validate_service_contract, compatible_requirements
    status = channel_factory(config, namespace).query_status()
    contract = validate_service_contract(status, compatible_requirements(status, config['required_contract']))
    if expected_identity is not None and any(contract.get(k) != v for k, v in expected_identity.items()):
        raise ValueError('服务在准备后更新或重启；尚未开始遥操，请重新核对')
    if (status.get('backend') != 'robot' or status.get('ready_to_start') is not True
            or status.get('owner_present') is not False or status.get('phase') != 'idle'):
        raise ValueError('机器人服务尚未允许新会话；请先查询状态，不会接管已有操作')
    return contract


def host_module():
    spec = importlib.util.spec_from_file_location('kuavo_current_input_host',
        ROOT / 'apps/pico_kuavo5_arms_live.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_video_connection():
    """Explicit foreground authentication only; no camera, SDK or robot session."""
    import io
    from teleop.vision import kuavo_session as video
    print('准备视频连接：如有提示，在本终端输入电脑或 AGX 密码。这里只登录视频电脑，不启动遥操。', flush=True)
    with io.StringIO() as log:
        master = video.Session(log).connect(video.private_directory())
    print(json.dumps(dict(event='KUAVO_VIDEO_CONNECTION_READY', ssh_socket=master,
        camera_started=False, pico_started=False, control_session_started=False,
        physical_robot_command_sent=False), ensure_ascii=False), flush=True)
    return 0


def run_wizard(config, args, session_control):
    from teleop.robots.kuavo5_current_wizard import PreparationWizard, WizardCancelled
    from teleop.robots.kuavo5_service_client import KuavoServiceClient
    from teleop.robots.kuavo5_wizard_input_check import prepare_network, check_pico
    from teleop.robots.kuavo5_wizard_receipts import ReceiptStore
    client = KuavoServiceClient(config)
    client._channel = lambda: channel_factory(config, args.network_namespace)
    def execute(identity):
        video_options = (['--with-video', '--video-fps', str(args.video_fps),
                          '--video-quality', str(args.video_quality)] if getattr(args, 'with_video', False) else [])
        if getattr(args, 'with_base', False):
            video_options.append('--with-base')
        return main(['--execute', '--config', str(args.config),
                     '--network-namespace', args.network_namespace,
                     '--duration', str(args.duration), '--accept-unverified-gripper-bus', *video_options],
                    session_control=session_control, expected_identity=identity)
    state_dir = args.config.resolve().parent / 'wizard-state'
    print('准备请求记录：' + str(state_dir), flush=True)
    try:
        with ReceiptStore(state_dir) as receipts:
            wizard = PreparationWizard(client, receipts,
                prepare_network=lambda: prepare_network(ROOT, args.network_namespace, service_port=config['network_port']),
                check_pico=lambda: check_pico(ROOT), execute=execute,
                say=lambda message: print(message, flush=True),
                bus_accepted=args.accept_unverified_gripper_bus)
            return wizard.run()
    except (WizardCancelled, KeyboardInterrupt, EOFError):
        print('已退出本轮向导。没有请求关闭厂家主控；已发出的准备请求不会自动撤销。', flush=True)
        return 130


def main(argv=None, *, session_control=None, expected_identity=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--status', action='store_true')
    action.add_argument('--execute', action='store_true')
    action.add_argument('--dry-run', action='store_true')
    action.add_argument('--prepare-video', action='store_true',
                        help='prepare video login in this terminal; no camera or robot control')
    action.add_argument('--wizard', action='store_true', help='interactive direct-network preparation and teleoperation')
    action.add_argument('--wizard-preview', action='store_true', help='show workflow without network or PICO')
    action.add_argument('--wizard-check-pico', action='store_true', help=argparse.SUPPRESS)
    action.add_argument('--robot-status', action='store_true', help='controller and gripper preparation metadata; no CAN query')
    action.add_argument('--start-controller', '--prepare-controller', dest='start_controller', action='store_true')
    action.add_argument('--initialize', action='store_true')
    action.add_argument('--inspect-grippers', action='store_true')
    action.add_argument('--prepare-grippers', action='store_true')
    action.add_argument('--preparation-job', metavar='REQUEST_ID', help='read an existing preparation receipt, never repeat its action')
    action.add_argument('--settings', action='store_true', help='read shared idle-session parameters')
    action.add_argument('--preview-settings', metavar='JSON', help='validate proposed parameters without applying them')
    action.add_argument('--apply-settings', metavar='JSON', help='apply bounded parameters while idle')
    action.add_argument('--reset-settings', action='store_true', help='restore formal parameter defaults, not robot home')
    parser.add_argument('--expected-settings-revision', type=int)
    parser.add_argument('--confirm-settings', action='store_true')
    parser.add_argument('--confirm-motion', action='store_true')
    parser.add_argument('--motor-ids-confirmed', action='store_true')
    parser.add_argument('--expected-service-instance-id')
    parser.add_argument('--expected-installation-sha256')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--network-namespace', choices=('kuavons', 'current'), default='kuavons')
    parser.add_argument('--duration', type=float, default=0., help='0: run until Ctrl+C/platform stop')
    parser.add_argument('--accept-unverified-gripper-bus', action='store_true')
    parser.add_argument('--with-video', action='store_true', help='one joint head-video and formal arm/gripper session')
    parser.add_argument('--with-base', action='store_true', help='explicitly permit the published X-toggle base in this same session')
    parser.add_argument('--video-fps', type=int, choices=(30, 60), default=30)
    parser.add_argument('--video-quality', type=int, choices=range(1, 101), default=90, metavar='1..100')
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error('duration must be finite and nonnegative')
    try:
        if args.with_video and not (args.execute or args.wizard or args.dry_run):
            raise ValueError('--with-video requires --execute, --wizard or --dry-run; no devices opened')
        if args.with_base and not (args.execute or args.wizard or args.dry_run):
            raise ValueError('--with-base requires --execute, --wizard or --dry-run; no devices opened')
        from teleop.robots.kuavo5_service_client import settings_cli_options
        settings = settings_cli_options(args)
        if args.wizard_preview:
            from teleop.robots.kuavo5_current_wizard import PREVIEW
            print(PREVIEW)
            return 0
        if args.wizard_check_pico:
            from teleop.robots.kuavo5_wizard_input_check import probe_pico
            print('\n' + json.dumps(probe_pico()), flush=True)
            return 0
        if args.wizard and not sys.stdin.isatty():
            raise ValueError('--wizard 需要交互终端逐步输入 YES；未连接机器人。平台请使用明确的请求接口。')
        preparation = next((name for chosen, name in (
            (args.robot_status, 'status'), (args.start_controller, 'start'),
            (args.initialize, 'initialize'), (args.inspect_grippers, 'grippers_inspect'),
            (args.prepare_grippers, 'grippers_prepare')) if chosen), None)
        if args.confirm_motion and preparation not in ('start', 'initialize', 'grippers_prepare'):
            raise ValueError('--confirm-motion requires an explicit preparation action')
        if args.motor_ids_confirmed and preparation != 'start':
            raise ValueError('--motor-ids-confirmed applies only to --start-controller')
        if preparation in ('start', 'initialize', 'grippers_prepare') and not args.confirm_motion:
            raise ValueError('this preparation action requires --confirm-motion')
        if preparation == 'start' and not args.motor_ids_confirmed:
            raise ValueError('--start-controller requires --motor-ids-confirmed')
        if preparation in ('grippers_inspect', 'grippers_prepare') and not args.accept_unverified_gripper_bus:
            raise ValueError('gripper inspection/preparation requires --accept-unverified-gripper-bus')
        if (args.expected_service_instance_id is not None or args.expected_installation_sha256 is not None) and not (args.preparation_job or settings):
            raise ValueError('expected identity applies only to --preparation-job')
        if args.preparation_job and (args.expected_service_instance_id is None or args.expected_installation_sha256 is None):
            raise ValueError('preparation receipt requires expected service and installation identity')
        config = load_config(args.config)
        if session_control is not None and session_control.stop_event.is_set():
            return 0
        if args.prepare_video:
            return prepare_video_connection()
        if args.wizard:
            return run_wizard(config, args, session_control)
        if not args.execute and not args.status and preparation is None and not args.preparation_job and not settings:
            print(json.dumps(dict(event='KUAVO_CURRENT_CLIENT_DRY_RUN',
                connection_policy=config['connection_policy'],
                required_contract=config['required_contract'],
                configuration_source='robot_formal_installation', duration=args.duration,
                joint_video_requested=args.with_video,
                joint_base_requested=args.with_base,
                network_connected=False, pico_sdk_started=False, ssh_started=False,
                physical_robot_command_sent=False), ensure_ascii=False))
            return 0
        if args.status or preparation is not None or args.preparation_job or settings:
            from teleop.robots.kuavo5_service_client import KuavoServiceClient
            client = KuavoServiceClient(config)
            # Keep credential handling and namespace behavior identical to execute.
            client._channel = lambda: channel_factory(config, args.network_namespace)
            if settings:
                name, positional, keyword = settings
                report = getattr(client, name)(*positional, **keyword)
            elif preparation is not None:
                report = client.preparation(preparation, confirm_motion=args.confirm_motion,
                    motor_ids_confirmed=args.motor_ids_confirmed,
                    accept_unverified_gripper_bus=args.accept_unverified_gripper_bus)
            elif args.preparation_job:
                report = client.preparation_job(args.preparation_job,
                    expected_service_instance_id=args.expected_service_instance_id,
                    expected_installation_sha256=args.expected_installation_sha256)
            else:
                report = client.get_status()
            print(json.dumps(report, ensure_ascii=False, allow_nan=False))
            if settings:
                return 0 if report.get('ok') is True else 1
            if args.status:
                return 0 if report['management_ready'] else 1
            return 1 if report.get('error') is not None or (report.get('result') or {}).get('ok') is False else 0
        if session_control is not None and session_control.stop_event.is_set():
            return 0
        pins = {} if expected_identity is None else dict(expected_identity=expected_identity)
        contract = select_session(config, args.network_namespace, **pins)
        if not contract['shared_bus_compatibility_verified'] and not args.accept_unverified_gripper_bus:
            raise ValueError('当前夹爪配置仍要求 --accept-unverified-gripper-bus；尚未启动 PICO 或遥操')
        from teleop.robots.kuavo5_service_contract import host_bundle, TOGGLE_RETURN_CAPABILITY, BASE_CAPABILITY
        base_enabled = BASE_CAPABILITY in contract['capabilities']
        if args.with_base != base_enabled:
            raise ValueError('当前正式版已包含底盘，请显式添加 --with-base；尚未打开 PICO 或相机。'
                             if base_enabled else '当前正式版尚未接入底盘；不能使用 --with-base。')
        if args.with_video and TOGGLE_RETURN_CAPABILITY not in contract['capabilities']:
            raise ValueError('联合会话要求已发布的 Y 单键回位/取消功能；当前正式版尚未提供，未打开相机或 PICO')
        bundle = host_bundle(contract)
        logs = ROOT / 'artifacts/kuavo5-arms-live'
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / (time.strftime('%Y%m%d-%H%M%S')+'-current-'+uuid.uuid4().hex[:8]+'.jsonl')
        print('本次日志：'+str(log), flush=True)
        capabilities = '双臂 %g rad/s＋夹爪' % contract['ready_expectations']['max_velocity_rad_s']
        if TOGGLE_RETURN_CAPABILITY in contract['capabilities']:
            capabilities += '＋Y 回位/再按 Y 取消'
        elif 'return_yb_v1' in contract['capabilities']:
            capabilities += '＋Y 回位/B 取消'
        print('使用机器人正式版本：'+contract['installation_sha256'][:12]+
              '；'+capabilities+'。', flush=True)
        if base_enabled:
            print('底盘已加入同一会话：X 切换锁定/解锁；启动时锁定，摇杆回中后按 X 解锁。', flush=True)
        if args.with_video:
            from teleop.robots.kuavo5_pc_session import SessionControl
            from teleop.vision.kuavo_joint_session import JointVideoSession
            session_control = session_control or SessionControl()
            video_context = JointVideoSession(session_control, fps=args.video_fps,
                quality=args.video_quality, installation_sha256=contract['installation_sha256'],
                **({'base_integrated': True} if base_enabled else {}))
        else:
            video_context = nullcontext()
        with log.open('x', encoding='utf-8') as journal, video_context as joint:
            journal.write(json.dumps(dict(event='selected_service_contract',
                service_contract=contract), ensure_ascii=False)+'\n')
            journal.flush()
            result = host_module().run_host(None, bundle, duration=args.duration, journal=journal,
                channel_factory=lambda: channel_factory(config, args.network_namespace, args.duration,
                    **({'allow_base_control': True} if base_enabled else {})),
                service_contract=contract, session_control=session_control)
            if joint is not None:
                session_control.finish(result)
            return result
    except KeyboardInterrupt:
        if session_control is not None:
            session_control.request_stop()
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, EOFError, subprocess.SubprocessError) as exc:
        result = dict(event='KUAVO_CURRENT_CLIENT_ERROR', reason=str(exc))
        if hasattr(exc, 'request_id'):
            result['outcome_unknown'] = True
            for key in ('request_id', 'service_instance_id', 'installation_sha256'):
                if hasattr(exc, key):
                    result[key] = getattr(exc, key)
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
