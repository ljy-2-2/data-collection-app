#!/usr/bin/env python3
"""PC input client for the robot's current compatible formal release.

Default is local validation only. No source bundle, robot model, gripper
calibration or return configuration is uploaded or selected by this client.
"""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import sys
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


def channel_factory(config, namespace, duration=0.):
    # Called only for explicit --status/--execute; construction stays inert.
    from teleop.robots.kuavo5_direct_channel import DirectSessionChannel
    from teleop.robots.kuavo5_network_config import read_auth_key
    factory = None
    if namespace == 'kuavons':
        from teleop.robots.kuavo5_session_tcp_socket import namespace_socket
        factory = lambda: namespace_socket(ROOT)
    return DirectSessionChannel((config['network_host'], config['network_port']),
        read_auth_key(config['network_token_file']),
        cert_sha256=config['network_cert_sha256'], socket_factory=factory, duration_s=duration)


def select_session(config, namespace):
    from teleop.robots.kuavo5_service_contract import validate_service_contract
    status = channel_factory(config, namespace).query_status()
    contract = validate_service_contract(status, config['required_contract'])
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


def main(argv=None, *, session_control=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--status', action='store_true')
    action.add_argument('--execute', action='store_true')
    action.add_argument('--dry-run', action='store_true')
    action.add_argument('--robot-status', action='store_true', help='controller and gripper preparation metadata; no CAN query')
    action.add_argument('--start-controller', '--prepare-controller', dest='start_controller', action='store_true')
    action.add_argument('--initialize', action='store_true')
    action.add_argument('--inspect-grippers', action='store_true')
    action.add_argument('--prepare-grippers', action='store_true')
    action.add_argument('--preparation-job', metavar='REQUEST_ID', help='read an existing preparation receipt, never repeat its action')
    parser.add_argument('--confirm-motion', action='store_true')
    parser.add_argument('--motor-ids-confirmed', action='store_true')
    parser.add_argument('--expected-service-instance-id')
    parser.add_argument('--expected-installation-sha256')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--network-namespace', choices=('kuavons', 'current'), default='kuavons')
    parser.add_argument('--duration', type=float, default=0., help='0: run until Ctrl+C/platform stop')
    parser.add_argument('--accept-unverified-gripper-bus', action='store_true')
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error('duration must be finite and nonnegative')
    try:
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
        if (args.expected_service_instance_id is not None or args.expected_installation_sha256 is not None) and not args.preparation_job:
            raise ValueError('expected identity applies only to --preparation-job')
        if args.preparation_job and (args.expected_service_instance_id is None or args.expected_installation_sha256 is None):
            raise ValueError('preparation receipt requires expected service and installation identity')
        config = load_config(args.config)
        if not args.execute and not args.status and preparation is None and not args.preparation_job:
            print(json.dumps(dict(event='KUAVO_CURRENT_CLIENT_DRY_RUN',
                connection_policy=config['connection_policy'],
                required_contract=config['required_contract'],
                configuration_source='robot_formal_installation', duration=args.duration,
                network_connected=False, pico_sdk_started=False, ssh_started=False,
                physical_robot_command_sent=False), ensure_ascii=False))
            return 0
        if args.status or preparation is not None or args.preparation_job:
            from teleop.robots.kuavo5_service_client import KuavoServiceClient
            client = KuavoServiceClient(config)
            # Keep credential handling and namespace behavior identical to execute.
            client._channel = lambda: channel_factory(config, args.network_namespace)
            if preparation is not None:
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
            if args.status:
                return 0 if report['management_ready'] else 1
            return 1 if report.get('error') is not None or (report.get('result') or {}).get('ok') is False else 0
        if session_control is not None and session_control.stop_event.is_set():
            return 0
        contract = select_session(config, args.network_namespace)
        if not contract['shared_bus_compatibility_verified'] and not args.accept_unverified_gripper_bus:
            raise ValueError('当前夹爪配置仍要求 --accept-unverified-gripper-bus；尚未启动 PICO 或遥操')
        from teleop.robots.kuavo5_service_contract import host_bundle
        bundle = host_bundle(contract)
        logs = ROOT / 'artifacts/kuavo5-arms-live'
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / (time.strftime('%Y%m%d-%H%M%S')+'-current-'+uuid.uuid4().hex[:8]+'.jsonl')
        print('本次日志：'+str(log), flush=True)
        capabilities = '双臂 0.7 rad/s＋夹爪'
        if 'return_yb_v1' in contract['capabilities']:
            capabilities += '＋Y 回位/B 取消'
        print('使用机器人正式版本：'+contract['installation_sha256'][:12]+
              '；'+capabilities+'。', flush=True)
        with log.open('x', encoding='utf-8') as journal:
            journal.write(json.dumps(dict(event='selected_service_contract',
                service_contract=contract), ensure_ascii=False)+'\n')
            journal.flush()
            return host_module().run_host(None, bundle, duration=args.duration, journal=journal,
                channel_factory=lambda: channel_factory(config, args.network_namespace, args.duration),
                service_contract=contract, session_control=session_control)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, EOFError) as exc:
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
