"""Local Tianji control, shared camera ownership and Episode collection."""

from __future__ import annotations

import asyncio
import atexit
import json
import io
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .gdk_env import add_xr_runtime_env
from .runtime_paths import legacy_robot_python, resolve_robot_python
from .tianji_config import camera_host, robot_configuration
from .tianji_camera_credentials import import_credentials, resolve_credentials
from .tianji_models import TianjiMove, TianjiParameters, TianjiStart
from .tianji_collection import TianjiCollection
from .tianji_camera_session import CameraSession
from .tianji_camera_recovery import restart_local_preview

APP_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = APP_ROOT / "teleop_control/runtime/tianji"
CAMERAS = ("usb_1", "usb_2", "usb_4")


class TianjiManager:
    def __init__(self, controller, *, umi=None):
        self.controller = controller
        self.umi = umi
        self.process = None
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.command_lock = threading.Lock()
        self.checking_devices = False
        self.pending = {}
        self.value = {}
        self.received = 0.0
        self.error = ""
        self.camera_error = ""
        self.frames = {}
        self.camera_thread = None
        self.camera_client = None
        self.camera_session = None
        self.camera_resume_session = None
        self.camera_stop = threading.Event()
        self.camera_cooldown = 2.0
        self.last_access = time.monotonic()
        self.camera_phase = "idle"
        self.camera_preview_restarting = False
        self.camera_recovery_cancel = threading.Event()
        self.diagnostic_events = deque(maxlen=64)
        self.collection = TianjiCollection(self)
        atexit.register(self.close)

    def active(self):
        return self.process is not None and self.process.poll() is None

    def require_other_idle(self):
        if self.umi is not None and self.umi.status().state != "idle":
            raise ValueError("请先停止 UMI 任务，释放 VR 服务")
        _, session, motion = self.controller.status()
        if session.state != "idle" or motion.state != "idle":
            raise ValueError("请先停止 G2 遥操作或手动控制")
        for method in ("dagger_status", "hil_serl_status"):
            fn = getattr(self.controller, method, None)
            if fn and fn().state not in ("idle", "finished", "error"):
                raise ValueError("请先停止其他机器人控制任务")

    def _write(self, value):
        with self.write_lock:
            if not self.active():
                raise RuntimeError(self.error or "天机控制未连接，请检查设备")
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _read(self, process):
        tail = []
        for line in process.stdout:
            if not line.startswith("TIANJI:"):
                tail.append(line.strip())
                tail = tail[-8:]
                continue
            try:
                data = json.loads(line[7:])
            except ValueError:
                continue
            if "telemetry" in data:
                try:
                    self.collection.accept(data["telemetry"])
                except Exception as exc:
                    self.collection.stop("invalid_telemetry", str(exc))
                    self.collection.error = str(exc)
                continue
            with self.lock:
                if process is not self.process:
                    return
                if "status" in data:
                    self.value, self.received = data["status"], time.monotonic()
                elif data.get("reply") in self.pending:
                    event, box = self.pending[data["reply"]]
                    box.update(data)
                    if "state" in data:
                        self.value["state"] = data["state"]
                        self.value["error"] = data.get("error", "")
                        self.received = time.monotonic()
                    event.set()
        code = process.wait()
        with self.lock:
            if process is not self.process:
                return
            self.error = (
                ("天机控制进程已退出" + (("：" + " ".join(tail)[-700:]) if tail else ""))
                if code
                else ""
            )
            self.received = 0
            for event, box in self.pending.values():
                if not event.is_set():
                    box.update(ok=False, error=self.error)
                    event.set()

    def _launch(self):
        if getattr(self.controller, "hardware_enabled", True) is False:
            raise RuntimeError("模拟模式不连接天机或执行真实运动")
        self.require_other_idle()
        self.error = ""
        self.value = {}
        self.received = 0.0
        # Explicit overrides take priority over the relocated bundled robot runtime.
        python = os.environ.get("TIANJI_PYTHON") or os.environ.get("TELEOP_GDK_PYTHON")
        if not python:
            python = resolve_robot_python() or legacy_robot_python()
        if python:
            python = str(Path(python).expanduser())
        env = add_xr_runtime_env(
            os.environ.copy(),
            str(Path(robot_configuration()["service_dir"]) / "runService.sh"),
        )
        env["PYTHONPATH"] = os.pathsep.join(
            (str(APP_ROOT / "teleop_control/src"), env.get("PYTHONPATH", ""))
        )
        self.process = subprocess.Popen(
            [python or sys.executable, "-u", "-m", "teleop_control.tianji_worker"],
            cwd=APP_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def command(self, action, payload=None):
        # Stop can interrupt a pending start/check; ordinary commands serialize.
        if action == "stop":
            return self._command(action, payload)
        with self.command_lock:
            if action == "check":
                self.require_other_idle()
                if self.collection.armed or self.collection.active():
                    raise ValueError("请先停止天机遥操作和采集，再检查设备")
                self.camera_recovery_cancel.clear()
                with self.lock:
                    self.checking_devices = True
            try:
                if action == "check" and not self.active():
                    self._launch()
                return self._command(action, payload)
            finally:
                if action == "check":
                    with self.lock:
                        self.checking_devices = False

    def _command(self, action, payload):
        if action == "start" and self.collection.active():
            raise ValueError("上一轮数据仍在保存，请稍后再开始遥操作")
        if action in ("stop", "close"):
            self.camera_recovery_cancel.set()
            self.collection.disarm()
        ident = uuid.uuid4().hex
        event, box = threading.Event(), {}
        with self.lock:
            self.pending[ident] = (event, box)
        try:
            self._diagnostic_event("command", action=action)
            self._write({"action": action, "payload": payload or {}, "id": ident})
            if not event.wait(60 if action == "check" else 40):
                # Queue a stop after an ambiguous result instead of assuming it failed safely.
                self._write({"action": "stop"})
                raise RuntimeError("天机控制响应超时，已请求停止；请检查设备状态")
            if not box.get("ok"):
                raise RuntimeError(box.get("error", "天机指令失败"))
            self._diagnostic_event("command_ok", action=action)
        finally:
            with self.lock:
                self.pending.pop(ident, None)
        if action == "check":
            self.check_camera_recovery()
            if not self.camera_recovery_cancel.is_set():
                self.last_access = time.monotonic()
                self.start_cameras()
        elif action == "start":
            self.collection.arm(payload)
        return {"ok": True}

    def _diagnostic_event(self, kind, **details):
        # Bounded in-memory metadata only; never perform file I/O on the SDK reader.
        with self.lock:
            self.diagnostic_events.append(dict(
                kind=kind, monotonic=time.monotonic(), wall_time=time.time(), **details
            ))

    def diagnostics(self):
        # Monitoring must not keep motion or camera operator leases alive.
        with self.lock:
            return dict(status=self.snapshot(touch=False),
                        monotonic=time.monotonic(), wall_time=time.time(),
                        last_operator_access=self.last_access,
                        camera_phase=self.camera_phase,
                        camera_thread_alive=bool(self.camera_thread and self.camera_thread.is_alive()),
                        events=list(self.diagnostic_events))

    def snapshot(self, *, touch=True):
        if touch:
            self.last_access = time.monotonic()
        if touch and self.active():
            try:
                self._write({"action": "heartbeat"})
            except (OSError, RuntimeError):
                pass
        with self.lock:
            fresh = self.active() and time.monotonic() - self.received < 2
            value = dict(self.value) if fresh else {}
            # An idle check can block the worker before its first status (or
            # while opening devices). This is not lost feedback during motion.
            fallback_state = "idle"
            if self.active():
                fallback_state = (
                    "checking"
                    if self.checking_devices and self.value.get("state", "idle") == "idle"
                    else "unknown"
                )
            value["state"] = value.get("state", fallback_state)
            value["error"] = value.get("error") or self.error
            value["joints"] = value.get("joints", {})
            value["devices"] = [
                {
                    "id": "robot",
                    "name": "Tianji",
                    "status": "connected" if value.get("robot_ready") else "offline",
                    "detail": "",
                },
                {
                    "id": "xr",
                    "name": "Galaxis VR",
                    "status": "connected" if value.get("vr_ready") else "offline",
                    "detail": value.get("vr_detail", ""),
                },
                {
                    "id": "headset",
                    "name": "Pico 设备",
                    "status": "connected" if value.get("pico_ready") else "offline",
                    "detail": value.get("pico_detail", ""),
                },
            ]
            value["cameras"] = [
                {
                    "id": c,
                    "available": c in self.frames and time.monotonic() - self.frames[c][1] < 2,
                    "updated_at": self.frames[c][1] if c in self.frames else None,
                }
                for c in CAMERAS
            ]
            grippers = value.get("grippers")
            if grippers:
                for motor in grippers.get("motors", {}).values():
                    feedback = motor.get("feedback")
                    if feedback:
                        feedback["position_rad"] = feedback.get("position")
                        feedback["age_s"] = time.monotonic() - feedback.get("received", 0)
            value["camera_error"] = self.camera_error
            value["camera_phase"] = self.camera_phase
            value["collection"] = self.collection.snapshot()
            value["recording"] = value["collection"]["recording"]
            return value

    def parameters(self):
        path = RUNTIME / "parameters.json"
        try:
            return TianjiParameters.model_validate_json(path.read_text())
        except (OSError, ValueError):
            return TianjiParameters()

    def save_parameters(self, parameters):
        if self.snapshot()["state"] != "idle":
            raise ValueError("请先停止控制后保存参数")
        RUNTIME.mkdir(parents=True, exist_ok=True)
        temporary = RUNTIME / "parameters.tmp"
        temporary.write_text(parameters.model_dump_json(indent=2))
        temporary.replace(RUNTIME / "parameters.json")
        return parameters

    def start_cameras(self):
        if self.camera_preview_restarting:
            return
        if self.camera_thread and self.camera_thread.is_alive():
            return
        self.camera_stop.clear()
        self.camera_thread = threading.Thread(target=self._cameras, daemon=True)
        self.camera_thread.start()

    def check_camera_recovery(self):
        # Check Devices only rebuilds the local preview receiver.
        with self.lock:
            healthy = (self.camera_thread and self.camera_thread.is_alive()
                       and not self.camera_error and self.camera_phase == "streaming"
                       and all(c in self.frames and time.monotonic() - self.frames[c][1] < 2
                               for c in CAMERAS))
        if healthy:
            return
        self.camera_preview_restarting = True
        try:
            restart_local_preview(self)
        except Exception as exc:
            self.camera_error = str(exc)
            self.camera_phase = "fault"
            raise RuntimeError(self.camera_error) from exc
        finally:
            self.camera_preview_restarting = False

    def new_camera_client(self):
        sdk_path = str(APP_ROOT / "fusion_camera_stream")
        if sdk_path not in sys.path:
            sys.path.insert(0, sdk_path)
        from fusion_camera import FusionStreamClient

        host = camera_host()
        credentials = str(resolve_credentials(RUNTIME))
        try:
            return FusionStreamClient(host, credentials=credentials, timeout=5)
        except FileNotFoundError as exc:
            raise RuntimeError("缺少相机授权配置或证书，请在相机画面右上角导入 client.json；证书须与配置一起提供") from exc

    def import_camera_credentials(self, path):
        with self.command_lock:
            from .tianji_camera_recovery import require_idle
            require_idle(self)
            if self.camera_thread and self.camera_thread.is_alive():
                self.pause_cameras()
            import_credentials(Path(path))
            with self.lock:
                self.camera_error = ""
                self.camera_phase = "idle"
                self.frames.clear()
            return {"ok": True}

    def resume_cameras_after_episode(self):
        # Called after the real end/clock query, with a fallback after failed
        # collection cleanup. Never reopen USB from the image writer callback.
        with self.lock:
            session = self.camera_session
            if session is None or not session.attached:
                self.start_cameras()
                return
            if self.camera_resume_session is session:
                return
            self.camera_resume_session = session
            previous_thread = self.camera_thread

        def resume():
            if previous_thread:
                previous_thread.join(timeout=12)
                if previous_thread.is_alive():
                    self.camera_error = "相机仍未释放，无法恢复预览"
                    self.camera_phase = "fault"
                    return
            if self.camera_stop.wait(self.camera_cooldown + 0.1):
                return
            if self.collection.armed and time.monotonic() - self.last_access < 8:
                self.start_cameras()
            elif self.camera_phase == "resuming":
                self.camera_phase = "idle"
                with self.lock:
                    self.frames.clear()

        threading.Thread(target=resume, name="tianji-preview-resume", daemon=True).start()

    def adopt_camera_stream(self):
        session = self.camera_session
        if session is None:
            raise RuntimeError("相机预览尚未就绪，请先检查设备")
        client = session.subscribe()
        self._diagnostic_event("collection_adopted_preview")
        return client

    def pause_cameras(self):
        self._diagnostic_event("preview_pause_requested")
        self.camera_stop.set()
        client = self.camera_client
        if client:
            client.request_stop()
        if self.camera_thread:
            self.camera_thread.join(timeout=12)
            if self.camera_thread.is_alive():
                raise RuntimeError("相机预览尚未释放，请稍后再开始采集")
        time.sleep(self.camera_cooldown + 0.1)

    def _prepare_preview(self, client):
        status = client.status()
        cameras = client.list_cameras()
        self._diagnostic_event("preview_preflight", cameras=cameras,
                               service_fault=status.get("fault"))
        available = {c["id"] for c in cameras if c.get("available")}
        labels = {"usb-1": "主相机", "usb-2": "右腕相机", "usb-4": "左腕相机"}
        missing = [f"{name}（{key}）" for key, name in labels.items() if key not in available]
        if missing:
            # Do not ask the serial service to open an incomplete group: its
            # stream preflight would latch the temporary missing-device fault.
            raise RuntimeError("控制器未识别到" + "、".join(missing) + "；未启动三路取流")
        if status.get("idle") is not True:
            raise RuntimeError("相机服务正被其他取流任务占用")
        if status.get("fault"):
            raise RuntimeError("控制器相机服务故障，本地重连无法解除，请在控制器端处理："
                               + str(status["fault"]))
        seconds = status.get("max_stream_seconds", 60)
        if type(seconds) is not int or seconds < 1:
            raise RuntimeError("相机服务返回无效的取流时限")
        self.camera_cooldown = max(2.0, float(status.get("cooldown_seconds", 2.0)))
        return min(seconds, 600)

    def _cameras(self):
        # Reuse the app's existing camera SDK, not another copied SDK tree.
        sys.path.insert(0, str(APP_ROOT / "fusion_camera_stream"))
        from fusion_camera import CameraError
        session = None
        try:
            self.camera_phase = "connecting"
            self._diagnostic_event("preview_connecting")
            with self.new_camera_client() as client:
                self.camera_client = client
                while not self.camera_stop.is_set() and time.monotonic() - self.last_access < 8:
                    seconds = self._prepare_preview(client)
                    session = CameraSession(client, client.clock_sync(samples=3), self.new_camera_client)
                    self.camera_session = session
                    self.camera_phase = "starting"
                    self._diagnostic_event("preview_stream_requested", seconds=seconds)
                    first_frames = set()
                    # Explicit observation mode keeps images in RAM during motion.
                    try:
                        for event in client.stream(seconds=seconds, mode="teleop_observation"):
                            session.publish(event)
                            if event.metadata.get("type") in ("start", "end"):
                                self._diagnostic_event("preview_" + event.metadata["type"], metadata=event.metadata)
                            if self.camera_stop.is_set() or time.monotonic() - self.last_access > 8:
                                client.request_stop()
                            if event.metadata.get("type") == "image":
                                camera = str(event.metadata.get("camera_id")).replace("-", "_")
                                if camera in CAMERAS:
                                    if camera not in first_frames:
                                        first_frames.add(camera)
                                        self.camera_phase = "streaming"
                                        self._diagnostic_event("preview_first_frame", camera=camera)
                                    data = event.data
                                    if event.metadata.get("format") == "yuyv":
                                        buffer = io.BytesIO()
                                        event.frame().to_pil().save(buffer, format="JPEG", quality=85)
                                        data = buffer.getvalue()
                                    with self.lock:
                                        self.frames[camera] = (data, time.monotonic())
                                        self.camera_error = ""
                    except CameraError as exc:
                        session.finish(exc)
                        raise
                    session.finish()
                    if session.attached:
                        # The Episode owns this real end and its final clock query.
                        # Its completion path starts the next preview after release.
                        break
                    self.camera_error = ""
                    self.camera_phase = "cooldown"
                    # Respect the camera service's cleanup interval before the next observation.
                    if self.camera_stop.wait(self.camera_cooldown + 0.1):
                        break
        except Exception as exc:
            self.camera_error = "相机预览不可用：" + str(exc)
            self.camera_phase = "fault"
            self._diagnostic_event("preview_error", error=str(exc))
        finally:
            if session:
                session.finish(self.camera_error or None)
            resuming = (session and session.attached and session.end
                        and session.end.metadata.get("ok") and self.collection.armed
                        and not self.camera_stop.is_set())
            if self.camera_phase != "fault":
                self.camera_phase = "resuming" if resuming else "idle"
            self._diagnostic_event("preview_exited", phase=self.camera_phase)
            self.camera_client = None
            if not resuming:
                with self.lock:
                    self.frames.clear()

    def close(self):
        self.camera_recovery_cancel.set()
        self.collection.disarm("app_closed")
        self.camera_stop.set()
        if self.camera_client:
            self.camera_client.close()
        if self.active():
            try:
                self._command("close", {})
                self.process.wait(timeout=8)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                self.process.terminate()
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("无法确认天机已停止，请检查硬件急停和控制进程") from exc
                if self.process.returncode != 0:
                    raise RuntimeError("天机控制异常退出，请确认机械臂已停止") from exc
        self.collection.close()
        return {"ok": True}


def tianji_router(manager):
    router = APIRouter(prefix="/tianji")

    def call(fn, *args):
        try:
            return fn(*args)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/status")
    def status():
        return manager.snapshot()

    @router.get("/diagnostics")
    def diagnostics():
        return manager.diagnostics()

    @router.post("/camera-credentials")
    def camera_credentials(payload: dict):
        path = payload.get("path")
        if not isinstance(path, str) or not path:
            raise HTTPException(422, "请选择相机配置文件")
        return call(manager.import_camera_credentials, path)

    @router.post("/check")
    def check():
        return call(manager.command, "check")

    @router.get("/parameters")
    def parameters():
        return manager.parameters()

    @router.post("/parameters")
    def save(parameters: TianjiParameters):
        return call(manager.save_parameters, parameters)

    @router.post("/start")
    def start(request: TianjiStart):
        call(manager.require_other_idle)
        return call(manager.command, "start", request.model_dump())

    @router.post("/move")
    def move(request: TianjiMove):
        call(manager.require_other_idle)
        return call(
            manager.command,
            "move",
            {**request.model_dump(), "parameters": manager.parameters().model_dump()},
        )

    @router.post("/stop")
    def stop():
        return call(manager.command, "stop")

    @router.post("/record/{action}")
    def record(action: str):
        if action == "start":
            call(manager.collection.start)
        elif action == "stop":
            manager.collection.stop("ui_stop")
        else:
            raise HTTPException(404, "未知采集操作")
        return manager.collection.snapshot()

    @router.post("/close")
    def close():
        return call(manager.close)

    @router.get("/cameras/{camera_id}/stream")
    async def stream(camera_id: str, request: Request, raw: bool = False):
        if camera_id not in CAMERAS:
            raise HTTPException(404, "未知相机")

        async def frames():
            stamp = None
            while not await request.is_disconnected():
                with manager.lock:
                    value = manager.frames.get(camera_id)
                if value and value[1] != stamp and time.monotonic() - value[1] < 2:
                    stamp = value[1]
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + value[0] + b"\r\n")
                elif ((manager.camera_thread is None or not manager.camera_thread.is_alive())
                      and not manager.collection.active()
                      and manager.camera_phase != "resuming"):
                    # Keep the HTTP reader across the audited end/reopen gap.
                    break
                await asyncio.sleep(1 / 30)

        return StreamingResponse(
            frames(),
            media_type="application/octet-stream"
            if raw
            else "multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.get("/cameras/{camera_id}/frame")
    def frame(camera_id: str):
        if camera_id not in CAMERAS:
            raise HTTPException(404, "未知相机")
        with manager.lock:
            value = manager.frames.get(camera_id)
            if not value or time.monotonic() - value[1] > 2:
                raise HTTPException(503, "等待相机画面")
            return Response(
                value[0], media_type="image/jpeg", headers={"Cache-Control": "no-store"}
            )

    return router
