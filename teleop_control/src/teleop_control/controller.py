from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException

from .devices import DeviceDetector
from .gdk_env import refresh_discovery_env
from .hil_serl import HilSerlManager
from .models import (
    DaggerCommandRequest,
    DaggerStartRequest,
    DaggerStartResponse,
    DaggerStatus,
    DeviceStatus,
    HilSerlArtifacts,
    HilSerlStartRequest,
    HilSerlStartResponse,
    HilSerlStatus,
    MotionStatus,
    MoveRequest,
    MoveResponse,
    SessionStatus,
    StartSessionRequest,
    StartSessionResponse,
    StopSessionResponse,
)

MANUAL_JOINTS = (
    [f"idx2{i}_arm_l_joint{i}" for i in range(1, 8)]
    + ["idx31_gripper_l_inner_joint1"]
    + [f"idx0{i}_body_joint{i}" for i in range(1, 6)]
    + [f"idx6{i}_arm_r_joint{i}" for i in range(1, 8)]
    + ["idx71_gripper_r_inner_joint1"]
)


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def send_signal(self, signal_number: int) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class XrService(Protocol):
    def ensure_started(self) -> bool: ...

    def restart(self) -> None: ...


class HeadsetSetup(Protocol):
    def ensure_started(self) -> bool: ...


class ControlPreparer(Protocol):
    def prepare(self) -> None: ...


ProcessFactory = Callable[..., ProcessLike]
ProcessStopper = Callable[[ProcessLike], None]
Clock = Callable[[], datetime]


def stop_process_group(process: ProcessLike) -> None:
    os.killpg(process.pid, signal.SIGINT)


