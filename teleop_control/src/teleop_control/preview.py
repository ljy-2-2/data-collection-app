from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .gdk_env import refresh_discovery_env
from .models import CameraPreview, PreviewStatus

CAMERA_IDS = (
    "head_color", "head_stereo_left", "head_stereo_right",
    "head_back_fisheye", "head_left_fisheye", "head_right_fisheye",
    "hand_left_color", "hand_right_color", "head_depth", "hand_left_depth", "hand_right_depth",
)
REQUIRED_BODY_JOINTS = tuple(f"idx0{i}_body_joint{i}" for i in range(1, 6))
DAGGER_CAMERA_IDS = ("head_color", "hand_right_color")


def terminate_existing_preview_workers(
    worker_script: Path,
    *,
    proc_root: Path = Path("/proc"),
    kill: Callable[[int, signal.Signals], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
) -> list[int]:
    """Stop orphaned copies of this project's isolated GDK preview worker."""
    expected = str(worker_script.resolve())
    found: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = [
                value.decode(errors="replace")
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
        except OSError:
            continue
        if expected not in argv:
            continue
        pid = int(entry.name)
        try:
            kill(pid, signal.SIGTERM)
            found.append(pid)
        except ProcessLookupError:
            pass
    if found:
        sleep(0.3)
    return found


class GdkPreview:
    """Lazy, fail-closed reader for the isolated GDK preview subprocess."""

    def __init__(
        self,
        command: list[str],
        *,
        process_env: dict[str, str] | None = None,
        recording_status: Path | None = None,
        dagger_preview_dir: Path | None = None,
        cleanup_orphans: bool = False,
        stale_after: float = 3.0,
        robot_interface: str = "auto",
    ) -> None:
        self.runtime = Path(tempfile.mkdtemp(prefix="teleop-preview-"))
        self.selection_file = self.runtime / "selected-camera.txt"
        self.selection_file.write_text("head_color")
        self.command = [
            *command,
            "--runtime-dir",
            str(self.runtime),
            "--selection-file",
            str(self.selection_file),
        ]
        if dagger_preview_dir is not None:
            self.command.extend(["--dagger-preview-dir", str(dagger_preview_dir)])
        self.process_env = process_env
        self.recording_status = recording_status
        self.dagger_preview_dir = dagger_preview_dir
        self.stale_after = stale_after
        self.robot_interface = robot_interface
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last_start = 0.0
        self._selected_camera: str | None = "head_color"
        if cleanup_orphans and len(command) >= 2:
            stopped = terminate_existing_preview_workers(Path(command[1]))
            if stopped:
                print(f"[preview] stopped {len(stopped)} orphaned GDK preview workers")
        atexit.register(self.close)

    def select_camera(self, camera_id: str) -> None:
        if camera_id not in CAMERA_IDS:
            raise ValueError(f"unsupported preview camera: {camera_id}")
        with self._lock:
            self._selected_camera = camera_id
            worker_camera = (
                "" if camera_id in DAGGER_CAMERA_IDS and self._dagger_active()
                else camera_id
            )
            self._set_worker_camera_locked(worker_camera)

    def _set_worker_camera_locked(self, camera_id: str) -> None:
        try:
            if self.selection_file.read_text().strip() == camera_id:
                return
        except OSError:
            pass
        temporary = self.selection_file.with_suffix(".tmp")
        temporary.write_text(camera_id)
        os.replace(temporary, self.selection_file)

    def _dagger_active(self) -> bool:
        if self.dagger_preview_dir is None:
            return False
        marker = self.dagger_preview_dir / "active.json"
        try:
            payload = json.loads(marker.read_text())
            pid = int(payload["pid"])
            os.kill(pid, 0)
            return payload.get("state") == "recording"
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return False

    def _dagger_frame(self, camera_id: str) -> Path | None:
        if (
            self.dagger_preview_dir is None
            or camera_id not in DAGGER_CAMERA_IDS
            or not self._dagger_active()
        ):
            return None
        path = self.dagger_preview_dir / f"{camera_id}.jpg"
        try:
            if time.time() - path.stat().st_mtime <= 1.0:
                return path
        except OSError:
            pass
        return None

    def _ensure_started(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            now = time.monotonic()
            if now - self._last_start < 2.0:
                return
            self._last_start = now
            try:
                self._clear_runtime_outputs()
                with (self.runtime / "preview.log").open("ab") as log:
                    self._process = subprocess.Popen(
                        self.command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                        cwd=self.runtime,
                        env=self.process_env,
                    )
            except OSError:
                self._process = None

    def _clear_runtime_outputs(self) -> None:
        for path in [self.runtime / "status.json", *self.runtime.glob("*.jpg")]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _status_fresh(self) -> bool:
        try:
            return (
                time.time() - (self.runtime / "status.json").stat().st_mtime
                <= self.stale_after
            )
        except OSError:
            return False

    def refresh(self) -> None:
        """Drop stale GDK handles and reconnect the isolated preview worker."""
        with self._lock:
            if (
                self._process is not None
                and self._process.poll() is None
                and self._status_fresh()
            ):
                return
        self.close()
        if self.process_env is not None:
            refresh_discovery_env(self.process_env, self.robot_interface)
        with self._lock:
            self._last_start = 0.0
        self._ensure_started()

    def _payload(self) -> dict:
        self._ensure_started()
        try:
            path = self.runtime / "status.json"
            if time.time() - path.stat().st_mtime > self.stale_after:
                self.refresh()
                return {}
            payload = json.loads(path.read_text())
            return payload if isinstance(payload, dict) else {}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def snapshot(self) -> PreviewStatus:
        payload = self._payload()
        selected = self._selected_camera
        dagger_active = bool(
            selected in DAGGER_CAMERA_IDS and self._dagger_active()
        )
        with self._lock:
            self._set_worker_camera_locked("" if dagger_active else (selected or ""))
        raw_joints = payload.get("joint_positions", {})
        joints = {
            str(name): float(value)
            for name, value in raw_joints.items()
            if isinstance(value, (int, float))
        } if isinstance(raw_joints, dict) else {}
        updated_at = payload.get("updated_at")
        available_cameras = set(payload.get("cameras", []))
        camera_updated_at = payload.get("camera_updated_at", {})
        if not isinstance(camera_updated_at, dict):
            camera_updated_at = {}
        shared = self._dagger_frame(selected) if selected else None
        if shared is not None and selected is not None:
            available_cameras.add(selected)
            camera_updated_at[selected] = datetime.fromtimestamp(
                shared.stat().st_mtime, UTC
            ).isoformat()
        return PreviewStatus(
            available=all(name in joints for name in REQUIRED_BODY_JOINTS),
            recording=self._recording(),
            updated_at=updated_at,
            joint_positions=joints,
            cameras=[
                CameraPreview(
                    id=camera_id,
                    available=camera_id in available_cameras,
                    updated_at=(
                        camera_updated_at.get(camera_id)
                        if camera_id in available_cameras
                        else None
                    ),
                )
                for camera_id in CAMERA_IDS
            ],
        )

    def _recording(self) -> bool:
        if self.recording_status is None:
            return False
        try:
            payload = json.loads(self.recording_status.read_text())
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and payload.get("recording") is True

    def frame(self, camera_id: str) -> bytes | None:
        if camera_id not in CAMERA_IDS:
            return None
        self.select_camera(camera_id)
        status = self.snapshot()
        if not any(camera.id == camera_id and camera.available for camera in status.cameras):
            return None
        shared = self._dagger_frame(camera_id)
        try:
            return (
                shared if shared is not None
                else self.runtime / f"{camera_id}.jpg"
            ).read_bytes()
        except OSError:
            return None

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass


def unavailable_preview() -> PreviewStatus:
    return PreviewStatus(
        available=False,
        recording=False,
        updated_at=datetime.now(UTC),
        cameras=[CameraPreview(id=camera_id, available=False) for camera_id in CAMERA_IDS],
    )
