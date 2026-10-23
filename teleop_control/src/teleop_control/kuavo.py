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
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from .runtime_paths import APP_ROOT, resolve_robot_python, legacy_robot_python, resolve_xr_service
from .gdk_env import add_xr_runtime_env
from .kuavo_preview import KuavoModel
from .kuavo_settings import LocalSettings, GripperSettings, ConnectionSettings

LIMITATIONS = [
    '本地保存完整遥操会话日志；尚未接入图像＋关节同步数据集及平台上传。',
    '头部相机需独立 AGX 视频连接；本页暂未接入预览、相机启动和图像录制。双腕相机接口尚未提供。',
    '姿态使用官方 v63 外观模型：会话内更新双臂实测反馈，其余部位为参考零位；夹爪外观及开合遥测待接入。',
    '夹爪参数可编辑并保存本机 JSON；现有机器人服务不支持在线改参，需更新机器人服务配置后生效。手臂速度、加速度仍沿用原配置。',
    '单臂模式屏蔽另一侧 Grip / Trigger，并禁用双臂 Y 回位；右 B 取消仍有效。',
]

class Options(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    network_namespace: Literal['kuavons','current'] = 'current'
    task: str = Field(default='KUAVO 遥操作', min_length=1, max_length=120)
    confirm_motion: bool = False
    accept_unverified_gripper_bus: bool = False
    motor_ids_confirmed: bool = False
    arm_mode: Literal['dual','left','right'] = 'dual'
    position_scale: float = Field(default=1., ge=.1, le=1., allow_inf_nan=False)
    preparation_action: Literal['start','initialize','grippers_inspect','grippers_prepare'] = 'start'

class StopRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str

class KuavoManager:
    def __init__(self, controller, *, other_idle=lambda: None, root=APP_ROOT):
        self.controller, self.other_idle, self.root = controller, other_idle, Path(root)
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.process = None
        self.session_id = None
        self.pending_operation = False
        self.last_status = 0.
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

    def active(self):
        with self.lock:
            return self.pending_operation or self.process is not None and self.process.poll() is None

    def snapshot(self):
        with self.lock:
            value = copy.deepcopy(self.value)
            if self.process is not None and self.process.poll() is None and time.monotonic()-self.last_status>5:
                value.update(state='unknown',error='客户端状态未更新；请结束本次会话并检查日志')
            value.update(session_id=self.session_id, active=self.active(), logs=list(self.logs))
            age = None if self.feedback_age is None else self.feedback_age+time.monotonic()-self.feedback_received
            value['pose_live'] = bool(value['active'] and age is not None and age<1.)
            value['pose_available'] = self.feedback_stamp is not None
            value['pico_live'] = bool(value.get('pico_live') and value['active'] and time.monotonic()-self.last_status<1.)
            value['gripper_settings'] = self.settings.snapshot(value.get('service') if value['state'] not in ('error','unknown') else {})
            value['connection'] = self.settings.connection_snapshot()
            return value

    def save_settings(self, body):
        with self.lock:
            if self.active(): raise RuntimeError('请先结束 KUAVO 会话或等待检查完成')
            self.other_idle()
            if isinstance(body, ConnectionSettings):
                self.settings.save_connection(body)
                self.value.update(service={}, preparation={}, pico_sdk_available=False, state='idle', error='')
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
        with self.lock:
            telemetry=data.get('telemetry') or {}
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
                self.value['state'] = {'finished':'idle','failed':'error'}.get(phase,phase)
                report = data['session'].get('report') or {}
                self.value['error'] = data['session'].get('error') or '; '.join(report.get('errors',[]))

    def inspect(self, options, *, prepare=False):
        if not self.operation.acquire(blocking=False):
            raise RuntimeError('已有操作正在处理，请等待结果')
        try:
            with self.lock:
                if self.active():raise RuntimeError('请先结束 KUAVO 会话')
                self.other_idle()
                if prepare and options.get('preparation_action')=='grippers_prepare':
                    self.settings.expected_gripper_hash()
                self.pending_operation = True
                self.value.update(state='checking',error='')
                if not prepare:self.value.update(service={},pico_sdk_available=False,vr_checked=False,vr_ready=False,pico_ready=False)
            command, env, payload = self._launch_options(options)
            result = subprocess.run([*command, 'prepare' if prepare else 'check'], input=json.dumps(payload)+'\n',
                text=True, capture_output=True, cwd=self.root, env=env, timeout=240 if prepare else 90)
            received=False
            for line in result.stdout.splitlines():
                if line.startswith('KUAVO:'):
                    self._merge(json.loads(line[6:]))
                    received=True
            if result.returncode:
                raise RuntimeError(self.value.get('error') or result.stderr[-1200:] or '客户端检查失败')
            if not received:raise RuntimeError('客户端没有返回检查结果')
            self.value['state']='idle'
            self.pending_operation=False
            return self.snapshot()
        except Exception as exc:
            with self.lock:self.value.update(state='error',error=str(exc))
            raise
        finally:
            with self.lock:self.pending_operation=False
            self.operation.release()

    def start(self, options):
        if options.get('confirm_motion') is not True:
            raise ValueError('请明确确认启动遥操作')
        with self.lock:
            if self.active():raise RuntimeError('上一会话仍在运行或退出')
            self.other_idle()
            self.settings.expected_gripper_hash()
            command, env, payload = self._launch_options(options)
            self.session_id = uuid.uuid4().hex
            payload['output'] = str(self.root/'teleop_control/runtime/kuavo/episodes'/
                                    (time.strftime('%Y%m%d_%H%M%S')+'_'+self.session_id[:8]))
            self.logs.clear()
            self.value.update(state='starting',error='',session={},log_directory='')
            self.value.update(telemetry={},pico_live=False,segments=self.model.segments())
            self.feedback_stamp=self.feedback_age=None
            self.last_status=time.monotonic()
            try:
                process = subprocess.Popen([*command,'execute'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, bufsize=1, cwd=self.root, env=env)
            except OSError as exc:
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
            code=process.wait()
            process.stdin.close()
            with self.lock:
                if self.process is process:
                    self.value['state']='idle' if code in (0,130) else 'error'
                    if code not in (0,130) and not self.value.get('error'):
                        self.value['error']='KUAVO 客户端异常退出，请查看会话日志'

    def stop(self, session_id):
        with self.lock:
            if session_id != self.session_id:raise ValueError('会话编号已改变，未停止其它会话')
            if self.process is not None and self.process.poll() is None:
                self.process.stdin.write('stop\n');self.process.stdin.flush()
                self.value['state']='stopping'
            return self.snapshot()

    def close(self):
        with self.lock:
            if self.process is None or self.process.poll() is not None:return
            self.stop(self.session_id)
            process=self.process
        # Let the original client finish its cleanup; never kill robot services.
        process.wait(timeout=45)


def kuavo_router(manager):
    router=APIRouter(prefix='/kuavo')
    def invoke(fn, *args, **kwargs):
        try:return fn(*args,**kwargs)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=409,detail=str(exc)) from None
    @router.get('/status')
    def status():return manager.snapshot()
    @router.post('/check')
    def check(body:Options):return invoke(manager.inspect,body.model_dump())
    @router.post('/start')
    def start(body:Options):return invoke(manager.start,body.model_dump())
    @router.post('/stop')
    def stop(body:StopRequest):return invoke(manager.stop,body.session_id)
    @router.post('/prepare')
    def prepare(body:Options):return invoke(manager.inspect,body.model_dump(),prepare=True)
    @router.post('/grippers')
    def grippers(body:GripperSettings):return invoke(manager.save_settings,body)
    @router.post('/connection')
    def connection(body:ConnectionSettings):return invoke(manager.save_settings,body)
    return router
