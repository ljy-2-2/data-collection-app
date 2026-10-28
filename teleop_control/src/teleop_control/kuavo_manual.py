"""App-owned manual session. No ROS imports, implicit connection or SSH."""
import copy
import json
import math
import secrets
import subprocess
import threading
import time


class KuavoManual:
    def __init__(self, manager):
        self.manager = manager
        from .kuavo_preview import ASSETS
        self.rows = json.loads((ASSETS/'manual-joints.json').read_text())
        self.lock = threading.RLock()
        self.process = None
        self.state = 'idle'
        self.error = ''
        self.session_id = None
        self.sequence = -1
        self.telemetry = {}
        self.received = 0.
        self.busy = False

    def active(self):
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def snapshot(self):
        with self.lock:
            service = (self.manager.value.get('service') or {}).get('service') or {}
            available = service.get('manual_protocol') == 'kuavo_manual_v1'
            active = self.active()
            fresh = active and time.monotonic()-self.received < .5
            return dict(state=self.state,active=active,available=available,
                session_id=self.session_id,error=self.error,busy=self.busy,
                reason='' if available else '机器人通信服务需要更新手动控制功能' if service else '请先检查设备',
                joints=copy.deepcopy(self.rows),telemetry=copy.deepcopy(self.telemetry),fresh=fresh)

    def start(self):
        m = self.manager
        # Explicit manual entry relinquishes only our prepared XR session.
        with m.lock:
            if m.closing: raise ValueError('App 正在退出')
            if m.active() and not m.standby: raise ValueError('请先结束遥操作')
            if m.pending_operation: raise ValueError('请等待检查设备完成')
            if not self.snapshot()['available']: raise ValueError(self.snapshot()['reason'])
        if m.standby: m.cancel_preparation()
        if not m.operation.acquire(blocking=False): raise ValueError('设备正在处理其他操作')
        try:
            with m.lock, self.lock:
                if m.active(): raise ValueError('已有控制会话')
                m.other_idle()
                if not self.snapshot()['available']: raise ValueError(self.snapshot()['reason'])
                command,env,payload = m._launch_options(dict(network_namespace='current'))
                command[-1] = 'teleop_control.kuavo_manual_client'
                self.session_id = secrets.token_hex(16); self.sequence = -1
                process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,text=True,bufsize=1,cwd=m.root,env=env)
                self.process = process; self.state = 'starting'; self.error = ''; self.busy = False
                self.telemetry = {}; self.received = 0.
                process.stdin.write(json.dumps(payload)+'\n'); process.stdin.flush()
                threading.Thread(target=self._read,args=(process,),daemon=True).start()
                threading.Thread(target=self._stderr,args=(process,),daemon=True).start()
                m.restart_available = False
                m.value['error'] = ''
        finally: m.operation.release()
        return m.snapshot()

    def input(self, body):
        with self.lock:
            if set(body) != {'session_id','sequence','command'}: raise ValueError('手动请求字段无效')
            if body.get('session_id') != self.session_id or not self.active():
                raise ValueError('手动会话已结束，请重新进入')
            sequence = body.get('sequence')
            if type(sequence) is not int or sequence <= self.sequence:
                raise ValueError('过期的手动指令已拒绝')
            command = body.get('command')
            if not isinstance(command,dict): raise ValueError('手动指令无效')
            action = command.get('action')
            if not isinstance(action,str): raise ValueError('手动指令类型无效')
            fields = {'heartbeat':{'action'},'stop':{'action'},'base':{'action','direction'},
                      'joint':{'action','joint','delta_deg'},'gripper':{'action','side','closed'}}
            if action not in fields or set(command)!=fields[action]: raise ValueError('手动指令字段无效')
            if action=='joint':
                delta = command['delta_deg']
                if (not isinstance(command['joint'],str) or command['joint'] not in {r['name'] for r in self.rows} or type(delta) not in (float,int)
                        or not math.isfinite(delta) or not 0 < abs(delta) <= 1): raise ValueError('关节点动范围为 ±1°')
            if action=='base' and command['direction'] not in ('forward','backward','left','right','turn_left','turn_right','stop'):
                raise ValueError('底盘方向无效')
            if action=='gripper' and (command['side'] not in ('left','right') or type(command['closed']) is not bool):
                raise ValueError('夹爪指令无效')
            if self.state != 'ready' and action != 'heartbeat': raise ValueError('手动控制尚未就绪')
            self.sequence = sequence
            self.process.stdin.write(json.dumps(command,allow_nan=False)+'\n'); self.process.stdin.flush()
        # Small reply; polling owns measured status. No optimistic position update.
        return dict(accepted=True,sequence=sequence)

    def stop(self, session_id):
        with self.lock:
            if session_id != self.session_id: raise ValueError('手动会话不匹配')
            if self.active():
                self.state = 'stopping'
                self.process.stdin.write('close\n'); self.process.stdin.flush()
        return self.manager.snapshot()

    def close(self):
        with self.lock:
            if self.active():
                self.state = 'stopping'
                try: self.process.stdin.write('close\n'); self.process.stdin.flush()
                except (OSError,ValueError): pass

    def _stderr(self, process):
        for line in process.stderr:
            with self.lock:
                if self.process is process: self.error = line.strip()[-700:]

    def _read(self, process):
        closed = False
        try:
            for line in process.stdout:
                if not line.startswith('KUAVO:'): continue
                value = json.loads(line[6:])
                with self.lock:
                    if self.process is not process: continue
                    event = value.get('event')
                    if event == 'ready': self.state = 'ready'
                    elif event == 'manual_feedback':
                        self.telemetry = value; self.received = time.monotonic(); self.busy = value.get('busy',False)
                    elif event == 'manual_ack' and not value.get('ok'):
                        self.error = value.get('error','手动指令被拒绝')
                    elif event == 'manual_ack' and value.get('ok'):
                        # A heartbeat must not erase the last rejected command.
                        pass
                    elif event == 'closed':
                        closed = True
                        self.state = 'idle' if value.get('cleanup_confirmed') else 'error'
                        self.error = value.get('error','')
                    elif value.get('error'): self.error = value['error']
        except (ValueError,OSError) as exc:
            with self.lock: self.error = str(exc)
        finally:
            process.wait()
            with self.lock:
                if not closed: self.state = 'error'; self.error = self.error or '手动连接已断开，请检查设备'
            # Read-only refresh of admission/settings; never automatically restart.
            self.manager._refresh_idle_parameters(self.manager.process)
