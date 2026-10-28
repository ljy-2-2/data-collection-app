"""Owned HTTPS reader for live pose and sensor recording. No actuator calls."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import sys
import threading
import time


def ssh_command(config, options):
    settings_path = Path(options['config']).with_name('readonly.json')
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    if not isinstance(settings, dict) or set(settings)-{'user', 'ros_setup', 'control_path'}:
        raise ValueError('readonly.json 仅支持 user、ros_setup、control_path')
    user = settings.get('user', 'lab')
    setup = settings.get('ros_setup', '/home/lab/kuavo-ros-control/devel/setup.bash')
    if not isinstance(user, str) or not re.fullmatch('[a-zA-Z_][a-zA-Z0-9_-]{0,31}', user):
        raise ValueError('只读 SSH 用户名无效')
    if not isinstance(setup, str) or not setup.startswith('/') or '\n' in setup:
        raise ValueError('只读 ROS 环境文件须为绝对路径')
    command = ['ssh', '-T', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=2']
    if settings.get('control_path'):
        path = settings['control_path']
        if not isinstance(path, str) or not path.startswith('/') or '\n' in path:
            raise ValueError('只读 SSH control_path 须为绝对路径')
        command += ['-S', path]
    command += [user+'@'+config['network_host'],
                'bash -lc '+shlex.quote('source '+shlex.quote(setup)+' && exec python3 -u -')]
    if options.get('network_namespace') == 'kuavons':
        command = ['ip', 'netns', 'exec', 'kuavons', *command]
    return command


def monitor(config, options, emit):
    from teleop.robots.kuavo5 import Kuavo5Adapter, v6_candidate_schema
    from teleop.robots.kuavo5_state_receiver import Kuavo5StateReceiver
    from teleop.robots.kuavo5_readonly_stream import KuavoReadonlyFeed
    from .kuavo_preview import ASSETS
    from .kuavo_network_diagnostics import inspect_route
    route = inspect_route(config['network_host'], options.get('network_namespace', 'current'))
    if route['blocked']:
        raise ValueError(route['detail'])
    xml = (ASSETS/'full_robot.urdf').read_bytes()
    schema = v6_candidate_schema(xml.decode())
    receiver = Kuavo5StateReceiver(Kuavo5Adapter(schema, robot_version='63'),
                                   joint_names=schema.names, robot_version='63')
    feed = KuavoReadonlyFeed(receiver, version='63', names=schema.names,
                            urdf_sha256=hashlib.sha256(xml).hexdigest(),maximum_source_age_ms=None)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    def listen():
        for line in sys.stdin:
            if line.strip() == 'stop':break
        stop.set()
    threading.Thread(target=listen, daemon=True).start()
    errors = deque(maxlen=6)
    reader_env = {key:value for key,value in os.environ.items() if key not in ('LD_LIBRARY_PATH','LD_PRELOAD')}
    from .kuavo_network_client import profile
    root=Path(options['config']).resolve().parents[3]
    network=profile(root,config['network_host'])
    command=[sys.executable,'-u',str(Path(__file__).with_name('kuavo_network_client.py')),json.dumps(network),'/joints','{}']
    if options.get('network_namespace')=='kuavons':command=['ip','netns','exec','kuavons',*command]
    process = subprocess.Popen(command, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=reader_env)
    def diagnostics():
        for line in process.stderr:
            errors.append(line.decode(errors='replace').strip()[-400:])
    threading.Thread(target=diagnostics, daemon=True).start()
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        buffer = b''
        started = time.monotonic();next_pose=0.
        while not stop.is_set():
            samples=[]
            for key, _ in selector.select(.02):
                block = os.read(key.fd, 65536)
                if not block:
                    raise RuntimeError('只读网络数据流断开：'+'; '.join(errors))
                buffer += block
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if not feed.accept_line(line.decode()):
                        raise ValueError(receiver.snapshot()['reason'])
                    packet=json.loads(line)
                    if packet.get('event')=='frame':
                        from .kuavo_preview import ARM_NAMES
                        import math
                        indexes=[schema.names.index(n) for n in ARM_NAMES]
                        velocity=packet.get('joint_v') or []
                        samples.append(dict(kind='joint_sample',pc_monotonic_ns=time.monotonic_ns(),
                            source_age_ms=packet['source_age_ms'],
                            source_timestamp_ns=packet['sensor_secs']*1_000_000_000+packet['sensor_nsecs'],
                            source_sequence=packet.get('sequence'),joint_names=list(ARM_NAMES),
                            position_rad=[packet['joint_q'][i] for i in indexes],
                            velocity_rad_s=[velocity[i] if len(velocity)==len(schema.names) and math.isfinite(velocity[i]) else None for i in indexes]))
                if len(buffer)>65536:raise ValueError('只读姿态数据过长')
            if samples:emit(dict(collection_events=samples))
            if time.monotonic()<next_pose:continue
            next_pose=time.monotonic()+.25
            state = receiver.snapshot()
            if not feed.ready and time.monotonic()-started>10:
                raise RuntimeError('只读姿态握手超时，请检查网络采集服务和机器人 ROS 环境')
            age_ms=None if state['age_ms'] is None else state['age_ms']+feed.last_source_age_ms
            fresh = state['status']=='fresh' and age_ms is not None and age_ms<=250
            pose_status='stale' if state['status']=='fresh' and not fresh else state['status']
            emit(dict(pose_status=pose_status, pose_detail='实测关节姿态在线' if fresh else
                      '等待新的实测关节姿态：'+state['reason'],
                      telemetry=dict(joints=state['positions_rad'], timestamp_ns=state['source_timestamp_ns'],
                                     age_s=None if age_ms is None else age_ms/1000)))
        return 0
    finally:
        selector.close();receiver.stop()
        # Close only our owned HTTP reader; the robot service releases its subscriber.
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)

