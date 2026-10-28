"""KUAVO workspace. Uses a separate Python process and the original PC policy."""
from __future__ import annotations
from collections import deque
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Annotated, Literal
from .runtime_paths import APP_ROOT, resolve_robot_python, legacy_robot_python, resolve_xr_service
from .gdk_env import add_xr_runtime_env
from .kuavo_preview import KuavoModel
from .kuavo_settings import LocalSettings, GripperSettings, ConnectionSettings, MotionSettings
from .kuavo_motion_controls import motion_controls
from .kuavo_readiness import hardware_readiness, service_block_detail
from .kuavo_collection import KuavoCollection, CollectionSettings

LIMITATIONS = [
    'A 开始采集，B 保存；按当前所选本地／云端平台上传完整回合。关节使用原始约 10 Hz 诊断记录，图像按 PC 接收时间软件对齐。',
    '检查设备自动连接本体头部及左右 USB 相机，USB 使用 640×480/30 fps 档位；图像与关节并非硬件同步。',
    '检查连接后通过网络服务订阅实测关节姿态，需完成一次网络服务安装；夹爪开合使用准备及遥操作会话的实测电机反馈，断流标记过期。',
    '新版服务支持左右臂及夹爪独立参数，预览通过后显式保存并回读；旧版仍保留本地参数。',
    '单臂模式屏蔽另一侧 Grip / Trigger；所有模式的 Y 均执行双臂及夹爪回位，再按 Y 取消。',
    '默认仅双臂；双臂＋底盘模式在开始后、摇杆回中且底盘状态通过检查时启用底盘；物理 X 键不参与控制。',
]

class PreparationCancelled(RuntimeError):
    pass

