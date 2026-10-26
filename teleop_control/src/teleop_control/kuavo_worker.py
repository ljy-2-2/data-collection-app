"""Isolated adapter for the bundled KUAVO PC client; no IO on import."""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import signal
import ssl
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
VENDOR = ROOT / 'teleop_control/vendor/kuavo_runtime'


def client_module():
    # Never load this vendor's `teleop` package into the G2 backend process.
    sys.path.insert(0, str(VENDOR / 'src'))
    spec = importlib.util.spec_from_file_location('kuavo_app_client', VENDOR / 'apps/kuavo5_current_client.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def connection(path):
    from teleop.robots.kuavo5_service_client import validate_request_config
    from teleop.robots.kuavo5_network_config import read_auth_key
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError('KUAVO 连接配置权限须为 0600')
    value = json.loads(path.read_text())
    token = Path(value['network_token_file'])
    value['network_token_file'] = str((path.parent / token).resolve())
    value = validate_request_config(value)
    if value.get('connection_policy') != 'compatible_service_v1':
        raise ValueError('需要原版 compatible_service_v1 连接配置')
    read_auth_key(value['network_token_file'])
    return value


def execute(module, config, options, emit):
    from teleop.robots.kuavo5_pc_session import SessionControl
    from teleop.robots.kuavo5_service_contract import host_bundle
    from teleop.xr import XRoboToolkitInputAdapter
    from .kuavo_input import InputAdapter, Observation, ObservedChannel
    observation = Observation()
    control = SessionControl()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: control.request_stop())
    def listen():
        for line in sys.stdin:
            if line.strip() == 'stop':
                control.request_stop()
        control.request_stop()  # Backend exit requests the original orderly cleanup.
    threading.Thread(target=listen, daemon=True).start()
    done = threading.Event()
    def publish():
        while not done.wait(.25):
            emit({'session': control.status(), **observation.snapshot()})
    threading.Thread(target=publish, daemon=True).start()
    try:
        contract = module.select_session(config, options['network_namespace'])
        require_gripper_config(contract, options)
        if not contract['shared_bus_compatibility_verified'] and not options.get('accept_unverified_gripper_bus'):
            raise ValueError('需确认原项目的夹爪共享总线使用条件')
        if control.stop_event.is_set():
            return 0
        output = Path(options['output'])
        output.mkdir(parents=True, exist_ok=False)
        metadata = dict(task=options['task'], started_at=time.time(), complete=False,
                        arm_mode=options.get('arm_mode','dual'), position_scale=options.get('position_scale',1.),
                        kind='kuavo_session_journal', image_dataset=False, platform_upload=False)
        (output / 'meta.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        emit({'log_directory': str(output)})
        code = 1
        try:
            with (output / 'events.jsonl').open('x') as journal:
                journal.write(json.dumps({'event': 'selected_service_contract', 'service_contract': contract})+'\n')
                code = module.host_module().run_host(None, host_bundle(contract), duration=0., journal=journal,
                    xr_factory=lambda: InputAdapter(XRoboToolkitInputAdapter(stale_after_s=.1),
                        options.get('arm_mode','dual'),options.get('position_scale',1.),observation),
                    channel_factory=lambda: ObservedChannel(module.channel_factory(config, options['network_namespace'], 0.),observation),
                    service_contract=contract, session_control=control)
            control.finish(code)
        except Exception as exc:
            control.finish(1,str(exc))
            raise
        finally:
            metadata.update(finished_at=time.time(), complete=code in (0,130), report=control.status())
            (output / 'meta.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        return code
    except Exception as exc:
        control.finish(1, str(exc))
        return 1
    finally:
        done.set()
        emit({'session': control.status()})


def require_gripper_config(contract, options):
    expected = options.get('expected_gripper_sha256')
    if expected and contract.get('profile',{}).get('gripper_config_sha256') != expected:
        raise ValueError('本机夹爪参数尚未应用到机器人服务；请先更新机器人服务配置并检查设备')


def inspect_service(client, emit):
    """Preserve authenticated diagnostics and make an incompatible check fail."""
    status = client.get_status()
    emit(dict(service=status, preparation=None))
    error = status.get('compatibility_error')
    if error:
        actual = (status.get('service', {}).get('session_contract') or {}).get('protocol', '未知')
        raise ValueError(f'机器人服务已响应，但与 App 客户端不兼容（机器人协议：{actual}）。'
                         f'请更新 App 的乐聚客户端；无需因该错误修改网卡地址。详情：{error}')
    if not status.get('management_ready'):
        raise ValueError('机器人服务已响应，但服务身份或管理协议未通过核验；未启动遥操作')
    emit(dict(preparation=client.preparation('status')))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['check','execute','prepare','validate-grippers'])
    args = parser.parse_args()
    output = sys.stdout
    sys.stdout = sys.stderr  # Original client messages remain diagnostics, never protocol.
    lock = threading.Lock()
    def emit(value):
        with lock:
            output.write('KUAVO:'+json.dumps(value, ensure_ascii=False, allow_nan=False)+'\n');output.flush()
    try:
        options = json.loads(sys.stdin.readline())
        if args.action == 'validate-grippers':
            sys.path.insert(0, str(VENDOR/'src'))
            from teleop.control.kuavo5_gripper_calibration import load_runtime_config
            load_runtime_config(options)
            emit({'valid':True})
            return 0
        module = client_module()
        config = connection(options['config'])
        namespace = options.get('network_namespace', 'current')
        if namespace not in ('current','kuavons'):
            raise ValueError('无效网络方式')
        if args.action == 'execute':
            if options.get('confirm_motion') is not True:
                raise ValueError('需要操作者明确启动')
            if importlib.util.find_spec('xrobotoolkit_sdk') is None:
                raise ValueError('当前 Python 缺少 PICO xrobotoolkit_sdk')
            return execute(module, config, options, emit)
        from teleop.robots.kuavo5_service_client import KuavoServiceClient
        client = KuavoServiceClient(config)
        client._channel = lambda: module.channel_factory(config, namespace)
        if args.action == 'check':
            from .kuavo_network_diagnostics import inspect_route, tls_failure
            from .kuavo_vr import probe_subprocess
            emit(probe_subprocess())
            route = inspect_route(config['network_host'], namespace)
            emit({'network':route})
            if route['blocked']: raise ValueError(route['detail'])
            try:
                inspect_service(client, emit)
            except ssl.SSLEOFError as exc:
                raise RuntimeError(tls_failure(exc,config,namespace,route)) from None
        else:
            action = options['preparation_action']
            if action not in ('start','initialize','grippers_inspect','grippers_prepare'):
                raise ValueError('无效准备操作')
            if action == 'grippers_prepare' and options.get('expected_gripper_sha256'):
                status = client.get_status()
                if not status.get('management_ready'): raise ValueError('机器人服务连接尚未核验')
                require_gripper_config(status.get('service',{}), options)
            report = client.preparation(action,
                confirm_motion=options.get('confirm_motion',False),
                motor_ids_confirmed=options.get('motor_ids_confirmed',False),
                accept_unverified_gripper_bus=options.get('accept_unverified_gripper_bus',False))
            emit({'preparation': report})
            if report.get('error') is not None or (report.get('result') or {}).get('ok') is False:
                emit({'error':json.dumps(report.get('error') or report.get('result'),ensure_ascii=False)})
                return 1
        return 0
    except Exception as exc:
        value = {'error':str(exc)}
        for key in ('request_id','service_instance_id','installation_sha256'):
            if hasattr(exc,key):value[key]=getattr(exc,key)
        emit(value)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
