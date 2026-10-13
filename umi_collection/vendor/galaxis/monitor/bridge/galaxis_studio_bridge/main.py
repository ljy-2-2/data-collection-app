"""Galaxis Studio 桥接服务 HTTP API。

- ``GET  /``               采集监看页(静态资源)
- ``GET  /api/health``     健康检查
- ``GET  /api/state``      最新状态快照
- ``GET  /api/ws``         WebSocket 状态推送
- ``GET  /api/streams``    相机流地址列表
- ``GET  /proxy/stream``   MJPEG 代理(供跨域/离线场景)
- ``POST /api/record/start`` / ``stop``  录制控制(需开启 STUDIO_ENABLE_CONTROL)
"""

# 注意:不要在此文件启用 ``from __future__ import annotations``。
# FastAPI 对局部作用域内的字符串注解解析有限,会导致 websocket 参数被误判为
# 查询参数(握手被以 1008 拒绝)。本文件全部使用运行时注解。

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

from .config import Settings, preview_stream_url_for, stream_url_for
from .ros_state import RosStateCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("galaxis.studio")

WEBUI_DIR = Path(__file__).resolve().parents[2] / "webui"
_RECORD_SERVICE = {
    # Use the same state-machine commands as the physical A/X and B/Y buttons.
    # Primitive start/stop_recording skips the continuous round segment and can
    # appear to record without producing a saveable HDF5 Episode.
    "start": ("/data_collection/accept", "std_srvs/srv/Trigger"),
    "stop": ("/data_collection/next_round", "std_srvs/srv/Trigger"),
    "cancel": ("/data_collection/cancel_episode", "std_srvs/srv/Trigger"),
}

# 增量推送的高频字段:这些键变化时才发送 patch,其余低频/静态字段随全量快照同步
_FAST_KEYS = (
    "ros_connected",
    "recording",
    "task_status",
    "task",
    "episode",
    "encoders",
    "vr",
    "episode_elapsed",
    "subtask_elapsed",
    "episodes_completed_total",
    "storage_gb",
    "logs",
)


