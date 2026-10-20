from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, StrictInt

from .models import (
    CameraResolutionRequest,
    UmiCameraSelectionRequest,
    DaggerCommandRequest,
    DaggerStartRequest,
    DaggerStartResponse,
    DaggerStatus,
    DeviceStatus,
    HilSerlArtifacts,
    HilSerlCommandRequest,
    HilSerlStartRequest,
    HilSerlStartResponse,
    HilSerlStatus,
    MotionStatus,
    MoveRequest,
    MoveResponse,
    PreviewStatus,
    SessionStatus,
    StartSessionRequest,
    StartSessionResponse,
    StatusResponse,
    StopSessionResponse,
    UmiDeviceConfigureRequest,
    UmiStartRequest,
    UmiStartResponse,
    UmiStatus,
)
from .platform_auth import PlatformLogin
from .login_credentials import CredentialError
from .xr_service import XrServiceStartError
from .umi_preview import preview_stream
from .umi_telemetry import telemetry_events
from .tianji import TianjiManager, tianji_router


class PlatformLoginRequest(BaseModel):
    username: str
    password: str
    remember_password: bool = False
    auto_login: bool = False
    use_saved_password: bool = False


class UploadTargetRequest(BaseModel):
    kind: str


class CameraBrightnessRequest(BaseModel):
    value: StrictInt


class Controller(Protocol):
    def status(self) -> tuple[list[DeviceStatus], SessionStatus, MotionStatus]: ...

    def check_devices(self) -> tuple[list[DeviceStatus], SessionStatus, MotionStatus]: ...

    def start_session(self, request: StartSessionRequest) -> StartSessionResponse: ...

    def stop_session(self, session_id: str) -> StopSessionResponse: ...

    def move(self, request: MoveRequest) -> MoveResponse: ...

    def dagger_status(self) -> DaggerStatus: ...

    def start_dagger(self, request: DaggerStartRequest) -> DaggerStartResponse: ...

    def dagger_command_request(self, request: DaggerCommandRequest) -> DaggerStatus: ...

    def hil_serl_status(self) -> HilSerlStatus: ...

    def hil_serl_artifacts(self) -> HilSerlArtifacts: ...

    def start_hil_serl(self, request: HilSerlStartRequest) -> HilSerlStartResponse: ...

    def hil_serl_command(self, command: str) -> HilSerlStatus: ...

    def delete_hil_serl_artifact(self, path: str) -> None: ...

    def resolve_hil_serl_artifact(self, path: str): ...


class PreviewProvider(Protocol):
    def refresh(self) -> None: ...

    def select_camera(self, camera_id: str) -> None: ...

    def snapshot(self) -> PreviewStatus: ...

    def frame(self, camera_id: str) -> bytes | None: ...


class UmiProvider(Protocol):
    monitor_url: str

    def status(self) -> UmiStatus: ...

    def check(self, collection_profile: str = "dual") -> UmiStatus: ...

    def shutdown_for_app(self) -> UmiStatus: ...

    def configure(self, request: UmiDeviceConfigureRequest) -> UmiStatus: ...

    def candidate_camera_frame(self, camera_key: str) -> bytes | None: ...

    def candidate_camera_stream(self, camera_key: str) -> Iterator[bytes] | None: ...

    def candidate_camera_ready(self, camera_key: str) -> bool: ...

    def encoder_preview(self, collection_profile: str = "dual") -> dict: ...

    def camera_brightness(self, camera_key: str, value: int | None = None) -> dict: ...

    def camera_resolution(self, camera_key: str, mode_id: str | None = None) -> dict: ...

    def start(self, request: UmiStartRequest) -> UmiStartResponse: ...

    def stop(self, session_id: str) -> UmiStatus: ...

    def record(self, action: str) -> UmiStatus: ...


class ImportScanner(Protocol):
    def scan_once(self) -> None: ...


def _next_stream_chunk(stream: Iterator[bytes]) -> bytes | None:
    return next(stream, None)