class Options(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    network_namespace: Literal['kuavons','current'] = 'current'
    task: str = Field(default='KUAVO 遥操作', min_length=1, max_length=120)
    confirm_motion: bool = False
    accept_unverified_gripper_bus: bool = True
    motor_ids_confirmed: bool = False
    prepare_controller: bool = False
    prepare_grippers: bool = False
    prepare_session: bool = False
    prepare_manual: bool = False
    require_prepared: bool = False
    restart_after_stop: bool = False
    arm_mode: Literal['dual','left','right','dual_base'] = 'dual'
    position_scale: float = Field(default=1., ge=.1, le=1., allow_inf_nan=False)
    preparation_action: Literal['start','initialize','grippers_inspect','grippers_prepare'] = 'start'

    @model_validator(mode='after')
    def validate_preparation_permission(self):
        if self.restart_after_stop and not (self.prepare_session and self.require_prepared):
            raise ValueError('再次开始需要完整准备并核验就绪')
        if (self.prepare_session or self.prepare_manual) and not (self.prepare_grippers and self.prepare_controller):
            raise ValueError('遥操作准备需要先完成主控与夹爪检查')
        if self.prepare_grippers and not (self.prepare_controller and self.accept_unverified_gripper_bus):
            raise ValueError('夹爪准备需要主控准备入口与当前共享总线配置许可')
        if self.prepare_controller and not (self.confirm_motion and self.motor_ids_confirmed):
            raise ValueError('检查并准备主控需要明确的启动初始化与电机配置许可')
        return self

class StopRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str


class SessionSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['read', 'preview', 'apply', 'reset'] = 'read'
    changes: dict[str, Annotated[float, Field(allow_inf_nan=False, strict=True)]] = Field(default_factory=dict)
    expected_revision: int | None = Field(default=None, ge=0)
    expected_service_instance_id: str | None = Field(default=None, pattern='^[0-9a-f]{32}$')
    expected_installation_sha256: str | None = Field(default=None, pattern='^[0-9a-f]{64}$')
    confirm: bool = False

    @model_validator(mode='after')
    def validate_action(self):
        if self.action != 'read' and any(getattr(self, key) is None for key in (
                'expected_revision', 'expected_service_instance_id', 'expected_installation_sha256')):
            raise ValueError('请先读取机器人当前参数和版本')
        if self.action in ('preview', 'apply') and not self.changes:
            raise ValueError('请提交修改过的参数')
        if self.action in ('read', 'reset') and self.changes:
            raise ValueError('此操作不接受参数改值')
        if self.action in ('apply', 'reset') and not self.confirm:
            raise ValueError('请明确确认参数操作')
        return self

class KuavoManager:
    def __init__(self, controller, *, other_idle=lambda: None, root=APP_ROOT):
        self.controller, self.other_idle, self.root = controller, other_idle, Path(root)
        from .kuavo_cameras import KuavoCameras
        self.cameras = KuavoCameras(self.root)
        self.collection=KuavoCollection(self,self.root/'teleop_control/runtime/kuavo/collection-episodes')
        self.cameras.recording_guard=self.collection.active
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.process = None
        self.monitor_process = None
        self.monitor_value = {}
        self.monitor_received = 0.
        self.session_id = None
        self.pending_operation = False
        self.last_status = 0.
        self.grippers_received = 0.
        self.session_started_at = None
        self.session_reached_running = False
        self.restart_available = False
        self.standby=False
        self.prepared_mode=None
        self.activation_requested=False
        self.session_stop_requested=False
        self.closing=False
        self.prepared_event=threading.Event()
        self.check_cancel=threading.Event()
        self.check_process=None
        self.preparation_options=None
        self.preparation_generation=0
        self.model = KuavoModel()
        self.settings = LocalSettings(self.root)
        self.feedback_received = 0.
        self.feedback_stamp = None
        self.feedback_age = None
        self.value = dict(state='idle', error='', service={}, preparation={}, session={},
                          log_directory='', pico_sdk_available=False, limitations=LIMITATIONS,
                          reference=self.model.reference, segments=self.model.segments(),telemetry={},pico_live=False,
                          vr_checked=False,vr_ready=False,pico_ready=False,vr_detail='尚未检查头显输入',pico_detail='尚未检查 PICO')
        self.logs = deque(maxlen=35)
        from .kuavo_manual import KuavoManual
        self.manual = KuavoManual(self)

    def active(self):
        with self.lock:
            return self.manual.active() or self.pending_operation or self.process is not None and self.process.poll() is None

    def snapshot(self):
        with self.lock:
            value = copy.deepcopy(self.value)
            if self.process is not None and self.process.poll() is None and time.monotonic()-self.last_status>5:
                value.update(state='unknown',error='客户端状态未更新；请结束本次会话并检查日志')
            value.update(session_id=self.session_id, active=self.active(), logs=list(self.logs),preparation_generation=self.preparation_generation)
            value['camera_preview'] = self.cameras.snapshot()
            value['collection']=self.collection.snapshot()
            form=value.get('session_settings')
            if form:
                from .kuavo_session_settings import lock_message
                code=form.get('locked_reason_code',form.get('locked_reason'))
                if code:
                    form['locked_reason_code']=code
                    form['locked_reason']=lock_message(code)
                if (self.standby or self.manual.checked_options is not None) and self._prestart_editable():
                    form['editable']=True
                    if code:form['locked_reason']=''

            value['prestart_editable']=self._prestart_editable()
            age = None if self.feedback_age is None else self.feedback_age+time.monotonic()-self.feedback_received
            value['pose_live'] = bool(value['active'] and age is not None and age<1.)
            value['pose_available'] = self.feedback_stamp is not None
            monitored = self.monitor_value
            sample = monitored.get('telemetry') or {}
            monitor_age = sample.get('age_s')
            monitor_live = (self.monitor_process is not None and self.monitor_process.poll() is None
                            and monitored.get('pose_status') == 'fresh' and monitor_age is not None
                            and monitor_age+time.monotonic()-self.monitor_received<.5)
            value['pose_detail'] = monitored.get('pose_detail', '检查设备后连接机器人只读网络服务')
            # ROS body/arm telemetry must not replace independent motor feedback.
            grippers = copy.deepcopy((value.get('telemetry') or {}).get('grippers') or {})
            for motor in grippers.values():
                motor['age_s'] += max(0., time.monotonic()-self.grippers_received)
                motor['live'] = bool(value['active'] and motor['age_s'] < 1.)
            if monitor_live or (not value['pose_live'] and sample.get('joints')):
                value.update(telemetry=copy.deepcopy(sample), segments=self.model.segments(sample['joints']),
                             pose_available=True, pose_live=bool(monitor_live))
            value.setdefault('telemetry', {})['grippers'] = grippers
            value['pico_live'] = bool(value.get('pico_live') and value['active'] and time.monotonic()-self.last_status<1.)
            value['control_ready']=self._prepared_for_check(value) and not self.pending_operation
            elapsed = 0 if self.session_started_at is None else max(0,int(time.monotonic()-self.session_started_at))
            value['execution_notice'] = ''
            if value['state']=='ready':
                value['execution_notice']='' if value['control_ready'] else '请松开握把、扳机和按键，摇杆回中，等待新鲜手柄输入。'
            elif value['state']=='preparing':
                value['execution_notice']=f'正在完成遥操作准备，已等待 {elapsed} 秒；准备完成后才能开始。'
            elif value['state']=='starting':
                step = {'connecting':'正在连接并核验机器人', 'compute_preparing':'机器人正在准备计算模块',
                        'verifying_ready':'正在核验机器人遥操作就绪回执'}.get(value.get('execution_stage'), '正在准备遥操作')
                value['execution_notice'] = f'{step}，已等待 {elapsed} 秒。尚未进入遥操作，手柄暂不生效；准备阶段超时上限约 90 秒。'
            elif value['state']=='running':
                value['execution_notice'] = ('遥操作已就绪。先松开握把和扳机，再按住选中侧的握把键（Grip）移动手柄控制手臂；食指扳机控制夹爪。'
                    if value['pico_live'] else '遥操作会话已就绪，但当前未收到新鲜手柄输入，请检查头显发送与追踪状态。')
            elif value['state']=='error' and self.session_started_at is not None and not self.session_reached_running:
                value['execution_notice'] = '本次会话未进入遥操作运行阶段。请查看具体退出原因；不能据此认定手柄控制已生效。'
            value['gripper_settings'] = self.settings.snapshot(value.get('service') if value['state'] not in ('error','unknown') else {})
            value['connection'] = self.settings.connection_snapshot()
            value['motion_settings'] = self.settings.motion_snapshot()
            protocol = ((value.get('service') or {}).get('service', {}).get('session_contract') or {}).get('protocol')
            value['parameter_mode'] = 'service' if protocol in ('kuavo_robot_session_v2', 'kuavo_robot_session_v3') else 'local'
            value['motion_controls'] = motion_controls(value.get('session_settings'))
            contract = (value.get('service') or {}).get('service') or {}
            base = 'base_vendor_vr_x_v1' in (contract.get('session_contract') or {}).get('capabilities', []) or contract.get('profile', {}).get('base_mode') == 'vendor_vr_x'
            value['base_supported'] = base
            value['service_connected']=bool((value.get('service') or {}).get('management_ready'))
            value['service_block_detail']=service_block_detail(contract)
            ready, detail = hardware_readiness(value.get('preparation'))
            value.update(hardware_ready=ready, hardware_detail=detail)
            value['start_blocked_reason'] = '' if ready or not (value.get('service') or {}).get('management_ready') else detail
            if value.get('gripper_preparation_required') and not value.get('gripper_preparation_ready'):
                value['start_blocked_reason'] = '左右夹爪准备尚未通过，请检查设备并查看具体失败原因'
            if value['service_block_detail']:
                value['start_blocked_reason']=value['service_block_detail']
            value['can_restart']=bool(self.restart_available and not value['active']
                and value['state']=='idle' and not self.closing and not self.collection.active()
                and not value['start_blocked_reason'] and contract.get('ready_to_start') is True
                and contract.get('owner_present') is False)
            value['manual'] = self.manual.snapshot()
            if value['manual']['active']:
                checked=self.manual.checked_options is not None
                manual_ready=checked and value['manual']['state']=='ready' and value['manual']['fresh'] and not value['manual']['busy']
                value.update(control_ready=manual_ready,can_restart=False,prestart_editable=self._prestart_editable())
                if value['manual']['fresh']:
                    feedback = value['manual']['telemetry']
                    value.update(telemetry=feedback,pose_live=True,pose_available=True,
                                 segments=self.model.segments(feedback['joints']))
            return value

    def _prestart_editable(self):
        if self.manual.active(): return self.manual.checked_options is not None and not self.manual.busy
        live=self.process is not None and self.process.poll() is None
        return not (live and (self.activation_requested or not self.standby))

    def _prepared_for_check(self, value):
        session=value.get('session') or {}
        stamp=session.get('robot_report_monotonic_ns')
        fresh=type(stamp) is int and 0<=time.monotonic_ns()-stamp<1_000_000_000
        return bool(self.standby and value['state']=='ready' and value.get('pico_live') and fresh
            and all(session.get(side+'_gripper')=='READY' for side in ('left','right'))
            and session.get('arm_state') not in ('unknown','fault','paused','paused_base')
            and value.get('input_neutral') and not self.activation_requested and not self.session_stop_requested
            and not self.check_cancel.is_set())

    def _request_preparation_cancel(self, *, preserve=False):
        with self.lock:
            if not self._prestart_editable():raise RuntimeError('请先结束正在运行的遥操作')
            self.restart_available=False
            self.preparation_generation+=1
            self.check_cancel.set();self.prepared_event.set()
            resume=(copy.deepcopy(self.preparation_options),self.preparation_generation) if preserve else None
            if not preserve:self.preparation_options=None
            check=self.check_process
            if check is not None and check.poll() is None:
                # Only the owned PC requester ends. Robot requests retain their
                # persisted IDs; subsequent checks query them instead of retrying.
                try:check.kill()
                except ProcessLookupError:pass
            if self.standby and self.process is not None and self.process.poll() is None:
                self.stop(self.session_id)
            if self.manual.active():self.manual.close()
            return resume

    def _finish_preparation_cancel(self):
        # Caller owns operation; never wait for the stdout reader under lock.
        if not self.manual.closed_event.is_set():self.manual.finish()
        process=self.process
        if process is not None and process.poll() is None:
            process.wait(timeout=45)
        with self.lock:
            if self.process is process:
                self.process=None
            self.standby=False
            self.value.update(state='idle',pico_live=False,input_enabled=False)

    def cancel_preparation(self):
        self._request_preparation_cancel()
        if not self.operation.acquire(timeout=60):raise RuntimeError('准备退出仍未确认，请等待当前请求结束')
        try:
            self._finish_preparation_cancel()
            self._stop_monitor()
            return self.snapshot()
        finally:self.operation.release()

    def _resume_preparation(self, resume):
        if not resume or not resume[0]:return
        options,generation=resume
        def run():
            try:self.inspect(options,_generation=generation)
            except Exception as exc:
                with self.lock:
                    if self.preparation_generation==generation:
                        self.value.update(state='error',error=str(exc))
        threading.Thread(target=run,daemon=True).start()

    def session_settings(self, body):
        with self.lock:
            if not self._prestart_editable():raise RuntimeError('请先结束正在运行的遥操作')
            if body.action=='read' and self.active() and self.value.get('session_settings'):
                result=self.snapshot()
                result['session_settings']['editable']=True
                result['session_settings']['locked_reason']=''
                return result
            interrupted=self.active() and body.action!='read'
            resume=(self._request_preparation_cancel(preserve=True) if interrupted else
                    (copy.deepcopy(self.preparation_options),self.preparation_generation)
                    if body.action!='read' and self.preparation_options and not self.activation_requested else None)
        if not self.operation.acquire(timeout=60 if interrupted else 0):
            raise RuntimeError('已有操作正在处理，请等待结果')
        claimed = False
        success=False
        try:
            if interrupted:self._finish_preparation_cancel()
            with self.lock:
                if self.active(): raise RuntimeError('请先结束 KUAVO 会话或等待检查完成')
                self.other_idle()
                self.pending_operation = True
                claimed = True
                self.value.update(settings_preview=None, error='')
            command, env, payload = self._launch_options({'network_namespace':'current', 'settings_request':body.model_dump()})
            result = subprocess.run([*command, 'settings'], input=json.dumps(payload)+'\n',
                text=True, capture_output=True, cwd=self.root, env=env, timeout=30)
            received = False
            for line in result.stdout.splitlines():
                if line.startswith('KUAVO:'):
                    self._merge(json.loads(line[6:])); received = True
            if result.returncode or not received:
                raise RuntimeError(self.value.get('error') or result.stderr[-1200:] or '参数操作未返回结果，请重新读取')
            with self.lock:
                self.pending_operation = False
                self.value['state'] = 'idle'
                success=True
            return self.snapshot()
        except Exception as exc:
            if claimed:
                with self.lock:
                    self.value.update(session_settings=None, settings_preview=None, error=str(exc))
            raise
        finally:
            if claimed:
                with self.lock: self.pending_operation = False
            self.operation.release()
            if success:self._resume_preparation(resume)

    def save_settings(self, body):
        with self.lock:
            if not self._prestart_editable():raise RuntimeError('请先结束正在运行的遥操作')
            interrupted=self.active()
            resume=self._request_preparation_cancel(preserve=not isinstance(body,ConnectionSettings)) if interrupted else None
        if not self.operation.acquire(timeout=60 if interrupted else 0):raise RuntimeError('已有操作正在处理')
        success=False
        try:
            if interrupted:self._finish_preparation_cancel()
            result=self._save_settings_idle(body)
            success=True
            return result
        finally:
            self.operation.release()
            if success:self._resume_preparation(resume)

    def _save_settings_idle(self, body):
        with self.lock:
            if self.active(): raise RuntimeError('请先结束 KUAVO 会话或等待检查完成')
            self.other_idle()
            if isinstance(body, MotionSettings):
                self.settings.save_motion(body)
            elif isinstance(body, ConnectionSettings):
                self.restart_available=False
                self._stop_monitor()
                self.settings.save_connection(body)
                self.value.update(service={},session_settings=None,settings_preview=None, preparation={}, pico_sdk_available=False, state='idle', error='')
            else:
                document = self.settings.build(body.model_dump())
                # Run the original validator in an isolated interpreter. It has
                # no SDK/network side effects and must not import vendor teleop
                # into the backend shared with G2.
                result = subprocess.run([sys.executable, '-m', 'teleop_control.kuavo_worker', 'validate-grippers'],
                    input=json.dumps(document)+'\n', text=True, capture_output=True,
                    cwd=APP_ROOT, env={**os.environ,'PYTHONPATH':str(APP_ROOT/'teleop_control/src')}, timeout=15)
                if result.returncode:
                    raise ValueError('夹爪参数未保存：'+result.stdout.removeprefix('KUAVO:').strip()[-800:])
                self.settings.save_grippers(document)
            return self.snapshot()

    def _launch_options(self, options):
        if getattr(self.controller, 'hardware_enabled', True) is False:
            raise ValueError('模拟模式不连接 KUAVO 实机')
        config = self.root/'teleop_control/runtime/kuavo/connection.json'
        if not config.is_file():
            raise ValueError('缺少 KUAVO 连接配置：请导入 connection.json 和 token，参见 KUAVO 接入说明')
        python = (os.environ.get('KUAVO_PYTHON') or os.environ.get('TELEOP_GDK_PYTHON')
                  or resolve_robot_python() or legacy_robot_python() or sys.executable)
        env = add_xr_runtime_env(os.environ.copy(), resolve_xr_service())
        env['PYTHONPATH'] = str(self.root/'teleop_control/src')
        command = [python, '-u', '-m', 'teleop_control.kuavo_worker']
        return command, env, {**options, 'config':str(config),
                             'expected_gripper_sha256':None if self.settings.error else self.settings.expected_gripper_hash()}

    def _merge(self, data):
        data=dict(data)
        events=data.pop('collection_events',[])
        overflow=data.pop('collection_overflow',False)
        self.collection.accept(events,overflow)
        if not data:return
        if (data.get('session') or {}).get('phase')=='failed':
            self.collection.accept([dict(kind='fault',error='机器人遥操作会话异常结束')])
        with self.lock:
            telemetry=data.get('telemetry') or {}
            if 'grippers' in telemetry:self.grippers_received=time.monotonic()
            if telemetry.get('timestamp_ns') is not None:
                if telemetry['timestamp_ns']!=self.feedback_stamp:
                    self.value['segments']=self.model.segments(telemetry.get('joints',{}))
                    self.feedback_stamp=telemetry['timestamp_ns']
                self.feedback_age=telemetry.get('age_s')
                self.feedback_received=time.monotonic()
            self.value.update(data)
            self.last_status = time.monotonic()
            if 'session' in data:
                phase = data['session'].get('phase','starting')
                if phase=='running':self.session_reached_running=True
                self.value['state'] = {'finished':'idle','failed':'error'}.get(phase,phase)
                if self.standby:
                    if phase=='starting':self.value['state']='preparing'
                    elif phase=='running':
                        self.value['state']='running' if data.get('input_enabled') else ('starting' if self.activation_requested else 'ready')
                        if data.get('input_enabled'):self.standby=False
                if self.session_stop_requested and phase not in ('finished','failed'):
                    self.value['state']='stopping'
                self.prepared_event.set()
                report = data['session'].get('report') or {}
                self.value['error'] = data['session'].get('error') or '; '.join(report.get('errors',[]))
                refusal = self.value.get('robot_refusal')
                if phase=='failed' and refusal and refusal not in self.value['error']:
                    detail = ('遥操作尚未就绪时，输入连接关闭或收到退出请求' if
                        not self.session_reached_running and 'input connection closed or operator exit' in refusal else refusal)
                    self.value['error'] = detail + ('；'+self.value['error'] if self.value['error'] else '')

    def inspect(self, options, *, prepare=False, _generation=None, connect_cameras=False):
        if not prepare and options.get('prepare_manual'):
            # Manufacturer/gripper checks first; acquire exactly one manual owner.
            result=self._inspect_devices({**options,'prepare_session':False},
                _generation=_generation,connect_cameras=connect_cameras)
            generation=result['preparation_generation']
            try:
                self.manual.start(checked_options=options,generation=generation)
                self.manual.wait_ready(generation)
                return self.snapshot()
            except Exception as exc:
                with self.lock:
                    if generation==self.preparation_generation:
                        self.value.update(state='idle' if self.check_cancel.is_set() else 'error',error=str(exc))
                raise
        return self._inspect_devices(options,prepare=prepare,_generation=_generation,connect_cameras=connect_cameras)

    def _inspect_devices(self, options, *, prepare=False, _generation=None, connect_cameras=False):
        if options.get('prepare_manual') and not (options.get('prepare_grippers') is True and options.get('prepare_controller') is True):
            raise ValueError('手动准备需要先完成主控与夹爪检查')
        if options.get('prepare_session') and not (options.get('prepare_grippers') is True and options.get('prepare_controller') is True):
            raise ValueError('遥操作准备需要先完成主控与夹爪检查')
        if options.get('prepare_grippers') and not (options.get('prepare_controller') is True and options.get('accept_unverified_gripper_bus') is True):
            raise ValueError('夹爪准备需要主控准备入口与当前共享总线配置许可')
        if options.get('prepare_controller') and not (options.get('confirm_motion') is True and options.get('motor_ids_confirmed') is True):
            raise ValueError('检查并准备主控需要明确的启动初始化与电机配置许可')
        if not self.operation.acquire(timeout=60 if _generation is not None else 0):
            raise RuntimeError('已有操作正在处理，请等待结果')
        claimed = False
        try:
            with self.lock:
                if _generation is not None and _generation!=self.preparation_generation:
                    raise PreparationCancelled('准备已取消')
                if self.active():raise RuntimeError('请先结束 KUAVO 会话')
                self.restart_available=False
                if _generation is None:self.preparation_generation+=1
                self.other_idle()
                if options.get('prepare_grippers') or (prepare and options.get('preparation_action')=='grippers_prepare'):
                    self.settings.expected_gripper_hash()
                if options.get('prepare_grippers'):
                    self.value.update(gripper_preparation_required=True,gripper_preparation_ready=False,gripper_preparation={})
                self.pending_operation = True
                self.check_cancel.clear()
                if options.get('prepare_session') or options.get('prepare_manual'):self.preparation_options=copy.deepcopy(options)
                claimed = True
                self.value.update(state='checking',error='')
                if not prepare:self.value.update(preparation_progress='正在检查连接与头显',settings_preview=None,pico_sdk_available=False,vr_checked=False,vr_ready=False,pico_ready=False)
            if connect_cameras and not prepare and getattr(self.controller,'hardware_enabled',True) is not False:
                self.cameras.request_reconnect()
            command, env, payload = self._launch_options(options)
            if not prepare and options.get('prepare_controller'):
                self._inspect_preparing(command, env, payload)
                result = subprocess.CompletedProcess(command, 0, 'KUAVO:{}\n', '')
            else:
                result = subprocess.run([*command, 'prepare' if prepare else 'check'], input=json.dumps(payload)+'\n',
                    text=True, capture_output=True, cwd=self.root, env=env, timeout=240)
            received=False
            for line in result.stdout.splitlines():
                if line.startswith('KUAVO:'):
                    self._merge(json.loads(line[6:]))
                    received=True
            if result.returncode:
                raise RuntimeError(self.value.get('error') or result.stderr[-1200:] or '客户端检查失败')
            if not received:raise RuntimeError('客户端没有返回检查结果')
            if self.check_cancel.is_set():raise PreparationCancelled('准备已取消')
            self.value['state']='idle'
            if not prepare:
                self._start_monitor(options)
            if not prepare and options.get('prepare_session'):
                self.start(options,_standby=True)
                self._wait_prepared()
            self.pending_operation=False
            return self.snapshot()
        except Exception as exc:
            if claimed:
                with self.lock:
                    if options.get('prepare_session') and self.standby and self.process is not None and self.process.poll() is None:
                        self.stop(self.session_id)
                    self.value.update(state='idle' if self.check_cancel.is_set() else 'error',error='' if self.check_cancel.is_set() else str(exc))
            raise
        finally:
            if claimed:
                with self.lock:self.pending_operation=False
            self.operation.release()

    def _inspect_preparing(self, command, env, payload):
        """Stream preparation progress while the request owns the operation lock."""
        process = subprocess.Popen([*command, 'check'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, cwd=self.root, env=env)
        with self.lock:
            self.check_process=process
            if self.check_cancel.is_set():process.kill()
        received, errors = [], []
        def read():
            try:
                for line in process.stdout:
                    if line.startswith('KUAVO:'):
                        self._merge(json.loads(line[6:]));received.append(True)
            except (ValueError, OSError) as exc:errors.append(str(exc))
        def diagnostics():
            for line in process.stderr:
                with self.lock:self.logs.append(line.rstrip()[-1200:])
        readers = [threading.Thread(target=fn, daemon=True) for fn in (read, diagnostics)]
        try:
            for reader in readers:reader.start()
            process.stdin.write(json.dumps(payload)+'\n');process.stdin.flush()
            code = process.wait(timeout=900)
            for reader in readers:reader.join(timeout=2)
            if self.check_cancel.is_set():raise PreparationCancelled('准备已取消')
            if code or errors or not received:
                raise RuntimeError(self.value.get('error') or '; '.join(errors) or '主控准备未返回完整结果，请重新检查原请求')
        except subprocess.TimeoutExpired:
            raise RuntimeError('检查等待超时，准备结果未确认；再次检查将查询已保存的原请求，不自动重发') from None
        finally:
            # Only this PC check worker is owned here. Robot control services
            # continue independently; any in-flight request retains its receipt.
            if process.poll() is None:
                process.kill();process.wait(timeout=5)
            for reader in readers:reader.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):stream.close()
            with self.lock:
                if self.check_process is process:self.check_process=None

    def _wait_prepared(self, timeout=110.):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if self.check_cancel.is_set():raise PreparationCancelled('准备已取消')
            current=self.snapshot()
            if current['control_ready'] or (self.pending_operation and self._prepared_for_check(current)):return
            if current['state'] in ('error','idle','unknown','stopping'):
                raise RuntimeError(current.get('error') or '遥操作准备已退出，请重新检查设备')
            self.prepared_event.wait(min(.25,max(0,deadline-time.monotonic())))
            self.prepared_event.clear()
        self.stop(self.session_id)
        raise RuntimeError('遥操作准备超时；未放行手柄输入，请查看日志后重新检查')

    def start(self, options, *, _standby=False):
        if options.get('confirm_motion') is not True:
            raise ValueError('请明确确认启动遥操作')
        if not _standby and self.manual.active():
            with self.lock:
                checked=copy.deepcopy(self.manual.checked_options)
                if not checked or not self.snapshot()['control_ready']:
                    raise RuntimeError('请等待手动控制动作完成并检查设备')
                generation=self.preparation_generation
            # No XR process can start until the manual worker confirms cleanup.
            self.manual.finish()
            if generation!=self.preparation_generation or self.check_cancel.is_set():
                raise PreparationCancelled('遥操作切换已取消')
            prepared={**checked,**options,'prepare_manual':False,'prepare_session':True,
                'prepare_controller':True,'prepare_grippers':True,'motor_ids_confirmed':True,
                'require_prepared':True,'restart_after_stop':False}
            self.inspect(prepared,_generation=generation)
            return self.start(prepared)
        if not _standby and options.get('restart_after_stop'):
            options=Options(**options).model_dump()
            with self.lock:
                if not self.snapshot()['can_restart']:
                    raise ValueError('上一会话尚未正常结束或设备状态已改变，请检查设备')
                # Consume this explicit click once. Stop never invokes this
                # branch; cancellation invalidates the generation before any
                # activation of the newly prepared session.
                self.restart_available=False
                self.preparation_generation+=1
                generation=self.preparation_generation
            self.inspect(options,connect_cameras=False,_generation=generation)
            with self.lock:
                if generation!=self.preparation_generation or self.check_cancel.is_set():
                    raise PreparationCancelled('再次开始已取消')
                return self.start({**options,'restart_after_stop':False})
        with self.lock:
            if not _standby and self.standby:
                mode=options.get('arm_mode','dual')
                if mode not in ('dual','left','right','dual_base'):raise ValueError('无效遥操作范围')
                if mode=='dual_base' and not self.snapshot()['base_supported']:
                    raise ValueError('当前机器人服务不支持双臂＋底盘')
                if not self.snapshot()['control_ready']:
                    raise ValueError('遥操作尚未准备完成，或手柄输入未释放/已过期')
                self.collection.arm({**options,'parameters':copy.deepcopy(self.value.get('session_settings'))})
                try:
                    self.process.stdin.write(json.dumps({'action':'activate','arm_mode':mode})+'\n');self.process.stdin.flush()
                except OSError:
                    self.collection.disarm();raise
                self.activation_requested=True
                self.value['state']='starting'
                return self.snapshot()
            if options.get('require_prepared') and not _standby:
                raise ValueError('请先检查设备，等待遥操作准备完成后再开始')
            if self.manual.active() or self.process is not None and self.process.poll() is None or (self.pending_operation and not _standby):
                raise RuntimeError('上一会话仍在运行或退出')
            self.other_idle()
            snapshot = self.snapshot()
            if snapshot['start_blocked_reason']:
                raise ValueError(snapshot['start_blocked_reason'])
            if options.get('arm_mode') == 'dual_base' and not snapshot['base_supported']:
                raise ValueError('当前机器人服务不支持双臂＋底盘，请重新检查设备或选择双臂')
            if snapshot['parameter_mode'] == 'service':
                current = snapshot.get('session_settings')
                if not current or not current.get('editable'):
                    raise ValueError('请重新读取机器人参数，确认服务空闲后开始')
                options = {**options, 'position_scale':1., 'expected_settings':{
                    key:current[key] for key in ('revision','settings_sha256','service_instance_id','installation_sha256')}}
            else:
                self.settings.require_supported_motion()
                self.settings.expected_gripper_hash()
                options = {**options, 'position_scale': self.settings.motion.position_scale}
            command, env, payload = self._launch_options(options)
            self.standby=_standby
            self.restart_available=False
            self.prepared_mode=options.get('arm_mode','dual')
            self.activation_requested=False
            self.session_stop_requested=False
            self.prepared_event.clear()
            payload['standby']=_standby
            self.session_id = uuid.uuid4().hex
            payload['output'] = str(self.root/'teleop_control/runtime/kuavo/episodes'/
                                    (time.strftime('%Y%m%d_%H%M%S')+'_'+self.session_id[:8]))
            self.logs.clear()
            self.value.update(state='preparing' if _standby else 'starting',error='',session={},log_directory='',execution_stage='connecting',robot_refusal='',input_enabled=not _standby,input_neutral=False)
            self.session_started_at=time.monotonic();self.session_reached_running=False
            self.value.update(telemetry={},pico_live=False,segments=self.model.segments())
            self.feedback_stamp=self.feedback_age=None
            self.last_status=time.monotonic()
            if not _standby:self.collection.arm({**options,'parameters':copy.deepcopy(self.value.get('session_settings'))})
            try:
                process = subprocess.Popen([*command,'execute'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, bufsize=1, cwd=self.root, env=env)
            except OSError as exc:
                self.collection.disarm()
                self.value.update(state='error',error=str(exc))
                raise
            self.process=process
            process.stdin.write(json.dumps(payload)+'\n');process.stdin.flush()
            threading.Thread(target=self._read, args=(process,), daemon=True).start()
            threading.Thread(target=self._stderr, args=(process,), daemon=True).start()
            return self.snapshot()

    def _stderr(self, process):
        for line in process.stderr:
            with self.lock:
                if self.process is process:self.logs.append(line.rstrip()[-1200:])

    def _read(self, process):
        try:
            for line in process.stdout:
                if line.startswith('KUAVO:'):
                    with self.lock:
                        if self.process is process:self._merge(json.loads(line[6:]))
        finally:
            refresh=False
            code=process.wait()
            process.stdin.close()
            with self.lock:
                if self.process is process:
                    was_running=self.activation_requested or not self.standby
                    self.restart_available=bool(was_running and self.session_reached_running and code in (0,130))
                    if was_running:self.preparation_options=None
                    refresh=was_running and code in (0,130) and bool(self.value.get('session_settings'))
                    self.standby=False
                    self.collection.disarm()
                    self.activation_requested=False;self.session_stop_requested=False
                    # An old service lock is not a new readback. Discard it; the
                    # next parameter read queries the service after cleanup.
                    if self.value.get('session_settings',{}):
                        self.value['session_settings']=None
                    self.prepared_event.set()
                    self.value['state']='idle' if code in (0,130) else 'error'
                    if code not in (0,130) and not self.value.get('error'):
                        self.value['error']='KUAVO 客户端异常退出，请查看会话日志'
            if refresh and not self.closing:
                threading.Thread(target=self._refresh_idle_parameters,args=(process,),daemon=True).start()

    def _refresh_idle_parameters(self, previous):
        # Only a read request. Never re-run device preparation after Stop.
        if not self.operation.acquire(blocking=False):return
        try:
            with self.lock:
                if self.closing or self.process is not previous or self.active():return
            command,env,payload=self._launch_options({'network_namespace':'current','settings_request':{'action':'read'}})
            result=subprocess.run([*command,'settings'],input=json.dumps(payload)+'\n',text=True,
                capture_output=True,cwd=self.root,env=env,timeout=30)
            if result.returncode:return
            with self.lock:
                if self.closing or self.process is not previous or self.active():return
                for text in result.stdout.splitlines():
                    if text.startswith('KUAVO:'):
                        data=json.loads(text[6:])
                        for key in ('session_settings','service'):
                            if key in data:self.value[key]=data[key]
        except (OSError,ValueError,subprocess.TimeoutExpired):pass
        finally:self.operation.release()

    def stop(self, session_id):
        with self.lock:
            if session_id != self.session_id:raise ValueError('会话编号已改变，未停止其它会话')
            if self.process is not None and self.process.poll() is None:
                self.session_stop_requested=True
                self.process.stdin.write('stop\n');self.process.stdin.flush()
                self.value['state']='stopping'
            return self.snapshot()

    def close(self):
        self.closing=True
        self.manual.close()
        self.collection.close()
        self.cameras.close()
        self._stop_monitor()
        with self.lock:
            if self.process is None or self.process.poll() is not None:return
            self.stop(self.session_id)
            process=self.process
        # Let the original client finish its cleanup; never kill robot services.
        process.wait(timeout=45)

    def _start_monitor(self, options):
        """Check starts read-only preview; status requests never connect."""
        self._stop_monitor()
        try:
            command, env, payload = self._launch_options(options)
            process = subprocess.Popen([*command, 'monitor'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=self.root, env=env)
            self.monitor_process = process
            self.monitor_value = dict(pose_status='waiting', pose_detail='正在连接只读关节姿态')
            process.stdin.write(json.dumps(payload)+'\n');process.stdin.flush()
            threading.Thread(target=self._read_monitor, args=(process,), daemon=True).start()
        except (OSError, ValueError) as exc:
            self.monitor_value = dict(pose_status='error', pose_detail='只读姿态连接失败：'+str(exc))

    def _read_monitor(self, process):
        try:
            for line in process.stdout:
                if not line.startswith('KUAVO:'):continue
                data = json.loads(line[6:])
                events=data.pop('collection_events',[])
                with self.lock:
                    if self.monitor_process is not process:continue
                    if events:self.collection.accept(events)
                    if not data:continue
                    if data.get('error'):
                        self.monitor_value.update(pose_status='error',pose_detail='只读姿态不可用：'+data['error'])
                    else:
                        # Keep the last valid pose visible, but never mark it current on heartbeats.
                        if not (data.get('telemetry') or {}).get('joints'):
                            data.pop('telemetry', None)
                        self.monitor_value.update(data)
                        if 'telemetry' in data:self.monitor_received=time.monotonic()
        except (ValueError, OSError) as exc:
            with self.lock:
                if self.monitor_process is process:self.monitor_value.update(pose_status='error',pose_detail=str(exc))
        finally:
            process.wait()
            with self.lock:
                if self.monitor_process is process:
                    self.monitor_value.update(pose_status='error', pose_detail=self.monitor_value.get('pose_detail','')+'；姿态订阅已结束，请重新检查设备')

    def _stop_monitor(self):
        # The worker owns only a read-only SSH subscriber, not robot services.
        process, self.monitor_process = self.monitor_process, None
        self.monitor_value = {}
        if process is not None and process.poll() is None:
            try:process.stdin.write('stop\n');process.stdin.flush();process.stdin.close()
            except (OSError, ValueError):pass
            try:process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.terminate()


def kuavo_router(manager):
    router=APIRouter(prefix='/kuavo')
    from .kuavo_cameras import camera_router
    router.routes.extend(camera_router(manager.cameras).routes)
    def invoke(fn, *args, **kwargs):
        try:return fn(*args,**kwargs)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=409,detail=str(exc)) from None
    @router.get('/status')
    def status():return manager.snapshot()
    @router.post('/manual/start')
    def manual_start(): return invoke(manager.manual.start)
    @router.post('/manual/input')
    def manual_input(body:dict): return invoke(manager.manual.input,body)
    @router.post('/manual/stop')
    def manual_stop(body:StopRequest): return invoke(manager.manual.stop,body.session_id)
    @router.post('/manual/poses')
    def manual_poses(body:dict): return invoke(manager.manual.pose_operation,body)
    @router.post('/check')
    def check(body:Options):return invoke(manager.inspect,body.model_dump(),connect_cameras=True)
    @router.post('/start')
    def start(body:Options):return invoke(manager.start,body.model_dump())
    @router.post('/stop')
    def stop(body:StopRequest):return invoke(manager.stop,body.session_id)
    @router.post('/cancel-preparation')
    def cancel_preparation():return invoke(manager.cancel_preparation)
    @router.post('/prepare')
    def prepare(body:Options):return invoke(manager.inspect,body.model_dump(),prepare=True)
    @router.post('/grippers')
    def grippers(body:GripperSettings):return invoke(manager.save_settings,body)
    @router.post('/connection')
    def connection(body:ConnectionSettings):return invoke(manager.save_settings,body)
    @router.post('/motion')
    def motion(body:MotionSettings):return invoke(manager.save_settings,body)
    @router.post('/collection/settings')
    def collection_settings(body:CollectionSettings):return invoke(manager.collection.configure,body)
    @router.post('/session-settings')
    def session_settings(body:SessionSettingsRequest):return invoke(manager.session_settings,body)
    return router
