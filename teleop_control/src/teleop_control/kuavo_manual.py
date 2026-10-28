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
        self.checked_options = None
        self.closed_event = threading.Event()
        self.closed_event.set()
        self.initial_pose = None
        self.poses_path = manager.root/'teleop_control/runtime/kuavo/manual-poses.json'
        self.poses = []
        try:
            stored = json.loads(self.poses_path.read_text())
            if stored.get('schema') != 1: raise ValueError('姿态文件版本不支持')
            for pose in stored['poses']:
                if not isinstance(pose.get('id'),str) or not isinstance(pose.get('name'),str):
                    raise ValueError('姿态文件格式无效')
                self._validate_pose(pose['pose'])
            self.poses = stored['poses']
        except FileNotFoundError: pass
        except (ValueError,KeyError,TypeError,AttributeError,OSError) as exc:
            self.error = '读取已保存姿态失败：'+str(exc)
            self.poses = None  # Never overwrite an unreadable pose file.

    def _validate_pose(self, pose):
        if not isinstance(pose,dict) or set(pose) != {'joints','grippers'}:
            raise ValueError('姿态数据不完整')
        q, grips = pose['joints'],pose['grippers']
        if not isinstance(q,dict) or set(q) != {r['name'] for r in self.rows}:
            raise ValueError('姿态必须包含全部 20 个关节')
        for r in self.rows:
            v = q[r['name']]
            if type(v) not in (float,int) or not math.isfinite(v) or not r['min_deg']-1e-5 <= math.degrees(v) <= r['max_deg']+1e-5:
                raise ValueError('姿态关节越界：'+r['name'])
        if not isinstance(grips,dict) or set(grips) != {'left','right'} or any(
                type(v) not in (float,int) or not math.isfinite(v) or not 0 <= v <= 1 for v in grips.values()):
            raise ValueError('姿态夹爪反馈不完整')
        return copy.deepcopy(pose)

    def _measured_pose(self):
        if not self.active() or self.state != 'ready' or self.busy or time.monotonic()-self.received >= .5:
            raise ValueError('请等待手动控制就绪、动作完成并收到最新实测姿态')
        return self._validate_pose(dict(joints=self.telemetry.get('joints'),grippers={
            side:(self.telemetry.get('grippers',{}).get(side) or {}).get('closure_fraction') for side in ('left','right')}))

    def _receive_initial_pose(self, value):
        self.initial_pose = None
        if value.get('initial_pose_source') == 'robot_init_state_param':
            try: self.initial_pose = self._validate_pose(value.get('initial_pose'))
            except ValueError: pass

    def pose_operation(self, body):
        with self.lock:
            if self.poses is None: raise ValueError('姿态文件读取失败，请先核对本地文件')
            action = body.get('action')
            selected = None
            if action == 'save' and set(body) == {'action','name','session_id'}:
                if body['session_id'] != self.session_id: raise ValueError('手动会话已变化')
                name = body['name']
                if not isinstance(name,str) or not 1 <= len(name.strip()) <= 50: raise ValueError('姿态名称为 1～50 个字符')
                pose = dict(id=secrets.token_hex(16),name=name.strip(),pose=self._measured_pose())
                values = [*self.poses,pose]; selected = pose['id']
            elif action == 'delete' and set(body) == {'action','id'}:
                if self.busy: raise ValueError('请等待当前动作完成')
                if not any(p['id']==body['id'] for p in self.poses): raise ValueError('姿态不存在或不允许删除')
                values = [p for p in self.poses if p['id'] != body['id']]
            else: raise ValueError('姿态操作字段无效')
            self.poses_path.parent.mkdir(parents=True,exist_ok=True)
            temp = self.poses_path.with_suffix('.tmp')
            temp.write_text(json.dumps(dict(schema=1,poses=values),ensure_ascii=False,allow_nan=False,indent=2)+'\n')
            temp.replace(self.poses_path)
            self.poses = values
        result = self.manager.snapshot()
        result['manual']['selected_pose'] = selected
        return result

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
                joints=copy.deepcopy(self.rows),telemetry=copy.deepcopy(self.telemetry),fresh=fresh,
                poses=[dict(id=p['id'],name=p['name']) for p in self.poses or []],
                poses_available=self.poses is not None,initial_pose_available=self.initial_pose is not None,
                pose_supported=self.telemetry.get('pose_protocol')=='kuavo_manual_pose_v1',
                restoring=bool(self.telemetry.get('restoring')))

    def start(self, *, checked_options=None, generation=None):
        m = self.manager
        # Explicit manual entry relinquishes only our prepared XR session.
        with m.lock:
            if generation is not None and (generation != m.preparation_generation or m.check_cancel.is_set()):
                raise ValueError('准备已取消')
            if m.closing: raise ValueError('App 正在退出')
            if m.active() and not m.standby: raise ValueError('请先结束遥操作')
            if m.pending_operation: raise ValueError('请等待检查设备完成')
            if not self.snapshot()['available']: raise ValueError(self.snapshot()['reason'])
        if m.standby: m.cancel_preparation()
        if not m.operation.acquire(blocking=False): raise ValueError('设备正在处理其他操作')
        try:
            with m.lock, self.lock:
                if generation is not None and (generation != m.preparation_generation or m.check_cancel.is_set()):
                    raise ValueError('准备已取消')
                if m.active(): raise ValueError('已有控制会话')
                if not self.closed_event.is_set(): raise ValueError('上一手动会话退出尚未确认')
                m.other_idle()
                if not self.snapshot()['available']: raise ValueError(self.snapshot()['reason'])
                command,env,payload = m._launch_options(dict(network_namespace='current'))
                command[-1] = 'teleop_control.kuavo_manual_client'
                self.session_id = secrets.token_hex(16); self.sequence = -1
                process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,text=True,bufsize=1,cwd=m.root,env=env)
                self.process = process; self.state = 'starting'; self.error = ''; self.busy = False
                self.checked_options = copy.deepcopy(checked_options)
                self.closed_event.clear()
                self.telemetry = {}; self.received = 0.
                self.initial_pose = None
                process.stdin.write(json.dumps(payload)+'\n'); process.stdin.flush()
                threading.Thread(target=self._read,args=(process,),daemon=True).start()
                threading.Thread(target=self._stderr,args=(process,),daemon=True).start()
                m.restart_available = False
                m.value['error'] = ''
        finally: m.operation.release()
        return m.snapshot()

    def wait_ready(self, generation, timeout=40.):
        end = time.monotonic()+timeout
        process = self.process
        try:
            while time.monotonic() < end:
                with self.manager.lock:
                    if generation != self.manager.preparation_generation or self.manager.check_cancel.is_set():
                        raise ValueError('准备已取消')
                    with self.lock:
                        if self.process is not process: raise ValueError('手动准备已被替换')
                        if not self.active() or self.state in ('error','stopping'):
                            raise ValueError(self.error or '手动控制连接未就绪')
                        if self.state=='ready' and self.received and time.monotonic()-self.received<.5:
                            self.manager.value['state']='ready'
                            return
                        # Keep startup alive only while this explicit Check request is pending.
                        self.process.stdin.write('{"action":"heartbeat"}\n');self.process.stdin.flush()
                time.sleep(.1)
            raise TimeoutError('手动控制准备超时，请检查设备')
        except Exception:
            with self.lock:
                if self.process is process:self.close()
            raise

    def finish(self):
        self.close()
        if not self.closed_event.wait(45): raise RuntimeError('手动控制退出尚未确认，请等待')
        with self.lock:
            if self.state != 'idle': raise RuntimeError(self.error or '手动控制保持与退出未确认')

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
                      'restore':{'action','pose_id'},
                      'joint':{'action','joint','delta_deg'},'gripper':{'action','side','closed'}}
            if action not in fields or set(command)!=fields[action]: raise ValueError('手动指令字段无效')
            if action=='joint':
                delta = command['delta_deg']
                if (not isinstance(command['joint'],str) or command['joint'] not in {r['name'] for r in self.rows} or type(delta) not in (float,int)
                        or not math.isfinite(delta) or not 0 < abs(delta) <= 10): raise ValueError('关节点动范围为 ±10°')
            if action=='base' and command['direction'] not in ('forward','backward','left','right','turn_left','turn_right','stop'):
                raise ValueError('底盘方向无效')
            if action=='gripper' and (command['side'] not in ('left','right') or type(command['closed']) is not bool):
                raise ValueError('夹爪指令无效')
            if action=='restore':
                self._measured_pose()
                if self.telemetry.get('pose_protocol') != 'kuavo_manual_pose_v1':
                    raise ValueError('机器人通信服务需要更新姿态复位功能')
                pose = self.initial_pose if command['pose_id']=='initial' else next((p['pose'] for p in self.poses or [] if p['id']==command['pose_id']),None)
                if pose is None: raise ValueError('姿态不存在，请重新选择')
                command = dict(action='restore',pose=self._validate_pose(pose))
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
                        self._receive_initial_pose(value)
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
                was_checked = self.checked_options is not None
                self.checked_options = None
            if was_checked:
                with self.manager.lock:
                    self.manager.value['state'] = 'idle' if self.state=='idle' else 'error'
                    if self.error:self.manager.value['error']=self.error
            # Read-only refresh of admission/settings; never automatically restart.
            try:self.manager._refresh_idle_parameters(self.manager.process)
            finally:self.closed_event.set()