class TeleopController:
    def __init__(
        self,
        *,
        detector: DeviceDetector,
        teleop_command: list[str],
        move_command: list[str],
        dagger_command: list[str] | None = None,
        dagger_runtime_dir: str | Path | None = None,
        guard_command: list[str] | None = None,
        xr_service: XrService | None = None,
        headset_setup: HeadsetSetup | None = None,
        control_preparer: ControlPreparer | None = None,
        xr_preflight_command: list[str] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        process_factory: ProcessFactory = subprocess.Popen,
        process_stopper: ProcessStopper = stop_process_group,
        process_env: dict[str, str] | None = None,
        robot_interface: str | None = None,
        now: Clock = lambda: datetime.now(UTC),
        hil_serl: HilSerlManager | None = None,
    ) -> None:
        self.detector = detector
        self.teleop_command = teleop_command
        self.move_command = move_command
        self.dagger_command = dagger_command
        self.dagger_runtime_dir = Path(dagger_runtime_dir) if dagger_runtime_dir else None
        self.guard_command = guard_command
        self.xr_service = xr_service
        self.headset_setup = headset_setup
        self.control_preparer = control_preparer
        self.xr_preflight_command = xr_preflight_command
        self.command_runner = command_runner
        self.process_factory = process_factory
        self.process_stopper = process_stopper
        self.process_env = process_env
        self.robot_interface = robot_interface
        self.now = now
        self.hil_serl = hil_serl
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._session_process: ProcessLike | None = None
        self._session_id: str | None = None
        self._session_started_at: datetime | None = None
        self._finalizing_processes: list[ProcessLike] = []
        self._motion_process: ProcessLike | None = None
        self._motion_id: str | None = None
        self._completed_motion_id: str | None = None
        self._failed_motion_id: str | None = None
        self._preview_enabled = False
        self._dagger_process: ProcessLike | None = None
        self._dagger_session_id: str | None = None
        self._dagger_status_file: Path | None = None
        self._dagger_command_file: Path | None = None
        self._dagger_log_file: Path | None = None

    def _devices(self) -> list[DeviceStatus]:
        return self.detector.check_all()

    def _guarded(self, command: list[str]) -> list[str]:
        if self.guard_command is None:
            return command
        return [*self.guard_command, "--parent-pid", str(os.getpid()), "--", *command]

    @staticmethod
    def _all_ready(devices: list[DeviceStatus]) -> bool:
        return {device.id for device in devices if device.status == "connected"} == {
            "robot",
            "xr",
            "headset",
        }

    def _refresh_processes(self) -> None:
        self._finalizing_processes = [
            process for process in self._finalizing_processes if process.poll() is None
        ]
        if self._session_process is not None and self._session_process.poll() is not None:
            self._session_process = None
            self._session_id = None
            self._session_started_at = None
        if self._motion_process is not None and self._motion_process.poll() is not None:
            if self._motion_process.poll() == 0:
                self._completed_motion_id = self._motion_id
                self._failed_motion_id = None
            else:
                self._failed_motion_id = self._motion_id
                self._completed_motion_id = None
            self._motion_process = None
            self._motion_id = None

        if self._dagger_process is not None and self._dagger_process.poll() is not None:
            self._dagger_process = None

    def status(self) -> tuple[list[DeviceStatus], SessionStatus, MotionStatus]:
        devices = self._devices()
        robot_connected = any(
            device.id == "robot" and device.status == "connected" for device in devices
        )
        with self._lock:
            # Keep preview availability accurate from plain status polling too,
            # so returning to the teleop page does not need a device check that
            # would restart the Galaxis app / GDK preview worker.
            self._preview_enabled = robot_connected
            self._refresh_processes()
            if self._session_process is None:
                session = SessionStatus(state="idle")
            else:
                session = SessionStatus(
                    state="running",
                    session_id=self._session_id,
                    started_at=self._session_started_at,
                )
            if self._motion_process is None:
                motion = MotionStatus(
                    state="idle",
                    completed_command_id=self._completed_motion_id,
                    failed_command_id=self._failed_motion_id,
                )
            else:
                motion = MotionStatus(state="moving", command_id=self._motion_id)
        return devices, session, motion

    def check_devices(self) -> tuple[list[DeviceStatus], SessionStatus, MotionStatus]:
        if self.xr_service is not None:
            self.xr_service.ensure_started()
        if self.headset_setup is not None:
            self.headset_setup.ensure_started()
        result = self.status()
        with self._lock:
            self._preview_enabled = any(
                device.id == "robot" and device.status == "connected" for device in result[0]
            )
        return result

    def preview_enabled(self) -> bool:
        with self._lock:
            return self._preview_enabled

    def _require_ready(self) -> None:
        if not self._all_ready(self._devices()):
            raise HTTPException(status_code=409, detail="Required teleop devices are not ready")

    def _require_robot_ready(self) -> None:
        if not any(
            device.id == "robot" and device.status == "connected" for device in self._devices()
        ):
            raise HTTPException(status_code=409, detail="Robot is not ready")

    def _prepare_xr_control(self) -> None:
        """Ensure both ends of the XR stream are available before robot control."""
        if self.control_preparer is not None:
            try:
                self.control_preparer.prepare()
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if self.xr_service is not None:
            self.xr_service.ensure_started()
        # Preserve an already-running Galaxis app and its in-app connection.
        # Galaxis VR 1.1.6 cannot restore the data-stream toggle after an ADB
        # force-stop, which otherwise makes the operator reconnect manually.
        if self.headset_setup is not None and not self.headset_setup.ensure_started():
            raise HTTPException(
                status_code=409,
                detail="Pico Galaxis 会话准备失败，无法接收手柄数据",
            )
        if self.xr_preflight_command is not None:
            result = self._run_xr_preflight()
            if result.returncode != 0 and self.xr_service is not None:
                restart = getattr(self.xr_service, "restart", None)
                if callable(restart):
                    try:
                        restart()
                        if self.headset_setup is not None:
                            self.headset_setup.ensure_started()
                        result = self._run_xr_preflight()
                    except RuntimeError as exc:
                        raise HTTPException(
                            status_code=409,
                            detail=f"PICO/XR 自动恢复失败: {exc}",
                        ) from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip().splitlines()
                reason = detail[-1] if detail else "未收到新鲜手柄数据"
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "PICO/XR 没有控制数据；已自动重启 Galaxis VR。"
                        "请确认头显为 WORK，Head、Controllers、Send Data 已开启。"
                        f"{reason}"
                    ),
                )
            # This vendor bridge can keep 60061 open after a diagnostic SDK
            # client disconnects while refusing frames to its successor. Give
            # the real controller a clean, single-client bridge instance.
            if self.xr_service is not None:
                restart = getattr(self.xr_service, "restart", None)
                if callable(restart):
                    try:
                        restart()
                        if self.headset_setup is not None:
                            self.headset_setup.ensure_started()
                    except RuntimeError as exc:
                        raise HTTPException(
                            status_code=409,
                            detail=f"PICO/XR 控制链路刷新失败: {exc}",
                        ) from exc

    def _run_xr_preflight(self) -> subprocess.CompletedProcess[str]:
        assert self.xr_preflight_command is not None
        try:
            return self.command_runner(
                self.xr_preflight_command,
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
                env=self.process_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"PICO/XR 控制数据验证失败: {exc}",
            ) from exc

    def _robot_launch_env(self) -> dict[str, str] | None:
        if self.robot_interface is None:
            return self.process_env
        env = dict(os.environ if self.process_env is None else self.process_env)
        refresh_discovery_env(env, self.robot_interface)
        if not env.get("LOCATOR_IP"):
            raise HTTPException(
                status_code=409,
                detail="未找到唯一的机器人网卡地址，请检查连接或配置 TELEOP_ROBOT_INTERFACE",
            )
        return env

    def start_session(self, request: StartSessionRequest) -> StartSessionResponse:
        with self._operation_lock:
            with self._lock:
                self._refresh_processes()
                if self._session_process is not None:
                    raise HTTPException(status_code=409, detail="Teleop session is already running")
                if self._finalizing_processes:
                    raise HTTPException(
                        status_code=409,
                        detail="Previous teleop session is being handed to the import queue",
                    )
                if self._motion_process is not None:
                    raise HTTPException(status_code=409, detail="Manual motion is still running")
                if self._dagger_active_locked():
                    raise HTTPException(status_code=409, detail="DAgger session is running")
                if self.hil_serl is not None and self.hil_serl.active():
                    raise HTTPException(status_code=409, detail="HIL-SERL training is running")
                self._discard_finished_dagger_locked()
            self._prepare_xr_control()
            self._require_ready()
            with self._lock:
                session_id = str(uuid.uuid4())
                started_at = self.now()
                session_folder = f"teleop-{started_at:%Y%m%d-%H%M%S}-{session_id[:8]}"
                try:
                    command = [
                        *self.teleop_command,
                        "--session-id",
                        session_id,
                        "--session-folder",
                        session_folder,
                        "--scale",
                        str(request.scale),
                        "--max-joint-vel",
                        str(request.max_joint_vel),
                    ]
                    if request.task is not None:
                        # Replace the legacy launch-time label, preserving all
                        # other runner arguments and never invoking a shell.
                        task_index = command.index("--task") if "--task" in command else None
                        if task_index is not None:
                            command[task_index + 1] = request.task
                        else:
                            command.extend(["--task", request.task])
                    for camera in request.extra_cameras:
                        command.extend(["--extra-camera", camera])
                    print(
                        "[launch] selected extra cameras: "
                        + (", ".join(request.extra_cameras) or "none"),
                        flush=True,
                    )
                    process = self.process_factory(
                        self._guarded(command),
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                        env=self._robot_launch_env(),
                    )
                except OSError as exc:
                    raise HTTPException(
                        status_code=503, detail=f"Cannot start teleop runner: {exc}"
                    ) from exc
                self._session_process = process
                self._session_id = session_id
                self._session_started_at = started_at
                return StartSessionResponse(
                    session_id=session_id,
                    started_at=started_at,
                    extra_cameras=list(request.extra_cameras),
                )

    def stop_session(self, session_id: str) -> StopSessionResponse:
        with self._lock:
            self._refresh_processes()
            if self._session_process is None or session_id != self._session_id:
                raise HTTPException(status_code=404, detail="Teleop session not found")
            process = self._session_process
            self.process_stopper(process)
            if process.poll() is None:
                self._finalizing_processes.append(process)
            self._session_process = None
            self._session_id = None
            self._session_started_at = None
            return StopSessionResponse(stopped_at=self.now())

    def move(self, request: MoveRequest) -> MoveResponse:
        if request.joint_names != MANUAL_JOINTS:
            raise HTTPException(status_code=400, detail="Unexpected G2 manual joint order")
        if len(request.joint_positions) != len(MANUAL_JOINTS):
            raise HTTPException(
                status_code=400, detail="Joint names and positions differ in length"
            )
        if not all(math.isfinite(value) for value in request.joint_positions):
            raise HTTPException(status_code=400, detail="Joint targets must be finite")
        if request.changed_joint is not None and request.changed_joint not in MANUAL_JOINTS:
            raise HTTPException(status_code=400, detail="Unexpected changed joint")
        with self._operation_lock:
            with self._lock:
                self._refresh_processes()
                if self._session_process is not None:
                    raise HTTPException(status_code=409, detail="Teleop session is running")
                if self._motion_process is not None:
                    raise HTTPException(status_code=409, detail="Manual motion is already running")
                if self._dagger_active_locked():
                    raise HTTPException(status_code=409, detail="DAgger session is running")
                if self.hil_serl is not None and self.hil_serl.active():
                    raise HTTPException(status_code=409, detail="HIL-SERL training is running")
            self._require_robot_ready()
            with self._lock:
                command = [
                    *self.move_command,
                    "--request-json",
                    json.dumps(request.model_dump(), separators=(",", ":")),
                ]
                try:
                    process = self.process_factory(
                        self._guarded(command),
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                        env=self._robot_launch_env(),
                    )
                except OSError as exc:
                    raise HTTPException(
                        status_code=503, detail=f"Cannot start GDK move: {exc}"
                    ) from exc
                self._motion_process = process
                self._motion_id = request.command_id
                self._completed_motion_id = None
                self._failed_motion_id = None
                return MoveResponse(accepted=True, motion_id=request.command_id)

    def _read_dagger_status_locked(self) -> dict:
        if self._dagger_status_file is None:
            return {}
        try:
            payload = json.loads(self._dagger_status_file.read_text())
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _dagger_active_locked(self) -> bool:
        self._refresh_processes()
        if self._dagger_process is None:
            return False
        state = self._read_dagger_status_locked().get("state", "starting")
        return state not in {"idle", "finished", "error"}

    def _discard_finished_dagger_locked(self) -> None:
        if self._dagger_process is None or self._dagger_active_locked():
            return
        self.process_stopper(self._dagger_process)
        self._dagger_process = None

    def dagger_status(self) -> DaggerStatus:
        with self._lock:
            self._refresh_processes()
            payload = self._read_dagger_status_locked()
            if not payload:
                state = "error" if self._dagger_session_id else "idle"
                message = "DAgger 进程已退出" if self._dagger_session_id else ""
                payload = {
                    "state": state,
                    "message": message,
                    "session_id": self._dagger_session_id,
                }
            lines: list[str] = []
            if self._dagger_log_file is not None:
                try:
                    lines = self._dagger_log_file.read_text(errors="replace").splitlines()[-80:]
                except OSError:
                    pass
            return DaggerStatus(
                state=payload.get("state", "idle"),
                message=str(payload.get("message", "")),
                session_id=payload.get("session_id"),
                session_dir=payload.get("session_dir"),
                saved_count=payload.get("saved_count"),
                log_tail=lines,
            )

    def start_dagger(self, request: DaggerStartRequest) -> DaggerStartResponse:
        if self.dagger_command is None or self.dagger_runtime_dir is None:
            raise HTTPException(status_code=503, detail="DAgger is not configured")
        checkpoint = Path(request.checkpoint).expanduser()
        if not checkpoint.is_dir():
            raise HTTPException(
                status_code=400, detail=f"Checkpoint directory not found: {checkpoint}"
            )
        with self._operation_lock:
            with self._lock:
                self._refresh_processes()
                if self._session_process is not None:
                    raise HTTPException(status_code=409, detail="Teleop session is running")
                if self._motion_process is not None:
                    raise HTTPException(status_code=409, detail="Manual motion is running")
                if self._dagger_active_locked():
                    raise HTTPException(status_code=409, detail="DAgger session is already running")
                if self.hil_serl is not None and self.hil_serl.active():
                    raise HTTPException(status_code=409, detail="HIL-SERL training is running")
                self._discard_finished_dagger_locked()
            self._prepare_xr_control()
            self._require_ready()
            with self._lock:
                session_id = str(uuid.uuid4())
                self.dagger_runtime_dir.mkdir(parents=True, exist_ok=True)
                prefix = f"dagger-{session_id}"
                status_file = self.dagger_runtime_dir / f"{prefix}.status.json"
                command_file = self.dagger_runtime_dir / f"{prefix}.commands.jsonl"
                log_file = self.dagger_runtime_dir / f"{prefix}.log"
                command_file.touch()
                command = [
                    *self.dagger_command,
                    "--session-id",
                    session_id,
                    "--checkpoint",
                    str(checkpoint),
                    "--task",
                    request.task,
                    "--status-file",
                    str(status_file),
                    "--command-file",
                    str(command_file),
                    "--log-file",
                    str(log_file),
                ]
                try:
                    process = self.process_factory(
                        self._guarded(command),
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                        env=self.process_env,
                    )
                except OSError as exc:
                    raise HTTPException(
                        status_code=503, detail=f"Cannot start DAgger: {exc}"
                    ) from exc
                self._dagger_process = process
                self._dagger_session_id = session_id
                self._dagger_status_file = status_file
                self._dagger_command_file = command_file
                self._dagger_log_file = log_file
                return DaggerStartResponse(session_id=session_id, state="starting")

    def dagger_command_request(self, request: DaggerCommandRequest) -> DaggerStatus:
        with self._lock:
            self._refresh_processes()
            if self._dagger_process is None or self._dagger_command_file is None:
                raise HTTPException(status_code=404, detail="DAgger session not found")
            state = self._read_dagger_status_locked().get("state", "starting")
            allowed = {
                "takeover": {"inference"},
                "resume": {"teleop"},
                "end": {"inference", "teleop"},
            }
            if state not in allowed[request.command]:
                raise HTTPException(
                    status_code=409, detail=f"Cannot {request.command} while DAgger is {state}"
                )
            with self._dagger_command_file.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "command": request.command,
                            "pc_ts_ns": time.time_ns(),
                            **(
                                {
                                    "save_mode": request.save_mode or "corrections",
                                    "pre_seconds": (
                                        request.pre_seconds
                                        if request.pre_seconds is not None
                                        else 2.0
                                    ),
                                    "post_seconds": (
                                        request.post_seconds
                                        if request.post_seconds is not None
                                        else 2.0
                                    ),
                                }
                                if request.command == "end"
                                else {}
                            ),
                        }
                    )
                    + "\n"
                )
        return self.dagger_status()

    def hil_serl_status(self) -> HilSerlStatus:
        if self.hil_serl is None:
            return HilSerlStatus(state="error", message="HIL-SERL is not configured")
        return self.hil_serl.status()

    def hil_serl_artifacts(self) -> HilSerlArtifacts:
        if self.hil_serl is None:
            raise HTTPException(status_code=503, detail="HIL-SERL is not configured")
        return self.hil_serl.artifacts()

    def start_hil_serl(self, request: HilSerlStartRequest) -> HilSerlStartResponse:
        if self.hil_serl is None:
            raise HTTPException(status_code=503, detail="HIL-SERL is not configured")
        with self._operation_lock:
            with self._lock:
                self._refresh_processes()
                if self._session_process is not None:
                    raise HTTPException(status_code=409, detail="Teleop session is running")
                if self._motion_process is not None:
                    raise HTTPException(status_code=409, detail="Manual motion is running")
                if self._dagger_active_locked():
                    raise HTTPException(status_code=409, detail="DAgger session is running")
                if self.hil_serl.active():
                    raise HTTPException(
                        status_code=409, detail="HIL-SERL training is already running"
                    )
            if request.mode != "bc":
                self._prepare_xr_control()
                self._require_ready()
            return self.hil_serl.start(request)

    def hil_serl_command(self, command: str) -> HilSerlStatus:
        if self.hil_serl is None:
            raise HTTPException(status_code=503, detail="HIL-SERL is not configured")
        return self.hil_serl.command(command)

    def delete_hil_serl_artifact(self, path: str) -> None:
        if self.hil_serl is None:
            raise HTTPException(status_code=503, detail="HIL-SERL is not configured")
        self.hil_serl.delete_artifact(path)

    def resolve_hil_serl_artifact(self, path: str) -> Path:
        if self.hil_serl is None:
            raise HTTPException(status_code=503, detail="HIL-SERL is not configured")
        return self.hil_serl.resolve_artifact(path)