def _umi_camera_upstream(camera: dict, monitor_url: str) -> str:
    direct_url = camera.get("direct_url")
    if isinstance(direct_url, str) and direct_url.startswith(
        ("http://127.0.0.1:9091/", "http://localhost:9091/")
    ):
        # The desktop Data Platform uses web_video_server's native 30 FPS
        # MJPEG stream. The monitor proxy is only a fallback.
        return direct_url
    proxy_path = camera.get("proxy_url")
    if isinstance(proxy_path, str) and proxy_path.startswith("/"):
        return f"{monitor_url}{proxy_path}"
    raise ValueError("camera stream unavailable")


def create_app(
    controller: Controller,
    *,
    cors_origins: list[str],
    preview: PreviewProvider | None = None,
    umi: UmiProvider | None = None,
    umi_uploader: ImportScanner | None = None,
    platform_api_url: str = "http://127.0.0.1:8000",
    platform_login: PlatformLogin | None = None,
    upload_targets=None,
    umi_gate=None,
) -> FastAPI:
    login_service = platform_login or PlatformLogin(platform_api_url)
    app = FastAPI(title="Robot Data Platform Teleop Control", version="0.1.0")
    tianji = TianjiManager(controller, umi=umi)
    app.state.tianji_manager = tianji
    for route in tianji_router(tianji).routes:
        app.add_api_route(route.path, route.endpoint, methods=route.methods)

    @app.middleware("http")
    async def robot_control_ownership(request: Request, call_next):
        if tianji.active() and request.method == "POST" and request.url.path in {
            "/devices/check", "/sessions", "/move", "/dagger", "/hil-serl", "/umi/check", "/umi/sessions",
        }:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=409, content={"detail": "请先在天机页面断开设备，释放 VR 控制连接"})
        return await call_next(request)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        # A cloud-hosted UI reaches USB/VR devices through the loopback agent
        # on the browser computer. Modern browsers require this explicit PNA
        # opt-in in addition to the normal origin allowlist.
        allow_private_network=True,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/platform/status")
    async def platform_status() -> dict[str, object]:
        """Report platform reachability and the current user session."""
        current_api = upload_targets.api_url if upload_targets is not None else platform_api_url
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{current_api.rstrip('/')}/healthz")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return {
                "healthy": False,
                "api_url": current_api,
                "authenticated": False,
                "user": login_service.store.user(),
                "upload_mode": "direct",
                "detail": str(exc),
            }
        auth = await asyncio.to_thread(login_service.status)
        return {
            "healthy": True,
            "api_url": current_api,
            **auth,
            "upload_mode": "direct",
        }

    @app.get("/platform/auth")
    def platform_auth_status() -> dict[str, object]:
        return login_service.status()

    def check_login_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin and origin not in cors_origins:
            raise HTTPException(status_code=403, detail="不允许该页面访问本机登录信息")

    @app.get('/platform/destinations')
    def platform_destinations(request: Request):
        check_login_origin(request)
        if upload_targets is None:
            raise HTTPException(status_code=503, detail='未配置上传目标')
        return upload_targets.public()

    @app.post('/platform/destinations')
    def select_platform_destination(body: UploadTargetRequest, request: Request):
        check_login_origin(request)
        if upload_targets is None:
            raise HTTPException(status_code=503, detail='未配置上传目标')
        with upload_targets.lock:
            if umi is not None and umi.status().state not in {'idle', 'error'}:
                raise HTTPException(status_code=409, detail='请先结束当前采集会话，再切换上传目标')
            _, session, motion = controller.status()
            if tianji.active() or session.state != 'idle' or motion.state != 'idle':
                raise HTTPException(status_code=409, detail='请先停止遥操作和机械臂运动，再切换平台')
            try:
                result = upload_targets.select(body.kind)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            auth = umi_gate.refresh() if umi_gate is not None else upload_targets.status()
            return {**result, **auth, 'preferences': upload_targets.preferences.public()}

    @app.get("/platform/auth/preferences")
    def platform_login_preferences(request: Request, response: Response) -> dict[str, object]:
        check_login_origin(request)
        response.headers["Cache-Control"] = "no-store"
        return login_service.preferences.public()

    @app.post("/platform/auth/login")
    def platform_auth_login(request: PlatformLoginRequest, http_request: Request) -> dict[str, object]:
        check_login_origin(http_request)
        try:
            return login_service.login(
                request.username, request.password,
                remember_password=request.remember_password,
                auto_login=request.auto_login,
                use_saved_password=request.use_saved_password,
            )
        except CredentialError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in {401, 429}:
                detail = "用户名或密码错误" if code == 401 else "尝试次数过多，请稍后再试"
                raise HTTPException(status_code=code, detail=detail) from None
            raise HTTPException(status_code=502, detail="平台登录服务暂时不可用") from None
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="无法连接数据平台，无法验证账号密码") from None

    @app.post("/platform/auth/logout")
    def platform_auth_logout(request: Request) -> dict[str, object]:
        check_login_origin(request)
        return login_service.logout()

    def require_umi() -> UmiProvider:
        if umi is None:
            raise HTTPException(status_code=503, detail="UMI collection is not configured")
        return umi

    @app.get("/umi/status", response_model=UmiStatus)
    async def umi_status() -> UmiStatus:
        return await asyncio.to_thread(require_umi().status)

    @app.get("/umi/telemetry")
    async def umi_telemetry():
        return StreamingResponse(
            telemetry_events(require_umi().monitor_url),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/umi/check", response_model=UmiStatus)
    async def umi_check(collection_profile: str = "dual") -> UmiStatus:
        if collection_profile not in {"dual", "single-left", "single-right"}:
            raise HTTPException(status_code=400, detail="不支持的采集硬件配置")
        try:
            return await asyncio.to_thread(require_umi().check, collection_profile)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/umi/camera-selection", response_model=UmiStatus)
    async def umi_camera_selection(request: UmiCameraSelectionRequest) -> UmiStatus:
        try:
            return await asyncio.to_thread(require_umi().select_live_camera, request.role, request.device)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/umi/app-shutdown", response_model=UmiStatus)
    async def umi_app_shutdown() -> UmiStatus:
        try:
            return await asyncio.to_thread(require_umi().shutdown_for_app)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/umi/configure", response_model=UmiStatus)
    async def umi_configure(request: UmiDeviceConfigureRequest) -> UmiStatus:
        try:
            return await asyncio.to_thread(require_umi().configure, request)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/umi/sessions", response_model=UmiStartResponse)
    async def start_umi(request: UmiStartRequest) -> UmiStartResponse:
        def begin():
            if upload_targets is None:
                return require_umi().start(request)
            with upload_targets.lock:
                auth = umi_gate.refresh() if umi_gate is not None else upload_targets.status()
                if not auth.get('authenticated'):
                    raise HTTPException(status_code=401, detail='请先登录所选上传平台并确认连接正常')
                return require_umi().start(request)
        try:
            return await asyncio.to_thread(begin)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/umi/sessions/{session_id}/stop", response_model=UmiStatus)
    async def stop_umi(session_id: str) -> UmiStatus:
        try:
            return await asyncio.to_thread(require_umi().stop, session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/umi/import/scan", response_model=UmiStatus)
    async def scan_umi_imports() -> UmiStatus:
        if umi_uploader is None:
            raise HTTPException(status_code=503, detail="UMI import is not configured")
        await asyncio.to_thread(umi_uploader.request_scan)
        return await asyncio.to_thread(require_umi().status)

    @app.post("/umi/record/{action}", response_model=UmiStatus)
    async def control_umi_recording(action: str) -> UmiStatus:
        try:
            return await asyncio.to_thread(require_umi().record, action)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/umi/encoders/preview")
    async def umi_encoder_preview(collection_profile: str = "dual") -> dict:
        try:
            return await asyncio.to_thread(
                require_umi().encoder_preview, collection_profile
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/umi/camera-controls/{camera_key}/brightness")
    async def get_umi_brightness(camera_key: str) -> dict:
        try:
            return await asyncio.to_thread(require_umi().camera_brightness, camera_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/umi/camera-controls/{camera_key}/brightness")
    async def set_umi_brightness(camera_key: str, request: CameraBrightnessRequest) -> dict:
        try:
            return await asyncio.to_thread(require_umi().camera_brightness, camera_key, request.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/umi/camera-controls/{camera_key}/resolution")
    async def get_umi_resolution(camera_key: str) -> dict:
        try:
            return await asyncio.to_thread(require_umi().camera_resolution, camera_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/umi/camera-controls/{camera_key}/resolution")
    async def set_umi_resolution(camera_key: str, request: CameraResolutionRequest) -> dict:
        try:
            return await asyncio.to_thread(require_umi().camera_resolution, camera_key, request.mode_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/umi/cameras/{camera_key}")
    async def umi_camera(camera_key: str, raw: bool = False, native: bool = False):
        provider = require_umi()
        state = await asyncio.to_thread(provider.status)
        camera = next(
            (item for item in state.monitor.get("cameras", []) if item.get("key") == camera_key),
            None,
        )
        if camera is None:
            raise HTTPException(status_code=404, detail="camera not found")
        try:
            upstream_url = _umi_camera_upstream(camera, provider.monitor_url)
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        return StreamingResponse(
            preview_stream(upstream_url, native=native),
            media_type="application/octet-stream" if raw else "multipart/x-mixed-replace; boundary=boundarydonotcross",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/umi/device-cameras/{camera_key}/health")
    async def umi_candidate_camera_health(camera_key: str) -> dict[str, bool]:
        try:
            ready = await asyncio.to_thread(
                require_umi().candidate_camera_ready, camera_key
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ready": ready}

    @app.get("/umi/device-cameras/{camera_key}")
    async def umi_candidate_camera(camera_key: str, raw: bool = False):
        try:
            stream = await asyncio.to_thread(
                require_umi().candidate_camera_stream, camera_key
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if stream is None:
            raise HTTPException(status_code=503, detail="camera preview unavailable")

        async def disconnect_safe_stream():
            try:
                while True:
                    chunk = await asyncio.to_thread(_next_stream_chunk, stream)
                    if chunk is None:
                        break
                    yield chunk
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()

        return StreamingResponse(
            disconnect_safe_stream(),
            media_type="application/octet-stream" if raw else "multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    @app.get("/status", response_model=StatusResponse)
    async def status() -> StatusResponse:
        devices, session, motion = await asyncio.to_thread(controller.status)
        return StatusResponse(
            checked_at=datetime.now(UTC), devices=devices, session=session, motion=motion
        )

    @app.post("/devices/check", response_model=StatusResponse)
    async def check_devices() -> StatusResponse:
        try:
            devices, session, motion = await asyncio.to_thread(controller.check_devices)
        except XrServiceStartError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        robot_ready = any(
            device.id == "robot" and device.status == "connected" for device in devices
        )
        refresh_preview = getattr(preview, "refresh", None)
        if robot_ready and callable(refresh_preview):
            await asyncio.to_thread(refresh_preview)
        return StatusResponse(
            checked_at=datetime.now(UTC), devices=devices, session=session, motion=motion
        )

    @app.get("/preview", response_model=PreviewStatus)
    async def preview_status(camera_id: str | None = None) -> PreviewStatus:
        preview_allowed = getattr(controller, "preview_enabled", lambda: True)()
        if preview is None or not preview_allowed:
            return PreviewStatus(
                available=False,
                cameras=[
                    {"id": camera_id, "available": False}
                    for camera_id in (
                        "head_color",
                        "head_stereo_left",
                        "head_stereo_right",
                        "head_back_fisheye",
                        "head_left_fisheye",
                        "head_right_fisheye",
                        "hand_left_color",
                        "hand_right_color",
                        "head_depth",
                        "hand_left_depth",
                        "hand_right_depth",
                    )
                ],
            )
        if camera_id is not None:
            try:
                await asyncio.to_thread(preview.select_camera, camera_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await asyncio.to_thread(preview.snapshot)

    @app.get("/cameras/{camera_id}/frame")
    async def camera_frame(camera_id: str) -> Response:
        preview_allowed = getattr(controller, "preview_enabled", lambda: True)()
        if preview is None or not preview_allowed:
            return Response(status_code=503)
        try:
            await asyncio.to_thread(preview.select_camera, camera_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        frame = await asyncio.to_thread(preview.frame, camera_id)
        if frame is None:
            return Response(status_code=503)
        return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/sessions", response_model=StartSessionResponse)
    async def start_session(request: StartSessionRequest) -> StartSessionResponse:
        return await asyncio.to_thread(controller.start_session, request)

    @app.post("/sessions/{session_id}/stop", response_model=StopSessionResponse)
    async def stop_session(session_id: str) -> StopSessionResponse:
        return await asyncio.to_thread(controller.stop_session, session_id)

    @app.post("/move", response_model=MoveResponse)
    async def move(request: MoveRequest) -> MoveResponse:
        return await asyncio.to_thread(controller.move, request)

    @app.get("/dagger", response_model=DaggerStatus)
    async def dagger_status() -> DaggerStatus:
        return await asyncio.to_thread(controller.dagger_status)

    @app.post("/dagger", response_model=DaggerStartResponse)
    async def start_dagger(request: DaggerStartRequest) -> DaggerStartResponse:
        return await asyncio.to_thread(controller.start_dagger, request)

    @app.post("/dagger/command", response_model=DaggerStatus)
    async def dagger_command(request: DaggerCommandRequest) -> DaggerStatus:
        return await asyncio.to_thread(controller.dagger_command_request, request)

    @app.get("/hil-serl", response_model=HilSerlStatus)
    async def hil_serl_status() -> HilSerlStatus:
        return await asyncio.to_thread(controller.hil_serl_status)

    @app.get("/hil-serl/artifacts", response_model=HilSerlArtifacts)
    async def hil_serl_artifacts() -> HilSerlArtifacts:
        return await asyncio.to_thread(controller.hil_serl_artifacts)

    @app.post("/hil-serl", response_model=HilSerlStartResponse)
    async def start_hil_serl(request: HilSerlStartRequest) -> HilSerlStartResponse:
        return await asyncio.to_thread(controller.start_hil_serl, request)

    @app.post("/hil-serl/command", response_model=HilSerlStatus)
    async def command_hil_serl(request: HilSerlCommandRequest) -> HilSerlStatus:
        return await asyncio.to_thread(controller.hil_serl_command, request.command)

    @app.post("/hil-serl/artifacts/delete")
    async def delete_hil_serl_artifact(path: str) -> dict[str, bool]:
        await asyncio.to_thread(controller.delete_hil_serl_artifact, path)
        return {"deleted": True}

    @app.get("/hil-serl/artifacts/download")
    async def download_hil_serl_artifact(path: str):
        artifact = await asyncio.to_thread(controller.resolve_hil_serl_artifact, path)
        if artifact.is_file():
            return FileResponse(artifact, filename=artifact.name)
        archive_name = f"{artifact.name}.tar.gz"
        process = subprocess.Popen(
            ["tar", "-czf", "-", "-C", str(artifact.parent), "--", artifact.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        def stream_archive():
            try:
                assert process.stdout is not None
                while chunk := process.stdout.read(1024 * 1024):
                    yield chunk
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait()

        return StreamingResponse(
            stream_archive(),
            media_type="application/gzip",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(archive_name)}"},
        )

    return app
