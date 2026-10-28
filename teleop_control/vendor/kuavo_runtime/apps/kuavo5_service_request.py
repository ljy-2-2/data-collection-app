#!/usr/bin/env python3
"""Shared robot-local/PC management entry. Default: no network or hardware I/O."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT/'src') not in sys.path:
    sys.path.insert(0, str(ROOT/'src'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--status', action='store_true', help='read-only service identity and session status')
    actions.add_argument('--stop-session', metavar='ROBOT_SESSION_ID', help='request cleanup of this specific session')
    actions.add_argument('--enter-maintenance', action='store_true', help=argparse.SUPPRESS)
    actions.add_argument('--leave-maintenance', action='store_true', help=argparse.SUPPRESS)
    actions.add_argument('--robot-status', action='store_true', help='observe controller and preparation metadata; no CAN query')
    actions.add_argument('--start-controller', '--prepare-controller', dest='start_controller', action='store_true')
    actions.add_argument('--initialize', action='store_true')
    actions.add_argument('--inspect-grippers', action='store_true')
    actions.add_argument('--prepare-grippers', action='store_true')
    actions.add_argument('--preparation-job', metavar='REQUEST_ID', help='observe a previous preparation receipt without repeating it')
    actions.add_argument('--settings', action='store_true', help='read shared idle-session parameters')
    actions.add_argument('--preview-settings', metavar='JSON')
    actions.add_argument('--apply-settings', metavar='JSON')
    actions.add_argument('--reset-settings', action='store_true', help='restore parameter defaults, not robot home')
    parser.add_argument('--expected-settings-revision', type=int)
    parser.add_argument('--confirm-settings', action='store_true')
    parser.add_argument('--confirm-motion', action='store_true')
    parser.add_argument('--motor-ids-confirmed', action='store_true')
    parser.add_argument('--accept-unverified-gripper-bus', action='store_true')
    parser.add_argument('--confirm-stop', action='store_true')
    parser.add_argument('--confirm-maintenance', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--expected-service-instance-id', help='observed service identity for a receipt or maintenance request')
    parser.add_argument('--expected-installation-sha256', help='observed installation identity for a receipt or maintenance request')
    parser.add_argument('--config', type=Path,
                        help='private client JSON with local token path and reviewed connection policy')
    parser.add_argument('--network-namespace', choices=('kuavons',),
                        help='PC only: use existing managed network namespace; no automatic setup')
    args = parser.parse_args(argv)
    unknown_type = ()
    try:
        from teleop.robots.kuavo5_service_client import settings_cli_options
        settings = settings_cli_options(args)
        preparation = next((name for chosen, name in (
            (args.robot_status, 'status'), (args.start_controller, 'start'),
            (args.initialize, 'initialize'), (args.inspect_grippers, 'grippers_inspect'),
            (args.prepare_grippers, 'grippers_prepare')) if chosen), None)
        maintenance = args.enter_maintenance or args.leave_maintenance
        if args.confirm_maintenance and not maintenance:
            raise ValueError('maintenance confirmation and identity require a maintenance action')
        if (args.expected_service_instance_id is not None or args.expected_installation_sha256 is not None) and not (maintenance or args.preparation_job or settings):
            raise ValueError('expected identity requires maintenance or a preparation receipt query')
        if maintenance and (not args.confirm_maintenance or
                args.expected_service_instance_id is None or args.expected_installation_sha256 is None):
            raise ValueError('maintenance requires explicit confirmation and expected service/installation identity')
        if args.preparation_job and (args.expected_service_instance_id is None or args.expected_installation_sha256 is None):
            raise ValueError('preparation receipt requires expected service and installation identity')
        if args.confirm_motion and preparation not in ('start', 'initialize', 'grippers_prepare'):
            raise ValueError('--confirm-motion requires an explicit preparation action')
        if args.motor_ids_confirmed and preparation != 'start':
            raise ValueError('--motor-ids-confirmed applies only to --start-controller')
        if args.accept_unverified_gripper_bus and preparation not in ('grippers_inspect', 'grippers_prepare'):
            raise ValueError('--accept-unverified-gripper-bus requires a gripper preparation action')
        if preparation in ('start', 'initialize', 'grippers_prepare') and not args.confirm_motion:
            raise ValueError('this preparation action requires --confirm-motion')
        if preparation == 'start' and not args.motor_ids_confirmed:
            raise ValueError('--start-controller requires --motor-ids-confirmed')
        if preparation in ('grippers_inspect', 'grippers_prepare') and not args.accept_unverified_gripper_bus:
            raise ValueError('gripper inspection/preparation requires --accept-unverified-gripper-bus')
        if args.confirm_stop and args.stop_session is None:
            raise ValueError('--confirm-stop requires --stop-session')
        if args.stop_session is not None and not args.confirm_stop:
            raise ValueError('--stop-session requires explicit --confirm-stop')
        if not args.status and args.stop_session is None and not maintenance and preparation is None and not args.preparation_job and not settings:
            print(json.dumps(dict(event='KUAVO_SERVICE_REQUEST_DRY_RUN',
                network_connected=False, session_start_requested=False,
                stop_requested=False, physical_robot_command_sent=False)))
            return 0
        if args.config is None:
            raise ValueError('--config is required; no automatic service or credentials discovery')
        sys.path.insert(0, str(ROOT/'src'))
        from teleop.robots.kuavo5_service_client import KuavoServiceClient
        from teleop.robots.kuavo5_direct_channel import StopOutcomeUnknown, MaintenanceOutcomeUnknown, PreparationOutcomeUnknown, SettingsOutcomeUnknown
        unknown_type = (StopOutcomeUnknown, MaintenanceOutcomeUnknown, PreparationOutcomeUnknown, SettingsOutcomeUnknown)
        socket_factory = None
        if args.network_namespace:
            if not (ROOT/'scripts/kuavo-network').is_file():
                raise ValueError('--network-namespace is PC-only; omit it on the robot')
            from teleop.robots.kuavo5_session_tcp_socket import namespace_socket
            socket_factory = lambda: namespace_socket(ROOT)
        client = KuavoServiceClient.from_config(args.config, socket_factory=socket_factory)
        if settings:
            name, positional, keyword = settings
            result = getattr(client, name)(*positional, **keyword)
        elif preparation:
            result = client.preparation(preparation, confirm_motion=args.confirm_motion,
                motor_ids_confirmed=args.motor_ids_confirmed,
                accept_unverified_gripper_bus=args.accept_unverified_gripper_bus)
        elif args.preparation_job:
            result = client.preparation_job(args.preparation_job,
                expected_service_instance_id=args.expected_service_instance_id,
                expected_installation_sha256=args.expected_installation_sha256)
        elif maintenance:
            result = client.maintenance('enter' if args.enter_maintenance else 'leave',
                expected_service_instance_id=args.expected_service_instance_id,
                expected_installation_sha256=args.expected_installation_sha256, confirm=True)
        else:
            result = (client.get_status() if args.status else
                      client.stop_session(args.stop_session, confirm_stop=True))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if settings:
            return 0 if result.get('ok') is True else 1
        if preparation or args.preparation_job:
            return 1 if result.get('error') is not None or (result.get('result') or {}).get('ok') is False else 0
        return 0
    except (OSError, ValueError, RuntimeError, EOFError) as exc:
        result = dict(event='KUAVO_SERVICE_REQUEST_ERROR', reason=str(exc),
            outcome_unknown=isinstance(exc, unknown_type))
        for key in ('request_id', 'service_instance_id', 'installation_sha256'):
            if hasattr(exc, key):
                result[key] = getattr(exc, key)
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
