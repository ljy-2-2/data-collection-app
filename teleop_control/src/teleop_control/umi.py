from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from . import camera_devices
from .camera_brightness import read_brightness, set_brightness
from .camera_resolution import read_modes, choose_mode, ros_parameters, usb_connection_info
from .umi_preview import PREVIEW_FPS, PREVIEW_QUALITY, preview_size
from .models import (
    UmiDeviceConfigureRequest,
    UmiStartRequest,
    UmiStartResponse,
    UmiStatus,
)


ENCODER_LIMITS_PATH = Path("/etc/galaxis/encoder_limits.yaml")


def _read_encoder_minima(collection_profile: str) -> dict[str, float]:
    """Read confirmed raw-radian zero points; never substitute power-on zero."""
    sides = {
        "dual": ("left", "right"),
        "single-left": ("left",),
        "single-right": ("right",),
    }[collection_profile]
    try:
        document = yaml.safe_load(ENCODER_LIMITS_PATH.read_text(encoding="utf-8"))
        params = document["encoder_node"]["ros__parameters"]
        minima = {}
        for side in sides:
            value = params[f"{side}_min_raw"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("encoder minimum must be a number in radians")
            if not math.isfinite(value):
                raise ValueError("encoder minimum must be finite")
            minima[side] = float(value)
        return minima
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("夹爪零点标定缺失或无效，请重新确认所有启用夹爪的闭合最小值") from exc


def _mjpeg_chunk(payload: bytes) -> bytes:
    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
        + payload
        + b"\r\n"
    )


def _failure_detail(lines: list[str]) -> str:
    """Prefer actionable startup failures over later informational output."""
    details = [line.strip() for line in lines if line.strip()]
    for marker in ("env file ", "ERROR:", "Traceback (most recent call last)"):
        matches = [line for line in details if marker in line]
        if matches:
            return matches[-1]
    return next(
        (
            line
            for line in reversed(details)
            if not line.startswith(("[notice]", "WARNING: Running pip"))
        ),
        "",
    )


class _CameraPreviewWorker:
    """Own one V4L2 handle and fan its latest frame out to browser streams."""

    def __init__(self, device: str, on_idle, mode: dict | None = None) -> None:
        self.device = device
        try:
            info = Path(device).stat()
            self._device_generation = (info.st_ino, info.st_rdev)
        except OSError:
            self._device_generation = None
        self._on_idle = on_idle
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._capture = None
        self._subscribers = 0
        self._sequence = 0
        self._payload: bytes | None = None
        self._last_frame_at = 0.0
        self._mode = mode or dict(width=640, height=480, fps=30.0, fourcc="MJPG")
        self._revision = 0
        self.error = ""
        self._thread = threading.Thread(
            target=self._run,
            name=f"umi-camera-{Path(device).name}",
            daemon=True,
        )
        self._thread.start()

    def subscribe(self) -> _CameraPreviewSubscription:
        with self._condition:
            self._subscribers += 1
        return _CameraPreviewSubscription(self)

    def wait_for_frame(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._has_recent_frame_locked() and not self._stop.is_set() and not self.error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            return self._has_recent_frame_locked()

    def configure_mode(self, mode: dict) -> None:
        # Only the capture thread may release/reopen V4L2; current subscribers
        # stay connected and receive the first confirmed frame of the new mode.
        with self._condition:
            self._mode = dict(mode)
            self._revision += 1
            self._payload = None
            self._last_frame_at = 0.0
            self.error = ""
            self._condition.notify_all()

    def has_recent_frame(self) -> bool:
        with self._condition:
            return self._has_recent_frame_locked()

    def _has_recent_frame_locked(self) -> bool:
        return self._payload is not None and time.monotonic() - self._last_frame_at < 2.0

    def unsubscribe(self) -> None:
        idle = False
        with self._condition:
            if self._subscribers > 0:
                self._subscribers -= 1
            idle = self._subscribers == 0
            self._condition.notify_all()
        if idle:
            self._on_idle(self)

    def next_payload(self, sequence: int, closed: threading.Event) -> tuple[int, bytes]:
        with self._condition:
            while not self._stop.is_set() and not closed.is_set():
                if self._payload is not None and self._sequence != sequence:
                    return self._sequence, self._payload
                self._condition.wait(timeout=0.5)
        raise StopIteration

    def stop(self) -> None:
        self._stop.set()
        # OpenCV/V4L2 may segfault when one thread calls release() while the
        # capture thread is inside read().  Let _run observe the stop event and
        # release its own handle before collection takes ownership.
        with self._condition:
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        import cv2

        while not self._stop.is_set():
            if self._device_generation is not None:
                try:
                    info = Path(self.device).stat()
                    unchanged = (info.st_ino, info.st_rdev) == self._device_generation
                except OSError:
                    unchanged = False
                if not unchanged:
                    self._stop.set()
                    with self._condition:
                        self._condition.notify_all()
                    return
            with self._condition:
                mode, revision = dict(self._mode), self._revision
            capture = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
            self._capture = capture
            if not capture.isOpened():
                capture.release()
                self._capture = None
                if self._stop.wait(0.5):
                    break
                continue
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*mode['fourcc']))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, mode['width'])
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, mode['height'])
            capture.set(cv2.CAP_PROP_FPS, mode['fps'])
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            next_frame_at = 0.0
            while not self._stop.is_set() and revision == self._revision:
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                actual_fps = capture.get(cv2.CAP_PROP_FPS)
                if frame.shape[1] != mode['width'] or frame.shape[0] != mode['height']:
                    with self._condition:
                        if revision == self._revision:
                            self.error = (f"相机实际输出 {frame.shape[1]}×{frame.shape[0]} / {actual_fps:g} 帧，"
                                          f"未能应用 {mode['width']}×{mode['height']} / {mode['fps']:g} 帧")
                            self._condition.notify_all()
                    break
                now = time.monotonic()
                if now < next_frame_at:
                    continue
                # Advance the display deadline, rather than restarting it on
                # every accepted frame (which turns a 30 Hz input into 15 Hz).
                next_frame_at += 1.0 / PREVIEW_FPS
                if next_frame_at <= now:
                    next_frame_at = now + 1.0 / PREVIEW_FPS
                # Validate the negotiated capture size above, then resize only
                # this display copy. Never lower the device's recording mode
                # to make the hardware-check page cheaper to render.
                display_size = preview_size(frame.shape[1], frame.shape[0])
                display = (cv2.resize(frame, display_size, interpolation=cv2.INTER_LINEAR)
                           if display_size != (frame.shape[1], frame.shape[0]) else frame)
                encoded, jpeg = cv2.imencode(
                    ".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY]
                )
                if not encoded:
                    continue
                payload = jpeg.tobytes()
                with self._condition:
                    if revision != self._revision:
                        continue
                    self.error = ''
                    self._payload = payload
                    self._last_frame_at = now
                    self._sequence += 1
                    self._condition.notify_all()
            capture.release()
            if self._capture is capture:
                self._capture = None
            if not self._stop.is_set():
                self._stop.wait(0.25)


class _CameraPreviewSubscription:
    def __init__(self, worker: _CameraPreviewWorker) -> None:
        self._worker = worker
        self._closed = threading.Event()
        self._sequence = -1

    def __iter__(self) -> _CameraPreviewSubscription:
        return self

    def __next__(self) -> bytes:
        self._sequence, payload = self._worker.next_payload(
            self._sequence, self._closed
        )
        return _mjpeg_chunk(payload)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._worker.unsubscribe()