def create_app(settings: Settings, collector: RosStateCollector):
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.websockets import WebSocket, WebSocketDisconnect

    app = FastAPI(title="Galaxis Studio Bridge", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if WEBUI_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=str(WEBUI_DIR)), name="assets")
        app.mount("/css", StaticFiles(directory=str(WEBUI_DIR / "css")), name="css")
        app.mount("/js", StaticFiles(directory=str(WEBUI_DIR / "js")), name="js")

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "ok": True,
            "mode": "mock" if settings.mock else "ros",
            "ros_connected": collector.snapshot().get("ros_connected", False),
            "enable_control": settings.enable_control,
        }

    @app.get("/api/state")
    async def state() -> dict:
        return collector.snapshot()

    @app.get("/api/streams")
    async def streams() -> dict:
        return {
            "video": {
                "host": settings.video_host,
                "port": settings.video_port,
            },
            "cameras": [
                {
                    "key": key,
                    "label": label,
                    "topic": topic,
                    "direct_url": preview_stream_url_for(settings, topic),
                    "proxy_url": f"/proxy/stream?topic={topic}",
                }
                for key, label, topic in settings.cameras
            ],
        }

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """增量推送:高频字段以 patch 消息发送,完整快照每约 1 秒同步一次。

        这样可以在 20 Hz 刷新编码器/VR 的同时,把单帧负载从全量 JSON(含相机、
        日志等静态字段)缩小到只有变化的几个字段,浏览器解析与渲染压力大减。
        """
        await websocket.accept()
        last_full: dict[str, Any] = {}
        tick = 0
        try:
            while True:
                snapshot = collector.snapshot()
                tick += 1
                if not last_full or tick % 20 == 0:
                    # 首帧或每 20 个周期(约 1s)发送一次完整快照兜底
                    await websocket.send_json(snapshot)
                    last_full = snapshot
                else:
                    patch = {
                        key: snapshot[key]
                        for key in _FAST_KEYS
                        if key in snapshot and snapshot[key] != last_full.get(key)
                    }
                    if patch:
                        await websocket.send_json({"type": "patch", "fields": patch})
                        for key, value in patch.items():
                            last_full[key] = value
                # 轻量心跳;短超时保证快变数据高频推送(20 Hz)
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("WebSocket 断开: %s", exc)

    @app.get("/proxy/stream")
    async def proxy_stream(topic: str, fps: int = 20, width: int = 0, quality: int = 60):
        """抽帧转发 web_video_server 的 MJPEG 流。

        页面预览走本代理,按 fps 对 30 fps 源做均匀抽帧;width>0 时用
        Pillow 解码缩放后重编码(大幅降低浏览器解码负担,用于 3 相机时
        的左右手小窗)。录制数据由 rosbag 直连 DDS 全帧率写入,与预览
        完全解耦,不影响录制帧率。fps<=0 时不做抽帧。
        """
        import re
        import urllib.request

        target = stream_url_for(settings, topic)
        # Full-rate, unscaled previews already contain camera-produced JPEGs.
        # Redirect them straight to web_video_server so the monitor process
        # does not copy and parse roughly 90 full JPEG frames per second while
        # rosbag is writing.  The proxy remains available for explicitly
        # downscaled/lower-rate fallbacks.
        if int(fps) >= 30 and int(width) <= 0:
            return RedirectResponse(
                preview_stream_url_for(settings, topic),
                status_code=307,
                headers={"Cache-Control": "no-store"},
            )
        try:
            response = urllib.request.urlopen(target, timeout=10)  # noqa: S310
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"视频流不可用: {exc}") from exc

        try:
            from PIL import Image  # noqa: PLC0415
            _has_pil = True
        except ImportError:
            _has_pil = False

        source_fps = 30
        target_fps = min(int(fps), source_fps) if fps and fps > 0 else source_fps
        scale_width = max(0, int(width))
        boundary = b"--boundarydonotcross"
        # 预编译正则:每帧都做一次查找/替换,避免重复编译开销
        _cl_search = re.compile(rb"Content-Length:\s*(\d+)")
        _cl_replace = re.compile(rb"Content-Length:\s*\d+")

        def keep_frame(idx: int) -> bool:
            """Bresenham 式均匀抽帧:source 帧中均匀保留 target 帧。"""
            if target_fps >= source_fps:
                return True
            return (idx * target_fps) // source_fps != ((idx - 1) * target_fps) // source_fps

        def shrink(frame: bytes) -> bytes:
            """解码->缩放到 width->按 quality 重编码 JPEG;失败则原帧输出。"""
            if not _has_pil or scale_width <= 0:
                return frame
            body_start = frame.find(b"\r\n\r\n")
            if body_start < 0:
                return frame
            body_start += 4
            try:
                from io import BytesIO

                img = Image.open(BytesIO(frame[body_start:]))
                img.load()
                if img.width > scale_width:
                    new_h = max(1, round(img.height * scale_width / img.width))
                    img = img.resize((scale_width, new_h), Image.BILINEAR)
                out = BytesIO()
                img.save(out, format="JPEG", quality=max(10, min(95, quality)))
                new_body = out.getvalue()
            except Exception:  # noqa: BLE001
                return frame
            # 原位替换 Content-Length,不能插到空行后(否则会被当成 body 的一部分)
            head = frame[:body_start]
            new_cl = b"Content-Length: " + str(len(new_body)).encode()
            new_head, n = _cl_replace.subn(new_cl, head)
            if n == 0:
                new_head = (
                    head[:-2]
                    + b"Content-Length: "
                    + str(len(new_body)).encode()
                    + b"\r\n\r\n"
                )
            return new_head + new_body

        def generate():
            buf = b""
            frame_index = 0
            try:
                while True:
                    # A small read size bounds latency.  A 64 KiB blocking read
                    # can collect several JPEGs before the generator gets a
                    # chance to emit one; when rosbag starts writing, that
                    # backlog is visible as jitter and stale video.
                    chunk = response.read(16384)
                    if not chunk:
                        break
                    buf += chunk
                    newest_frame = None
                    while True:
                        if not buf.startswith(boundary):
                            idx = buf.find(boundary)
                            if idx < 0:
                                buf = buf[-len(boundary):]
                                break
                            buf = buf[idx:]
                        header_end = buf.find(b"\r\n\r\n")
                        if header_end < 0:
                            break
                        match = _cl_search.search(buf[:header_end])
                        if not match:
                            nxt = buf.find(boundary, len(boundary))
                            if nxt < 0:
                                break
                            frame = buf[:nxt]
                            buf = buf[nxt:]
                        else:
                            body_len = int(match.group(1))
                            total = header_end + 4 + body_len
                            if len(buf) < total:
                                break
                            frame = buf[:total]
                            buf = buf[total:]
                        frame_index += 1
                        if keep_frame(frame_index):
                            # Preview is a live view, not a recording.  If the
                            # producer delivered several frames while this
                            # request was busy, emit only the newest candidate
                            # instead of making the browser decode stale JPEGs.
                            newest_frame = frame
                    if newest_frame is not None:
                        yield shrink(newest_frame) + b"\r\n"
            finally:
                response.close()

        return StreamingResponse(
            generate(),
            media_type="multipart/x-mixed-replace; boundary=boundarydonotcross",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    async def index() -> FileResponse:
        index_file = WEBUI_DIR / "index.html"
        if not index_file.is_file():
            raise HTTPException(status_code=404, detail="webui 未构建")
        return FileResponse(str(index_file))

    if settings.enable_control:
        _register_control(app, collector)

    return app


def _register_control(app, collector: RosStateCollector) -> None:
    """录制控制接口(可选)。通过 ROS 服务下发,失败时返回 503。"""
    from fastapi import HTTPException

    async def _call(service_name: str, request_type: str, payload: str = "") -> dict:
        result = await asyncio.to_thread(_ros_service_call, service_name, request_type, payload)
        if result is None:
            raise HTTPException(status_code=503, detail=f"ROS 录制服务尚未就绪: {service_name}")
        if not result.get("success", False):
            raise HTTPException(
                status_code=409,
                detail=result.get("message") or "录制安全条件未满足",
            )
        return result

    @app.post("/api/record/start")
    async def record_start() -> dict:
        return await _call(*_RECORD_SERVICE["start"])

    @app.post("/api/record/stop")
    async def record_stop() -> dict:
        return await _call(*_RECORD_SERVICE["stop"])

    @app.post("/api/record/cancel")
    async def record_cancel() -> dict:
        return await _call(*_RECORD_SERVICE["cancel"])

    collector.log_event("info", "录制控制接口已启用(STUDIO_ENABLE_CONTROL=1)")


def _ros_service_call(service_name: str, request_type: str, payload: str = "") -> dict | None:
    """同步调用 ROS 服务(在后台线程中执行)。"""
    try:
        import rclpy
    except ImportError:
        return None
    try:
        node = rclpy.create_node("_studio_control")
        client = node.create_client(
            _import_msg_type(request_type), service_name
        )
        if not client.wait_for_service(timeout_sec=3.0):
            node.destroy_node()
            return None
        request = client.srv_type.Request()
        if payload:
            request.data = payload
        future = client.call_async(request)
        timeout = (40.0 if service_name == _RECORD_SERVICE["start"][0]
                   else 40.0 if service_name == _RECORD_SERVICE["stop"][0] else 10.0)
        rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
        node.destroy_node()
        response = future.result()
        if response is None:
            return None
        return {"success": bool(getattr(response, "success", True)),
                "message": getattr(response, "message", "")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("调用 ROS 服务失败 %s (%s): %s", service_name, request_type, exc)
        return None


def _import_msg_type(type_name: str):
    package, kind, class_name = type_name.split("/", 2)
    if kind != "srv":
        raise ValueError(f"不支持的 ROS 服务类型: {type_name}")
    module = __import__(f"{package}.srv", fromlist=[class_name])
    return getattr(module, class_name)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Galaxis Studio 桥接服务")
    parser.add_argument("--host", default=None, help="监听地址(默认取环境变量 STUDIO_HOST)")
    parser.add_argument("--port", type=int, default=None, help="监听端口(默认 8770)")
    parser.add_argument(
        "--mock",
        action="store_true",
        default=None,
        help="模拟数据模式(无 ROS 环境时预览界面)",
    )
    parser.add_argument(
        "--enable-control",
        action="store_true",
        default=None,
        help="开启录制控制接口",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    args = parse_args(argv)
    defaults = Settings()
    settings = Settings(
        host=args.host or defaults.host,
        port=args.port if args.port is not None else defaults.port,
        mock=args.mock if args.mock is not None else defaults.mock,
        enable_control=(
            args.enable_control if args.enable_control is not None else defaults.enable_control
        ),
    )
    collector = RosStateCollector(settings, mock=settings.mock)
    collector.start()
    collector.log_event("info", f"桥接服务启动,模式: {settings.mock and 'mock' or 'ros'}")
    app = create_app(settings, collector)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
