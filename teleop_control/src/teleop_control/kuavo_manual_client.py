"""Isolated direct-network manual client; no XR, SSH or actuator imports."""
import json
import queue
import sys
import threading
import time
from .kuavo_worker import client_module, connection


def run(options, emit):
    client_module()  # Vendor package isolation: this is a dedicated subprocess.
    from teleop.robots.kuavo5_direct_channel import DirectSessionChannel
    from teleop.robots.kuavo5_network_config import read_auth_key
    from teleop.robots.kuavo5_manual_control import PROTOCOL, validate_command
    config = connection(options['config'])
    channel = DirectSessionChannel((config['network_host'],config['network_port']),
        read_auth_key(config['network_token_file']),cert_sha256=config['network_cert_sha256'],duration_s=0.)
    commands = queue.Queue(maxsize=1)
    lock = threading.Lock()
    stop = threading.Event()
    state = dict(ui_at=time.monotonic(),base='stop',base_at=0.)
    def listen():
        try:
            for line in sys.stdin:
                if line.strip() == 'close': break
                if len(line)>4096: raise ValueError('manual request too large')
                command = validate_command(json.loads(line))
                with lock:
                    state['ui_at'] = time.monotonic()
                    if command['action'] == 'base':
                        state.update(base=command['direction'],base_at=time.monotonic())
                    elif command['action'] != 'heartbeat':
                        state.update(base='stop',base_at=0.)
                        if command['action']=='stop':
                            try: commands.get_nowait()
                            except queue.Empty: pass
                        try: commands.put_nowait(command)
                        except queue.Full: emit(dict(event='manual_ack',ok=False,error='上一指令尚未发送，请稍后再操作'))
        except Exception as exc: emit(dict(error=str(exc)))
        finally: stop.set()
    try:
        status = channel._connect_status()
        if status.get('manual_protocol') != PROTOCOL:
            raise ValueError('机器人端尚未安装手动控制服务')
        if status.get('backend') != 'robot' or not status.get('ready_to_start') or status.get('owner_present'):
            raise ValueError('机器人控制权尚未释放：'+str(status.get('blocked_reason') or status.get('phase')))
        channel._send(dict(event='start_manual_session',protocol=PROTOCOL,confirm_motion=True,
            expected_service_instance_id=status['service_instance_id'],
            expected_installation_sha256=status['installation_sha256']),timeout_s=.5)
        reply = channel._payload(time.monotonic()+3.)
        if (reply.get('event') != 'manual_session_started' or reply.get('protocol') != PROTOCOL
                or reply.get('service_instance_id') != status['service_instance_id']
                or reply.get('installation_sha256') != status['installation_sha256']):
            raise ValueError('手动控制启动被拒绝：'+str(reply.get('reason') or reply))
        channel.session_id = reply['session_id']
        threading.Thread(target=listen,daemon=True).start()
        closing = False; until = time.monotonic()+35.
        while True:
            with lock: ui_lost = time.monotonic()-state['ui_at'] > 1.
            if (stop.is_set() or ui_lost) and not closing:
                channel.close_input(); closing = True; until = time.monotonic()+8.
            try: payload = channel._payload(min(until,time.monotonic()+.2))
            except TimeoutError:
                if time.monotonic() >= until: raise
                continue
            if payload.get('event') == 'refused': raise RuntimeError(str(payload.get('reason')))
            if (payload.get('event') != 'robot_packet' or payload.get('session_id') != channel.session_id):
                raise ValueError('手动会话响应身份不一致')
            packet = payload['packet']
            if packet.get('event') == 'manual_challenge' and not closing:
                with lock:
                    try: command = commands.get_nowait()
                    except queue.Empty:
                        direction = state['base'] if time.monotonic()-state['base_at'] < .15 else 'stop'
                        command = dict(action='base',direction=direction)
                channel._send(dict(event='manual_input',session_id=channel.session_id,packet=dict(
                    event='manual_input',challenge_id=packet['challenge_id'],command=command)),timeout_s=.05)
            else: emit(packet)
            if packet.get('event') == 'ready': until = float('inf')
            if packet.get('event') == 'closed': return
    finally:
        # Disconnect invokes existing supervisor cleanup; never kills its worker.
        channel._abort()


if __name__ == '__main__':
    def emit(value): print('KUAVO:'+json.dumps(value,ensure_ascii=False,allow_nan=False),flush=True)
    try: run(json.loads(sys.stdin.readline()),emit)
    except Exception as exc: emit(dict(error=type(exc).__name__+': '+str(exc))); sys.exit(1)