class _RecoveredProcess:
    """Minimal Popen-compatible handle for a collector recovered after restart."""

    def __init__(self, pid: int, expected_command: str) -> None:
        self.pid = pid
        self.expected_command = expected_command
        self.returncode: int | None = None

    def poll(self) -> int | None:
        try:
            command = (Path("/proc") / str(self.pid) / "cmdline").read_bytes()
        except OSError:
            self.returncode = 0
            return self.returncode
        if self.expected_command.encode() not in command.split(b"\0"):
            self.returncode = 0
            return self.returncode
        return None

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.expected_command, timeout)
            time.sleep(0.05)
        return int(self.returncode or 0)

    def terminate(self) -> None:
        os.killpg(self.pid, signal.SIGTERM)


class UmiManager:
    """Supervise the vendored Galaxis rosbag collector on the acquisition host."""

    def __init__(
        self,
        project_root: str | Path,
        data_root: str | Path,
        *,
        python: str,
        monitor_url: str = "http://127.0.0.1:8770",
        runtime_dir: str | Path,
        prepare_check: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.data_root = Path(data_root).expanduser().resolve()
        self.python = python
        self.monitor_url = monitor_url.rstrip("/")
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.prepare_check = prepare_check
        self._process: subprocess.Popen | None = None
        self._stopping = False
        self._session_id: str | None = None
        self._task: str | None = None
        self._record_format: str = "rosbag"
        self._collection_profile: str = "dual"
        self._started_at: datetime | None = None
        self._error = ""
        self._lock = threading.RLock()
        self._encoder_stop = threading.Event()
        self._encoder_threads: list[threading.Thread] = []
        self._encoder_ports: dict[str, object] = {}
        self._encoder_devices: tuple[str, ...] = ()
        self._encoder_values: dict[str, dict] = {}
        self._encoder_errors: dict[str, str] = {}
        self._encoder_started_at = 0.0
        self._encoder_preview_blockers: set[str] = set()
        self._configured_encoder_minima: tuple[str, dict[str, float]] | None = None
        self._stack_probe_at = 0.0
        self._external_stack_running = False
        self._docker_missing = False
        self._camera_workers: dict[str, _CameraPreviewWorker] = {}
        self._camera_control_lock = threading.RLock()
        self._camera_brightness_values: dict[str, int] = {}
        self._camera_modes: dict[str, dict] = {}
        self._camera_modes_file = self.runtime_dir / "camera-resolutions.json"
        try:
            saved = json.loads(self._camera_modes_file.read_text(encoding="utf-8"))
            self._saved_camera_modes = saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            self._saved_camera_modes = {}
        self._expected_camera_modes: dict[str, dict] = {}
        self._device_check: dict = {}
        self._camera_scan_at = 0.0
        self._camera_switching_until = 0.0
        self._camera_scan_lock = threading.Lock()
        self._camera_selection_file = self.project_root / "runtime-camera-selection" / "roles.json"
        self._camera_selection_file.parent.mkdir(parents=True, exist_ok=True)
        self._session_state_file = self.runtime_dir / "collection-session.json"
        self._restore_session_state()

    def _restore_session_state(self) -> None:
        """Recover ownership of a still-running collector after service restart."""
        try:
            saved = json.loads(self._session_state_file.read_text(encoding="utf-8"))
            pid = int(saved["pid"])
            command = str(saved["command"])
            process = _RecoveredProcess(pid, command)
            if process.poll() is not None:
                self._session_state_file.unlink(missing_ok=True)
                return
            self._process = process
            self._session_id = str(saved["session_id"])
            self._task = str(saved.get("task") or "Galaxis")
            self._collection_profile = str(saved.get("collection_profile") or "dual")
            self._expected_camera_modes = dict(saved.get('camera_modes') or {})
            self._record_format = str(saved.get("record_format") or "hdf5")
            started_at = saved.get("started_at")
            self._started_at = (
                datetime.fromisoformat(started_at) if started_at else None
            )
            self._encoder_preview_blockers.add("collection")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._session_state_file.unlink(missing_ok=True)

    def _save_session_state(self, command: str) -> None:
        pid = getattr(self._process, "pid", None)
        if not isinstance(pid, int):
            return
        temporary = self._session_state_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "command": command,
                    "session_id": self._session_id,
                    "task": self._task,
                    "record_format": self._record_format,
                    "collection_profile": self._collection_profile,
                    "camera_modes": self._expected_camera_modes,
                    "started_at": self._started_at.isoformat()
                    if self._started_at
                    else None,
                }
            ),
            encoding="utf-8",
        )
        temporary.replace(self._session_state_file)

    def _clear_session_state(self) -> None:
        self._session_state_file.unlink(missing_ok=True)

    def _monitor_state(self) -> tuple[bool, dict]:
        try:
            response = httpx.get(f"{self.monitor_url}/api/state", timeout=1.5)
            response.raise_for_status()
            payload = response.json()
            return True, payload if isinstance(payload, dict) else {}
        except Exception:
            return False, {}

    def _sync_external_stack(self) -> bool:
        """Do not preview devices owned by a collector outside this session."""
        now = time.monotonic()
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                return False
            probe_due = now - self._stack_probe_at >= 1.0
            occupied = self._external_stack_running
        if probe_due:
            try:
                result = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Running}}", "galaxis"],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
                self._docker_missing = False
                if result.returncode == 0:
                    occupied = result.stdout.strip() == "true"
            except FileNotFoundError:
                self._docker_missing = True
            except (OSError, subprocess.TimeoutExpired):
                pass
            with self._lock:
                self._stack_probe_at = now
                self._external_stack_running = occupied
        if occupied:
            with self._lock:
                already_blocked = "external-stack" in self._encoder_preview_blockers
            if not already_blocked:
                self._block_encoder_preview("external-stack")
                self._stop_camera_previews()
        else:
            self._unblock_encoder_preview("external-stack")
        return occupied

    def _stop_owned_stack(self) -> None:
        """Stop this App's Compose services, never another Galaxis installation."""
        running = self._owned_running_containers()
        if not running:
            with self._lock:
                self._stack_probe_at = 0.0
                self._external_stack_running = False
                self._device_check = {}
            return
        connected, monitor = self._monitor_state()
        if connected and monitor.get("recording"):
            raise RuntimeError("UMI 仍在录制，未停止采集服务以保护当前数据")
        self._wait_for_hdf5_finalizations()
        result = subprocess.run(
            # The exact names above have already passed the ownership check.
            # Stop them together; Compose's dependency order multiplies the
            # grace period while the operator is waiting to release devices.
            ["docker", "stop", "--time", "1", *running],
            capture_output=True, text=True, timeout=15.0, check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or "旧 Galaxis 服务停止失败")
        if still_running := self._owned_running_containers():
            raise RuntimeError(f"采集容器未退出: {', '.join(still_running)}")
        with self._lock:
            self._stack_probe_at = 0.0
            self._external_stack_running = False
            self._device_check = {}

    def _owned_running_containers(self) -> list[str]:
        expected_compose = str(self.project_root / "docker-compose.yml")
        running: list[str] = []
        for name in ("galaxis", "galaxis_core", "web_video_server", "galaxis_monitor"):
            try:
                inspect = subprocess.run(
                    [
                        "docker", "inspect", "--format",
                        "{{json .Config.Labels}}|{{.State.Running}}", name,
                    ],
                    capture_output=True, text=True, timeout=5.0, check=False,
                )
            except FileNotFoundError as exc:
                # A fresh desktop installation does not need Docker to show
                # the UI. Never waive cleanup when a collector/monitor may
                # still own hardware or an unfinished recording.
                with self._lock:
                    active = bool(self._session_id) or (
                        self._process is not None and self._process.poll() is None
                    )
                if active or self._monitor_state()[0]:
                    raise RuntimeError("找不到 Docker，无法确认采集容器已停止") from exc
                return []
            if inspect.returncode != 0:
                continue
            try:
                labels_text, running_text = inspect.stdout.strip().rsplit("|", 1)
                labels = json.loads(labels_text)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"无法验证 Galaxis 容器 {name} 的归属") from exc
            if (
                isinstance(labels, dict)
                and labels.get("com.docker.compose.project.config_files") == expected_compose
                and running_text == "true"
            ):
                running.append(name)
        return running

    def _wait_for_hdf5_finalizations(self) -> None:
        """Legacy HDF5 only; MP4 jobs survive independently of ROS shutdown."""
        if self._record_format != "hdf5":
            return
        deadline = time.monotonic() + 120.0
        while True:
            pending: list[Path] = []
            for staging in self.data_root.glob("hdf5/*/*/.rosbag_staging/metadata.yaml"):
                manifest = staging.parent.parent / "manifest.json"
                try:
                    complete = manifest.is_file() and json.loads(
                        manifest.read_text(encoding="utf-8")
                    ).get("state") == "complete"
                except (OSError, ValueError):
                    complete = False
                if not complete:
                    pending.append(staging)
            if not pending:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"仍有 {len(pending)} 轮 HDF5 未完成保存，采集服务保持运行以保护数据"
                )
            time.sleep(0.5)

    def shutdown_for_app(self) -> UmiStatus:
        """Save an active round and release owned hardware on App exit/start."""
        status = self.status()
        session_id = status.session_id
        if session_id:
            if not status.monitor_connected:
                raise RuntimeError("无法确认 UMI 录制状态，采集服务保持运行以保护数据")
            if status.recording:
                recording_root = self.data_root / ("rosbags" if self._record_format == "rosbag" else "hdf5")
                known_meta = {
                    path: path.stat().st_mtime_ns
                    for path in recording_root.glob("*/*/meta.json")
                }
                self.record("stop")
                saved_meta = [
                    path for path in recording_root.glob("*/*/meta.json")
                    if path not in known_meta
                    or path.stat().st_mtime_ns > known_meta[path]
                ]
                if not saved_meta:
                    raise RuntimeError("录制已停止但未找到本轮元数据，采集服务保持运行")
                newest_meta = max(saved_meta, key=lambda path: path.stat().st_mtime_ns)
                manifest = newest_meta.parent / "manifest.json"
                deadline = time.monotonic() + 120.0
                while time.monotonic() < deadline:
                    if self._record_format == "rosbag" and ((newest_meta.parent / "metadata.yaml").is_file()
                            or (newest_meta.parent / "media/video-manifest.json").is_file()):
                        break
                    if manifest.is_file():
                        try:
                            manifest_state = json.loads(
                                manifest.read_text(encoding="utf-8")
                            ).get("state")
                            if manifest_state == "complete":
                                break
                        except (OSError, ValueError):
                            pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError("本轮数据尚未完成保存，采集服务保持运行以保护数据")
            self._wait_for_hdf5_finalizations()
            self.stop(session_id)
            deadline = time.monotonic() + 45.0
            while time.monotonic() < deadline:
                with self._lock:
                    process = self._process
                    if process is None or process.poll() is not None:
                        break
                time.sleep(0.25)
            else:
                raise RuntimeError("UMI 采集进程未能安全退出")
        self._stop_owned_stack()
        self._stop_encoder_preview()
        self._stop_camera_previews()
        with self._lock:
            self._process = None
            self._session_id = None
            self._task = None
            self._started_at = None
            self._error = ""
            self._device_check = {}
            self._encoder_preview_blockers.clear()
            self._clear_session_state()
        return self.status()

    def _logs(self) -> list[str]:
        path = self.runtime_dir / "umi-collection.log"
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
        except OSError:
            return []

    @staticmethod
    def _usb_location(device: str) -> str:
        name = Path(device).name
        subsystem = "video4linux" if name.startswith("video") else "tty"
        path = (Path("/sys/class") / subsystem / name / "device").resolve()
        locations = [part for part in path.parts if re.fullmatch(r"\d+-[\d.]+", part)]
        return locations[-1] if locations else ""

    @staticmethod
    def _usb_hub(location: str) -> str:
        if "." in location:
            return location.rsplit(".", 1)[0]
        if "-" in location:
            return location.split("-", 1)[0]
        return location

    def _suggest_camera_roles(
        self,
        camera_details: list[dict],
        camera_sides: tuple[str, ...],
    ) -> dict[str, str]:
        available = [item["device"] for item in camera_details]
        hubs = {item["device"]: item["hub"] for item in camera_details}
        with self._lock:
            encoder_values = {
                side: dict(reading) for side, reading in self._encoder_values.items()
            }
        encoder_hubs = {
            side: self._usb_hub(self._usb_location(str(reading.get("device", ""))))
            for side, reading in encoder_values.items()
            if reading.get("device")
        }
        roles: dict[str, str] = {}
        used: set[str] = set()

        for side in ("left", "right"):
            if side not in camera_sides or not encoder_hubs.get(side):
                continue
            match = next(
                (
                    device
                    for device in available
                    if device not in used and hubs.get(device) == encoder_hubs[side]
                ),
                None,
            )
            if match:
                roles[side] = match
                used.add(match)

        if "center" in camera_sides:
            hand_hubs = {hub for hub in encoder_hubs.values() if hub}
            center = next(
                (
                    device
                    for device in available
                    if device not in used and hubs.get(device) not in hand_hubs
                ),
                None,
            )
            if center is None and not encoder_hubs:
                center = next(
                    (device for device in available if device not in used), None
                )
            if center:
                roles["center"] = center
                used.add(center)

        for side in ("left", "right"):
            if side not in camera_sides or side in roles:
                continue
            match = next((device for device in available if device not in used), None)
            if match:
                roles[side] = match
                used.add(match)
        return roles

    @staticmethod
    def _configured_camera_roles(
        camera_details: list[dict],
        camera_sides: tuple[str, ...],
        rules_path: Path = Path("/etc/udev/rules.d/99-galaxis-devices.rules"),
    ) -> dict[str, str]:
        """Resolve saved roles by USB topology, never by a stale /dev/videoN link."""
        try:
            rules = rules_path.read_text(encoding="utf-8")
        except OSError:
            rules = ""
        saved_ports: dict[str, str] = {}
        for line in rules.splitlines():
            role_match = re.search(
                r'SYMLINK\+="galaxis_(left|right|center)_camera"', line
            )
            path_match = re.search(
                r'ENV\{ID_PATH\}=="[^"]*usb-\d+:([\d.]+):\d+\.\d+"', line
            )
            if role_match and path_match:
                saved_ports[role_match.group(1)] = path_match.group(1)

        roles: dict[str, str] = {}
        used: set[str] = set()
        for side in camera_sides:
            saved_port = saved_ports.get(side)
            match = next(
                (
                    item["device"]
                    for item in camera_details
                    if item["device"] not in used
                    and str(item.get("usb_path", "")).partition("-")[2] == saved_port
                ),
                None,
            )
            if match:
                roles[side] = match
                used.add(match)

        # Compatibility for installations configured before stable udev rules
        # were written. Reject duplicate/stale targets instead of presenting
        # the same physical camera in two role cards.
        for side in camera_sides:
            if side in roles or side in saved_ports:
                continue
            link = Path("/dev") / f"galaxis_{side}_camera"
            if not link.is_symlink():
                continue
            resolved = str(link.resolve())
            if resolved in used or not any(
                item["device"] == resolved for item in camera_details
            ):
                continue
            roles[side] = resolved
            used.add(resolved)
        return roles

    def _write_camera_selection(self, roles: dict) -> None:
        temporary = self._camera_selection_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(roles, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self._camera_selection_file)

    def refresh_camera_devices(self, *, force=False) -> dict:
        """Lightweight hotplug scan: no PICO restart, encoder reset or stream open."""
        if not self._device_check and not force:
            return {}
        if not self._camera_scan_lock.acquire(blocking=False):
            return dict(self._device_check)
        try:
            if not force and time.monotonic()-self._camera_scan_at < 2.0:
                return dict(self._device_check)
            self._camera_scan_at = time.monotonic()
            details = camera_devices.discover()
            candidates = [item['device'] for item in details]
            saved = camera_devices.read_selection(self._camera_selection_file)
            matched = camera_devices.resolve_roles(saved, details)
            active = {'dual': ('left','right','center'), 'single-left': ('left','center'),
                      'single-right': ('right','center')}[self._collection_profile]
            with self._camera_control_lock, self._lock:
                previous = {d['device']: d for d in self._device_check.get('camera_candidate_details', [])}
                current = {d['device']: d for d in details}
                for device in list(previous):
                    if previous[device].get('generation') != current.get(device, {}).get('generation'):
                        worker = self._camera_workers.pop(device, None)
                        if worker:
                            worker.stop()
                        self._camera_modes.pop(device, None)
                        self._camera_brightness_values.pop(device, None)
                roles = ({role: d['device'] for role, d in matched.items()} if saved else
                         self._configured_camera_roles(details, active))
                self._device_check = {**self._device_check,
                    'camera_candidates': candidates, 'camera_candidate_details': details,
                    'camera_roles': roles, 'suggested_camera_roles': self._suggest_camera_roles(details, active),
                    'hardware_ready': len(candidates) >= len(active)
                        and len(self._device_check.get('encoder_candidates', [])) >= len(active)-1
                        and not self._device_check.get('stack_occupied', False)}
                return dict(self._device_check)
        finally:
            self._camera_scan_lock.release()

    def select_live_camera(self, role: str, device: str) -> UmiStatus:
        """Replace one role between episodes without restarting the collection task."""
        with self._camera_control_lock:
            status = self.status()
            if status.state not in ('ready', 'starting') or not status.monitor_connected:
                raise RuntimeError('请等待采集服务连接后再选择相机')
            if status.recording:
                raise RuntimeError('请先保存当前片段，再切换相机')
            mode = self._expected_camera_modes.get(role)
            if mode is None:
                raise ValueError('该相机未启用')
            check = self.refresh_camera_devices(force=True)
            item = next((d for d in check['camera_candidate_details'] if d['device'] == device), None)
            if item is None:
                raise ValueError('该相机已断开，设备列表正在自动更新')
            saved = camera_devices.read_selection(self._camera_selection_file)
            resolved = camera_devices.resolve_roles(saved, check['camera_candidate_details'])
            if any(r != role and d['device'] == device for r, d in resolved.items()):
                raise ValueError('该设备已分配给其他相机位置，请先选择未使用的设备')
            modes = read_modes(device)
            if not any(all(m[k] == mode[k] for k in ('width','height','fourcc')) for m in modes):
                raise ValueError('该相机不支持本任务的分辨率和格式，请返回检查硬件修改配置')
            fresh = next((d for d in camera_devices.discover() if d['device'] == device), None)
            if fresh is None or fresh['generation'] != item['generation']:
                raise RuntimeError('相机刚刚重新连接，请从更新后的设备列表选择')
            # Recheck after hardware probing, before publishing the new identity.
            connected, monitor = self._monitor_state()
            if not connected or monitor.get('recording'):
                raise RuntimeError('正在录制或服务未连接，不能切换相机')
            self._write_camera_selection({**saved, role: {**camera_devices.identity(item),
                **({'brightness': saved[role]['brightness']} if 'brightness' in saved.get(role, {}) else {})}})
            self._camera_scan_at = 0.0
            self._camera_switching_until = time.monotonic()+6.0
        return self.status()

    def status(self) -> UmiStatus:
        # The collector can outlive the control service (or its in-memory
        # handle). Recover the exact saved PID before deciding that live ROS
        # telemetry belongs to an idle session.
        with self._lock:
            if self._process is None and self._session_state_file.is_file():
                self._restore_session_state()
        self.refresh_camera_devices()
        connected, monitor = self._monitor_state()
        # Starting a session holds UploadTargets.lock before entering UMI.
        # Snapshot its queue outside the UMI lock to avoid the reverse order
        # when UI status polling races a start/retry request.
        queue = self.upload_queue()
        with self._lock:
            if self._device_check:
                # The live collector monitor is offline before a session starts.
                # Keep the latest preflight result available to status polling so
                # the UI does not jump back to 0/0 immediately after a check.
                monitor = {**monitor, "device_check": dict(self._device_check)}
            process = self._process
            running = process is not None and process.poll() is None
            if process is not None and not running:
                self._encoder_preview_blockers.discard("collection")
            if (
                process is not None
                and not running
                and process.returncode
                and not self._error
                and not self._stopping
            ):
                detail = _failure_detail(self._logs())
                self._error = f"采集程序已退出（{process.returncode}）"
                if detail:
                    self._error += f"：{detail}"
            elif process is not None and not running and not self._stopping and not self._error:
                self._error = "采集程序已退出，请结束返回以清理服务"
            if self._stopping:
                state = "stopping"
            elif self._error:
                state = "error"
            elif running and monitor.get("recording"):
                state = "recording"
            elif running and connected and self._monitor_ready(monitor):
                state = "ready"
            elif running:
                state = "starting"
            else:
                state = "idle"
            pending, imported = queue['pending'], queue['uploaded']
            return UmiStatus(
                state=state,
                message=self._error or (self._live_readiness_message(monitor)
                    if running else ''),
                session_id=self._session_id,
                task=self._task,
                record_format=self._record_format,
                collection_profile=self._collection_profile,
                started_at=self._started_at,
                monitor_connected=connected,
                recording=bool(monitor.get("recording")),
                episodes_completed=int(monitor.get("episodes_completed_total") or 0),
                pending_imports=pending,
                pending_processing=queue['processing'],
                failed_processing=queue['failed_processing'],
                imported_episodes=imported,
                upload_queue=queue,
                monitor=monitor,
                log_tail=self._logs(),
            )

    def _live_readiness_message(self, monitor: dict) -> str:
        issues = list(monitor.get('camera_quality_issues', []))
        # Only diagnose devices once the monitor has published their state.
        # A temporary dropout keeps the other previews and telemetry visible.
        if monitor.get('vr'):
            sides = {'single-left': ('hmd', 'left'), 'single-right': ('hmd', 'right')}.get(
                self._collection_profile, ('hmd', 'left', 'right'))
            labels = {'hmd': '头盔', 'left': '左手柄', 'right': '右手柄'}
            missing = [labels[side] for side in sides
                       if not monitor['vr'].get(side, {}).get('online')]
            if missing:
                issues.append('等待 PICO ' + '、'.join(missing) + '实时数据，请确认 Head、Controllers、Send Data 已开启')
            untracked = [labels[side] for side in sides
                         if monitor['vr'].get(side, {}).get('online') and
                         (monitor['vr'][side].get('tracked') is False or
                          monitor['vr'][side].get('valid') is False)]
            if untracked:
                issues.append('PICO ' + '、'.join(untracked) + '追踪无效，请唤醒头显并恢复追踪')
        return '；'.join(issues)

    def _monitor_ready(self, monitor: dict) -> bool:
        """Return true only when the complete live collection path is usable."""
        if time.monotonic() < self._camera_switching_until:
            return False
        if self._expected_camera_modes:
            actual = {c.get('key'): c for c in monitor.get('cameras', []) if isinstance(c, dict)}
            if monitor.get('recording_gate_level', 2) != 0:
                return False
            if any(actual.get(role, {}).get('width') != mode['width']
                   or actual.get(role, {}).get('height') != mode['height']
                   for role, mode in self._expected_camera_modes.items()):
                return False
        expected_cameras, expected_arms = {
            "dual": ({"left", "right", "center"}, {"left", "right"}),
            "single-left": ({"left", "center"}, {"left"}),
            "single-right": ({"right", "center"}, {"right"}),
        }[self._collection_profile]
        camera_keys = {
            item.get("key")
            for item in monitor.get("cameras", [])
            if isinstance(item, dict)
        }
        encoders = monitor.get("encoders", {})
        vr = monitor.get("vr", {})
        return (
            str(monitor.get("task_status", "")).startswith("received_task:")
            and expected_cameras.issubset(camera_keys)
            and all(
                bool(encoders.get(side, {}).get("online"))
                and float(encoders.get(side, {}).get("rate_hz") or 0) > 0
                for side in expected_arms
            )
            and all(
                bool(vr.get(side, {}).get("online"))
                and float(vr.get(side, {}).get("rate_hz") or 0) > 0
                and vr.get(side, {}).get("tracked") is not False
                and vr.get(side, {}).get("valid") is not False
                for side in ("hmd", *expected_arms)
            )
        )

    def check(self, collection_profile: str = "dual") -> UmiStatus:
        # The collection command reports its startup failure once, then the
        # preflight page owns the devices again.  A manual "重新检测" must start
        # from a clean session instead of preserving an error/process handle
        # that can keep previews suspended or the old alert pinned forever.
        with self._lock:
            if self._process is None and self._session_state_file.is_file():
                self._restore_session_state()
            ended_process = (
                self._process
                if self._process is not None and self._process.poll() is not None
                else None
            )
        if ended_process is not None:
            self._stop_owned_stack()
            self._finish_stopped_process(ended_process)
        expected = {
            "dual": (("left", "right", "center"), ("left", "right")),
            "single-left": (("left", "center"), ("left",)),
            "single-right": (("right", "center"), ("right",)),
        }
        if collection_profile not in expected:
            raise ValueError("unsupported UMI collection profile")
        stack_occupied = self._sync_external_stack()
        if self._docker_missing:
            raise RuntimeError(
                "未安装 Docker，UMI 采集暂不可用。请安装 Docker 和 Compose 插件；"
                "App 登录及其他采集页面仍可使用。"
            )
        camera_sides, encoder_sides = expected[collection_profile]
        profile_text = ""
        try:
            profile_text = Path("/etc/galaxis/collection_profile.yaml").read_text(
                encoding="utf-8"
            )
        except OSError:
            pass
        camera_details = camera_devices.discover()
        camera_candidates = [item["device"] for item in camera_details]
        encoder_candidates = sorted(
            {
                str(path)
                for pattern in ("ttyACM*", "ttyUSB*")
                for path in Path("/dev").glob(pattern)
            }
        )
        with self._lock:
            encoder_values = {
                side: dict(reading) for side, reading in self._encoder_values.items()
            }
            encoder_errors = dict(self._encoder_errors)
        encoder_details = []
        for device in encoder_candidates:
            location = self._usb_location(device)
            detected_side = next(
                (
                    side
                    for side, reading in encoder_values.items()
                    if reading.get("device") == device
                ),
                None,
            )
            encoder_details.append(
                {
                    "device": device,
                    "stable_id": f"usb:{location}" if location else device,
                    "usb_path": location,
                    "hub": self._usb_hub(location),
                    "detected_side": detected_side,
                    "error": (
                        "已有 Galaxis 采集容器占用串口"
                        if stack_occupied else encoder_errors.get(device, "")
                    ),
                }
            )
        saved_roles = camera_devices.resolve_roles(camera_devices.read_selection(self._camera_selection_file), camera_details)
        camera_roles = ({role: item["device"] for role, item in saved_roles.items()}
                        or self._configured_camera_roles(camera_details, camera_sides))
        camera_device_statuses = [
            {
                "key": side,
                "connected": (
                    side in camera_roles
                    and (Path("/dev") / f"galaxis_{side}_camera").exists()
                    and str((Path("/dev") / f"galaxis_{side}_camera").resolve())
                    == camera_roles[side]
                )
                or (side == "center" and "g2_head_color" in profile_text),
            }
            for side in camera_sides
        ]
        suggested_camera_roles = self._suggest_camera_roles(
            camera_details, camera_sides
        )
        encoder_devices = [
            {
                "key": side,
                "connected": (Path("/dev") / f"galaxis_{side}_esp32c6").exists(),
            }
            for side in encoder_sides
        ]
        cameras = [item for item in camera_device_statuses if item["connected"]]
        encoders = [item for item in encoder_devices if item["connected"]]
        service_check: dict[str, object] = {}
        if self.prepare_check is not None:
            try:
                service_check = dict(self.prepare_check())
            except Exception as exc:
                service_check = {
                    "vr_service_ready": False,
                    "vr_service_detail": f"Galaxis VR 检查失败：{exc}",
                    "pico_ready": False,
                    "pico_detail": f"PICO 准备失败：{exc}",
                }
        roles_ready = len(cameras) == len(camera_sides) and len(encoders) == len(
            encoder_sides
        )
        # The legacy teleop service is deliberately not started for UMI.
        # Preflight opens the headset app if needed; the collector starts and
        # verifies its own live ROS bridge after entering monitoring.
        vr_ready = (
            bool(service_check.get("pico_ready"))
            if self.prepare_check is not None
            else True
        )
        status = self.status()
        device_check = {
            "ready": roles_ready and vr_ready and not stack_occupied,
            "hardware_ready": (
                len(camera_candidates) >= len(camera_sides)
                and len(encoder_candidates) >= len(encoder_sides)
                and not stack_occupied
            ),
            "stack_occupied": stack_occupied,
            "roles_configured": roles_ready,
            "cameras": len(cameras),
            "expected_cameras": len(camera_sides),
            "encoders": len(encoders),
            "expected_encoders": len(encoder_sides),
            "camera_devices": camera_device_statuses,
            "camera_roles": camera_roles,
            "encoder_devices": encoder_devices,
            "camera_candidates": camera_candidates,
            "camera_candidate_details": camera_details,
            "suggested_camera_roles": suggested_camera_roles,
            "encoder_candidates": encoder_candidates,
            "encoder_candidate_details": encoder_details,
            **service_check,
        }
        with self._lock:
            self._collection_profile = collection_profile
            self._device_check = device_check
        status.collection_profile = collection_profile
        status.monitor = {**status.monitor, "device_check": device_check}
        return status

    def _brightness_device(self, camera_key: str) -> str:
        with self._lock:
            collecting = self._process is not None and self._process.poll() is None
            if camera_key in ("left", "center", "right"):
                active = {"center"} | {
                    "dual": {"left", "right"},
                    "single-left": {"left"},
                    "single-right": {"right"},
                }[self._collection_profile]
                if not collecting or self._stopping or camera_key not in active:
                    raise ValueError("该相机未在当前采集模式中启用")
                selected = self._device_check.get("camera_roles", {}).get(camera_key)
                return selected or f"/dev/galaxis_{camera_key}_camera"
            if not re.fullmatch(r"video\d+", camera_key):
                raise ValueError("camera not found")
            device = f"/dev/{camera_key}"
            if device not in self._device_check.get("camera_candidates", []):
                raise ValueError("camera not found")
            if collecting or self._encoder_preview_blockers:
                raise RuntimeError("正在交接采集设备，请稍后调节亮度")
            return device

    def camera_brightness(self, camera_key: str, value: int | None = None) -> dict:
        # V4L2 controls do not reopen video capture or transform preview pixels:
        # both the live stream and the recorder see the actual hardware change.
        with self._camera_control_lock:
            device = self._brightness_device(camera_key)
            control = read_brightness(device) if value is None else set_brightness(device, value)
            key = str(Path(device).resolve())
            if control["supported"]:
                self._camera_brightness_values[key] = control["value"]
                if value is not None and camera_key in ('left','center','right'):
                    saved = camera_devices.read_selection(self._camera_selection_file)
                    if camera_key in saved:
                        saved[camera_key]['brightness'] = control['value']
                        self._write_camera_selection(saved)
            else:
                self._camera_brightness_values.pop(key, None)
            return control

    def _selected_camera_mode(self, device: str) -> tuple[list[dict], dict, str]:
        modes = read_modes(device)
        properties = self._udev_properties(device)
        identity_fields = {key: properties.get(key, '') for key in
                           ('ID_VENDOR_ID', 'ID_MODEL_ID', 'ID_SERIAL_SHORT', 'ID_PATH')}
        legacy_identity = json.dumps(identity_fields, sort_keys=True)
        details = self._device_check.get('camera_candidate_details', [])
        serial = properties.get('ID_SERIAL_SHORT')
        if serial and sum(d.get('serial') == serial and d.get('vendor') == properties.get('ID_VENDOR_ID')
                          and d.get('product') == properties.get('ID_MODEL_ID') for d in details) == 1:
            identity_fields.pop('ID_PATH')
        identity = json.dumps(identity_fields, sort_keys=True)
        selected = choose_mode(modes, self._saved_camera_modes.get(identity, self._saved_camera_modes.get(legacy_identity)))
        self._camera_modes[device] = selected
        return modes, selected, identity

    def camera_resolution(self, camera_key: str, mode_id: str | None = None) -> dict:
        with self._camera_control_lock:
            if camera_key in ('left', 'center', 'right'):
                if mode_id is not None:
                    raise RuntimeError('请先结束采集服务，再在检查硬件页修改分辨率')
                device = self._brightness_device(camera_key)  # Verify the active role.
                mode = self._expected_camera_modes.get(camera_key)
                return dict(options=[], selected=mode, editable=False,
                            connection=usb_connection_info(device),
                            message='采集期间分辨率已锁定；修改请返回检查硬件')
            device = self._brightness_device(camera_key)
            modes, previous, identity = self._selected_camera_mode(device)
            if mode_id is None:
                return dict(options=modes, selected=previous, editable=True,
                            connection=usb_connection_info(device))
            mode = next((item for item in modes if item['id'] == mode_id), None)
            if mode is None:
                raise ValueError('相机不支持所选的 30 帧分辨率，请重新读取')
            # Hold the lifecycle lock until readback completes: configuring or
            # starting collection cannot race this camera reopen.
            with self._lock:
                self._brightness_device(camera_key)
                worker = self._camera_workers.get(device)
                temporary = worker is None
                if temporary:
                    worker = _CameraPreviewWorker(device, lambda _: None, mode)
                else:
                    worker.configure_mode(mode)
                try:
                    if not worker.wait_for_frame(8.0):
                        raise RuntimeError(worker.error or '所选分辨率未能出图，请选择较低分辨率或检查 USB 连接')
                    brightness = self._camera_brightness_values.get(str(Path(device).resolve()))
                    if brightness is not None:
                        set_brightness(device, brightness)
                    updated = {**self._saved_camera_modes, identity: mode}
                    tmp = self._camera_modes_file.with_suffix('.tmp')
                    tmp.write_text(json.dumps(updated, ensure_ascii=False), encoding='utf-8')
                    tmp.replace(self._camera_modes_file)
                    self._saved_camera_modes = updated
                    self._camera_modes[device] = dict(mode)
                except Exception:
                    if not temporary:
                        worker.configure_mode(previous)
                    raise
                finally:
                    if temporary:
                        worker.stop()
            return dict(options=modes, selected=mode, editable=True,
                        connection=usb_connection_info(device))

    def candidate_camera_frame(self, camera_key: str) -> bytes | None:
        if not re.fullmatch(r"video\d+", camera_key):
            raise ValueError("camera not found")
        device = f"/dev/{camera_key}"
        with self._lock:
            candidates = self._device_check.get("camera_candidates", [])
        if device not in candidates:
            raise ValueError("camera not found")
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return None

        stream = self.candidate_camera_stream(camera_key)
        if stream is None:
            return None
        try:
            return next(stream)
        finally:
            stream.close()

    def candidate_camera_stream(self, camera_key: str) -> Iterator[bytes] | None:
        if not re.fullmatch(r"video\d+", camera_key):
            raise ValueError("camera not found")
        device = f"/dev/{camera_key}"
        with self._lock:
            candidates = self._device_check.get("camera_candidates", [])
        if device not in candidates:
            raise ValueError("camera not found")
        try:
            import cv2  # noqa: F401 - fail in the request, not in the background thread
        except ImportError as exc:
            raise RuntimeError("相机预览缺少 OpenCV，请重新运行 install-app.sh") from exc
        with self._camera_control_lock, self._lock:
            if self._encoder_preview_blockers or (
                self._process is not None and self._process.poll() is None
            ):
                return None
            worker = self._camera_workers.get(device)
            if worker is None:
                _, mode, _ = self._selected_camera_mode(device)
                worker = _CameraPreviewWorker(device, self._camera_worker_idle, mode)
                self._camera_workers[device] = worker
            subscription = worker.subscribe()
        if not worker.wait_for_frame(timeout=5.0):
            subscription.close()
            raise RuntimeError(worker.error or f"{device} 未能读取到相机画面，请检查相机是否被占用或支持视频采集")
        return subscription

    def candidate_camera_ready(self, camera_key: str) -> bool:
        if not re.fullmatch(r"video\d+", camera_key):
            raise ValueError("camera not found")
        device = f"/dev/{camera_key}"
        with self._lock:
            if device not in self._device_check.get("camera_candidates", []):
                raise ValueError("camera not found")
            worker = self._camera_workers.get(device)
        return worker.has_recent_frame() if worker is not None else False

    def _camera_worker_idle(self, worker: _CameraPreviewWorker) -> None:
        with self._lock:
            if self._camera_workers.get(worker.device) is not worker:
                return
            self._camera_workers.pop(worker.device, None)
        worker.stop()

    def _stop_camera_previews(self) -> None:
        with self._lock:
            workers = list(self._camera_workers.values())
            self._camera_workers = {}
        for worker in workers:
            worker.stop()

    def _read_encoder_preview(self, device: str, stop: threading.Event) -> None:
        port = None
        try:
            import serial

            port = serial.Serial(port=None, baudrate=115200, timeout=0.1)
            port.dtr = False
            port.rts = False
            port.port = device
            port.open()
            with self._lock:
                if stop.is_set():
                    port.close()
                    return
                self._encoder_ports[device] = port
            try:
                port.reset_input_buffer()
            except (AttributeError, OSError):
                pass
            while not stop.is_set():
                line = port.readline().decode("utf-8", errors="ignore").strip()
                match = re.match(
                    r"^([LRlr])[^,]*,\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$",
                    line,
                )
                if not match:
                    continue
                value = float(match.group(2))
                if not math.isfinite(value):
                    continue
                side = "left" if match.group(1).lower() == "l" else "right"
                with self._lock:
                    self._encoder_values[side] = {
                        "device": device,
                        "value": value,
                        "updated_at": time.time(),
                    }
                    self._encoder_errors.pop(device, None)
        except Exception as exc:
            if not stop.is_set():
                with self._lock:
                    self._encoder_errors[device] = f"{type(exc).__name__}: {exc}"
        finally:
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
            with self._lock:
                if self._encoder_ports.get(device) is port:
                    self._encoder_ports.pop(device, None)

    def _stop_encoder_preview(self) -> None:
        with self._lock:
            stop = self._encoder_stop
            threads = list(self._encoder_threads)
            ports = list(self._encoder_ports.values())
            stop.set()
        # Match the desktop setup tool: close descriptors first so a blocked
        # readline wakes immediately, then wait until serial ownership is gone.
        for port in ports:
            try:
                port.close()
            except Exception:
                pass
        for thread in threads:
            thread.join(timeout=2.0)
        with self._lock:
            self._encoder_threads = []
            self._encoder_ports = {}
            self._encoder_devices = ()

    def _block_encoder_preview(self, owner: str) -> None:
        with self._lock:
            self._encoder_preview_blockers.add(owner)
        self._stop_encoder_preview()

    def _unblock_encoder_preview(self, owner: str) -> None:
        with self._lock:
            self._encoder_preview_blockers.discard(owner)

    def _ensure_encoder_preview(self) -> None:
        devices = tuple(
            sorted(
                {
                    str(path)
                    for pattern in ("ttyACM*", "ttyUSB*")
                    for path in Path("/dev").glob(pattern)
                }
            )
        )
        now = time.monotonic()
        with self._lock:
            if self._encoder_preview_blockers:
                return
            running = any(thread.is_alive() for thread in self._encoder_threads)
            if devices == self._encoder_devices and (
                running or now - self._encoder_started_at < 2.0
            ):
                return
        self._stop_encoder_preview()
        stop = threading.Event()
        threads = [
            threading.Thread(
                target=self._read_encoder_preview,
                args=(device, stop),
                name=f"umi-encoder-{Path(device).name}",
                daemon=True,
            )
            for device in devices
        ]
        with self._lock:
            if self._encoder_preview_blockers:
                return
            self._encoder_stop = stop
            self._encoder_devices = devices
            self._encoder_values = {}
            self._encoder_errors = {}
            self._encoder_threads = threads
            self._encoder_started_at = now
            # Register and start atomically with respect to a collection
            # handoff, so _stop_encoder_preview never observes unstarted
            # threads and the collector can only proceed after they join.
            for thread in threads:
                thread.start()

    def encoder_preview(self, collection_profile: str = "dual") -> dict:
        active_sides = {
            "dual": ("left", "right"),
            "single-left": ("left",),
            "single-right": ("right",),
        }.get(collection_profile)
        if active_sides is None:
            raise ValueError("unsupported UMI collection profile")
        self._sync_external_stack()
        self._ensure_encoder_preview()
        now = time.time()
        with self._lock:
            values = {side: dict(value) for side, value in self._encoder_values.items()}
            errors = dict(self._encoder_errors)
            devices = self._encoder_devices
            blockers = set(self._encoder_preview_blockers)
        encoders = {}
        for side in active_sides:
            reading = values.get(side, {})
            updated_at = reading.get("updated_at")
            encoders[side] = {
                "device": reading.get("device", ""),
                "online": isinstance(updated_at, (int, float))
                and now - updated_at < 1.0,
                "value": reading.get("value"),
                "updated_at": updated_at,
                "error": ""
                if reading
                else "; ".join(
                    f"{Path(device).name}: {errors[device]}"
                    for device in devices
                    if device in errors
                ),
            }
        suspended = bool(blockers)
        if suspended:
            message = (
                "已有 Galaxis 采集服务占用串口，请先结束旧采集服务"
                if "external-stack" in blockers
                else "采集程序正在独占串口"
                if "collection" in blockers
                else "正在交接串口"
            )
            for reading in encoders.values():
                reading["online"] = False
                reading["error"] = message
        else:
            message = ""
        return {
            "checked_at": now,
            "encoders": encoders,
            "preview_suspended": suspended,
            "message": message,
        }

    def record(self, action: str) -> UmiStatus:
        from .collection_gate import collection_available
        if action == "start" and not collection_available(getattr(self, 'collection_gate_path', None)):
            return self.status()
        if action not in {"start", "stop", "cancel"}:
            raise ValueError("unsupported UMI recording action")
        with self._lock:
            process = self._process
            session_active = (
                self._session_id is not None
                and process is not None
                and process.poll() is None
            )
        if not session_active:
            raise RuntimeError("UMI 回合操作失败: 请先启动 UMI 采集服务")
        monitor_connected, monitor = self._monitor_state()
        if not monitor_connected:
            raise RuntimeError(
                "UMI 回合操作失败: 采集服务正在启动，请等待状态变为“已就绪”"
            )
        if action == "start" and not self._monitor_ready(monitor):
            raise RuntimeError("UMI 回合操作失败: 相机、编码器或 PICO 实时数据尚未就绪")
        try:
            response = httpx.post(
                f"{self.monitor_url}/api/record/{action}",
                timeout={"start": 45.0, "stop": 45.0, "cancel": 12.0}[action],
            )
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and not result.get("success", True):
                raise RuntimeError(result.get("message") or "录制安全条件未满足")
        except httpx.HTTPStatusError as exc:
            try:
                detail = str(exc.response.json().get("detail") or "")
            except Exception:  # noqa: BLE001
                detail = ""
            raise RuntimeError(f"UMI 回合操作失败: {detail or exc}") from exc
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"UMI 回合操作失败: {exc}") from exc
        return self.status()

    @staticmethod
    def _udev_properties(device: str) -> dict[str, str]:
        result = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={device}"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"无法读取设备属性: {device}")
        properties = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in result.stdout.splitlines()
            if "=" in line
        }
        required = {"ID_VENDOR_ID", "ID_MODEL_ID", "ID_PATH"}
        if not required.issubset(properties):
            raise RuntimeError(f"设备缺少稳定 USB 标识: {device}")
        return properties

    def configure(self, request: UmiDeviceConfigureRequest) -> UmiStatus:
        with self._lock:
            if self._stopping or (self._process is not None and self._process.poll() is None):
                raise RuntimeError("请先结束 UMI 采集服务，再重新标定夹爪零点")
        active_arms = {
            "dual": {"left", "right"},
            "single-left": {"left"},
            "single-right": {"right"},
        }[request.collection_profile]
        if request.encoder_minima and set(request.encoder_minima) != active_arms:
            raise ValueError("请分别确认所有已启用夹爪的闭合最小值")
        if any(not math.isfinite(value) for value in request.encoder_minima.values()):
            raise ValueError("夹爪闭合最小值无效")
        if not request.encoder_minima and not request.capture_encoder_zero:
            raise ValueError("请先确认已启用夹爪的闭合最小值")
        self._block_encoder_preview("configure")
        try:
            return self._configure_devices(request)
        finally:
            self._unblock_encoder_preview("configure")

    def _configure_devices(self, request: UmiDeviceConfigureRequest) -> UmiStatus:
        current = self.check(request.collection_profile)
        check = current.monitor.get("device_check", {})
        if not check.get("hardware_ready") or (
            self.prepare_check is not None and not check.get("pico_ready")
        ):
            raise RuntimeError("相机、编码器或 PICO 未就绪，请重新检查硬件")
        candidates = set(check.get("camera_candidates", []))
        expected_roles = {
            "dual": {"left", "right", "center"},
            "single-left": {"left", "center"},
            "single-right": {"right", "center"},
        }[request.collection_profile]
        roles = dict(request.camera_roles)
        if set(roles) != expected_roles:
            raise ValueError("相机角色与所选采集硬件不一致")
        if len(set(roles.values())) != len(roles):
            raise ValueError("左、右、中心相机不能选择同一个设备")
        if not all(path in candidates for path in roles.values()):
            raise ValueError("相机选择已失效，请重新检查设备")
        if len(check.get("encoder_candidates", [])) < len(expected_roles - {"center"}):
            raise ValueError("编码器数量不足")

        helper = self.project_root / "tools" / "web_device_config.py"
        if not helper.is_file():
            raise RuntimeError(f"UMI 设备配置程序不存在: {helper}。若运行期间移动或重命名了 App 文件夹，请从新目录重新启动 App。")
        # Carry the hardware settings seen/adjusted in preview into the existing
        # ROS brightness controllers, which otherwise reset every camera to 0.
        camera_brightness = {}
        camera_modes = {}
        with self._camera_control_lock:
            for role, device in roles.items():
                _, mode, _ = self._selected_camera_mode(device)
                camera_modes[role] = mode
                key = str(Path(device).resolve())
                if key in self._camera_brightness_values:
                    # Preserve the confirmed readback even if closing preview
                    # makes a camera driver reset its controls during handoff.
                    camera_brightness[role] = self._camera_brightness_values[key]
        payload_path = self.runtime_dir / "device-selection.json"
        payload_path.write_text(
            json.dumps(
                {
                    "collection_profile": request.collection_profile,
                    "camera_roles": roles,
                    "camera_brightness": camera_brightness,
                    "camera_modes": {role: ros_parameters(mode) for role, mode in camera_modes.items()},
                    "encoder_minima": request.encoder_minima,
                    "device_properties": {
                        path: self._udev_properties(path)
                        for path in sorted(
                            set(roles.values()) | set(check["encoder_candidates"])
                        )
                    },
                }
            ),
            encoding="utf-8",
        )
        command = [
            "docker",
            "run",
            "--rm",
            "--privileged",
            "--entrypoint",
            "python3",
            "-v",
            "/dev:/dev",
            "-v",
            "/sys:/sys:ro",
            "-v",
            "/run/udev:/run/udev",
            "-v",
            "/etc/galaxis:/etc/galaxis",
            "-v",
            "/etc/udev/rules.d:/etc/udev/rules.d",
            "-v",
            f"{helper}:/opt/web_device_config.py:ro",
            "-v",
            f"{payload_path}:/opt/device-selection.json:ro",
            self._collection_image(),
            "/opt/web_device_config.py",
            "/opt/device-selection.json",
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=45.0, check=False
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise RuntimeError(detail[-1] if detail else "UMI 设备角色保存失败")
        minima = _read_encoder_minima(request.collection_profile)
        if request.encoder_minima and minima != request.encoder_minima:
            raise RuntimeError("保存的夹爪零点与确认的最小值不一致，请重新标定")
        # A successful preflight configuration starts a fresh attempt.  Do not
        # keep showing the previous session's startup error on the camera
        # confirmation page.
        details = {item["device"]: item for item in check["camera_candidate_details"]}
        self._write_camera_selection({role: {**camera_devices.identity(details[device]),
            **({'brightness': camera_brightness[role]} if role in camera_brightness else {})} for role, device in roles.items()})
        with self._lock:
            self._expected_camera_modes = camera_modes
            self._configured_encoder_minima = (request.collection_profile, minima)
            self._error = ""
            self._process = None
            self._session_id = None
            self._started_at = None
        configured = self.check(request.collection_profile)
        if not configured.monitor.get("device_check", {}).get("ready"):
            raise RuntimeError("角色配置已写入，但设备链接尚未生效，请重新插拔设备")
        return configured

    def _collection_image(self) -> str:
        manifest = self.project_root / "umi-runtime.json"
        if manifest.is_file():
            return str(json.loads(manifest.read_text())["image"])
        return "galaxis:latest"

    def start(self, request: UmiStartRequest) -> UmiStartResponse:
        script = self.project_root / "tools" / "galaxis_collection.py"
        if not script.is_file():
            raise RuntimeError(f"UMI 采集程序不存在: {script}")
        with self._lock:
            if self._stopping:
                raise RuntimeError("UMI 采集服务正在退出，请稍候")
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("UMI 采集会话已在运行")
        if not self.check(request.collection_profile).monitor.get("device_check", {}).get("ready"):
            raise RuntimeError("硬件检测未通过，请返回设备检测并确认相机、编码器和 PICO")
        minima = _read_encoder_minima(request.collection_profile)
        if self._configured_encoder_minima is not None and self._configured_encoder_minima != (
            request.collection_profile, minima
        ):
            raise RuntimeError("夹爪零点或采集模式已改变，请重新确认闭合最小值")
        with self._lock:
            if self._stopping or (
                self._process is not None and self._process.poll() is None
            ):
                raise RuntimeError("UMI 采集会话已在运行或正在退出")
            self._encoder_preview_blockers.add("collection")
        try:
            # The vendor collector owns the 63901 bridge and reconnects the
            # ADB tunnel after its listener is ready. Do not reset it here.
            self._stop_encoder_preview()
            self._stop_camera_previews()
            return self._start_with_encoder_handoff(request, script)
        except Exception:
            self._unblock_encoder_preview("collection")
            raise

    def _start_with_encoder_handoff(
        self, request: UmiStartRequest, script: Path
    ) -> UmiStartResponse:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("UMI 采集会话已在运行")
            self._error = ""
            self._stopping = False
            self._session_id = str(uuid.uuid4())
            self._task = request.task.strip()
            self._record_format = "rosbag"
            self._collection_profile = request.collection_profile
            self._started_at = datetime.now(UTC)
            log = (self.runtime_dir / "umi-collection.log").open("w", encoding="utf-8")
            env = os.environ.copy()
            env["DATA_MOUNT_PATH"] = str(self.data_root)
            command = [
                self.python,
                str(script),
                "sample",
                "--task",
                self._task,
                "--format",
                "rosbag",
                "--no-open",
                "--skip-device-setup",
                "--wait-for-devices",
                "--collection-profile",
                request.collection_profile,
            ]
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self._save_session_state(str(script))
            finally:
                log.close()
            return UmiStartResponse(
                session_id=self._session_id,
                state="starting",
                started_at=self._started_at,
            )

    def stop(self, session_id: str) -> UmiStatus:
        with self._lock:
            if self._process is None and self._session_state_file.is_file():
                self._restore_session_state()
            if session_id != self._session_id:
                raise ValueError("UMI 采集会话不存在")
            process = self._process
            if self._stopping:
                raise RuntimeError("采集服务正在关闭，请稍候")
        connected, monitor = self._monitor_state()
        if connected and monitor.get("recording"):
            raise RuntimeError("请先完成或放弃当前回合，再结束采集")
        with self._lock:
            self._stopping = True
            self._error = ""
        try:
            # An earlier saved round can still be converting in the
            # background. Do not interrupt its HDF5 worker.
            self._wait_for_hdf5_finalizations()
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=1)
            # SIGINT during early startup can bypass the collector's own
            # cleanup handler. Confirm every container is down before making
            # the configuration page usable again.
            self._stop_owned_stack()
            self._finish_stopped_process(process)
        except Exception as exc:
            with self._lock:
                self._stopping = False
                self._error = f"停止采集失败：{exc}"
            raise
        return self.status()

    def _finish_stopped_process(self, process: subprocess.Popen | None) -> None:
        with self._lock:
            if process is not None and self._process is not process:
                return
            self._process = None
            self._session_id = None
            self._started_at = None
            self._stopping = False
            self._error = ""
            self._device_check = {}
            self._stack_probe_at = 0.0
            self._encoder_preview_blockers.discard("collection")
            self._clear_session_state()

    def release_control(self) -> None:
        """Stop an idle UMI supervisor before PICO control returns to teleop."""
        connected, monitor = self._monitor_state()
        if connected and bool(monitor.get("recording")):
            raise RuntimeError("UMI 正在录制，请先停止并保存当前回合")
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                if not connected:
                    raise RuntimeError(
                        "UMI 仍在运行但无法确认录制状态，请先在 UMI 页面停止采集"
                    )
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.terminate()
            self._process = None
            self._session_id = None
            self._started_at = None
            self._error = ""
            self._encoder_preview_blockers.discard("collection")

    def import_counts(self) -> tuple[int, int]:
        queue = self.upload_queue()
        return queue['pending'], queue['uploaded']

    def upload_queue(self):
        from .umi_queue import queue_summary
        targets = getattr(self, 'upload_targets', None)
        return queue_summary(self.data_root, targets.snapshot() if targets else None)

    def processing_counts(self) -> tuple[int, int]:
        pending = list((self.data_root / "rosbags").glob("*/*/.mp4-pending.json"))
        failed = sum((p.parent / "recording-failure.json").is_file() for p in pending)
        return len(pending) - failed, failed
