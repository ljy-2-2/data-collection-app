from __future__ import annotations

import hashlib
import json
import os
import shutil
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

from .models import (
    HilSerlArtifact,
    HilSerlArtifacts,
    HilSerlProgress,
    HilSerlStartRequest,
    HilSerlStartResponse,
    HilSerlStatus,
)


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...


ProcessFactory = Callable[..., ProcessLike]
ProcessStopper = Callable[[ProcessLike], None]


def _stop_group(process: ProcessLike) -> None:
    os.killpg(process.pid, signal.SIGINT)


class HilSerlManager:
    """Own one HIL-SERL supervisor and expose its file-backed live status."""

    ACTIVE_STATES = {
        "validating",
        "converting",
        "starting_learner",
        "starting_actor",
        "resetting",
        "waiting_scene",
        "running",
        "training_bc",
        "stopping",
    }

    def __init__(
        self,
        *,
        root: str | Path,
        runtime_dir: str | Path,
        runner_command: list[str],
        process_factory: ProcessFactory = subprocess.Popen,
        process_stopper: ProcessStopper = _stop_group,
        process_env: dict[str, str] | None = None,
        source_roots: list[str | Path] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.runner_command = runner_command
        self.process_factory = process_factory
        self.process_stopper = process_stopper
        self.process_env = process_env
        self.source_roots = [
            Path(path).expanduser().resolve()
            for path in (
                source_roots
                if source_roots is not None
                else [
                    self.root.parent / "hil",
                    Path.home() / "agibot_episodes" / "right_arm",
                    Path.home() / "agibot_episodes" / "dual_arm",
                ]
            )
        ]
        self._lock = threading.RLock()
        self._process: ProcessLike | None = None
        self._session_id: str | None = None
        self._status_file: Path | None = None
        self._command_file: Path | None = None
        self._log_file: Path | None = None

    @staticmethod
    def _read_json(path: Path | None) -> dict:
        if path is None:
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _refresh_locked(self) -> None:
        if self._process is not None and self._process.poll() is not None:
            self._process = None

    def active(self) -> bool:
        with self._lock:
            self._refresh_locked()
            if self._process is None:
                return False
            return (
                self._read_json(self._status_file).get("state", "validating") in self.ACTIVE_STATES
            )

    def status(self) -> HilSerlStatus:
        with self._lock:
            self._refresh_locked()
            payload = self._read_json(self._status_file)
            if not payload:
                payload = {
                    "state": "error" if self._session_id else "idle",
                    "message": "HIL-SERL 进程已退出" if self._session_id else "",
                    "session_id": self._session_id,
                }
            lines: list[str] = []
            if self._log_file is not None:
                try:
                    lines = self._log_file.read_text(errors="replace").splitlines()[-100:]
                except OSError:
                    pass
            actor = (
                self._read_json(Path(payload["actor_progress_file"]))
                if payload.get("actor_progress_file")
                else None
            )
            learner = (
                self._read_json(Path(payload["learner_progress_file"]))
                if payload.get("learner_progress_file")
                else None
            )
            return HilSerlStatus(
                state=payload.get("state", "idle"),
                mode=payload.get("mode"),
                message=str(payload.get("message", "")),
                session_id=payload.get("session_id"),
                checkpoint_path=payload.get("checkpoint_path"),
                demo_path=payload.get("demo_path"),
                started_at=payload.get("started_at"),
                scene_waiting=bool(payload.get("scene_waiting", False)),
                actor=HilSerlProgress(**actor) if actor else None,
                learner=HilSerlProgress(**learner) if learner else None,
                log_tail=lines,
            )

    def start(self, request: HilSerlStartRequest) -> HilSerlStartResponse:
        if not self.root.is_dir():
            raise HTTPException(status_code=503, detail=f"HIL-SERL 目录不存在: {self.root}")
        with self._lock:
            if self.active():
                raise HTTPException(status_code=409, detail="HIL-SERL 训练已在运行")
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            session_id = str(uuid.uuid4())
            prefix = f"hil-serl-{session_id}"
            status_file = self.runtime_dir / f"{prefix}.status.json"
            command_file = self.runtime_dir / f"{prefix}.commands.jsonl"
            log_file = self.runtime_dir / f"{prefix}.log"
            request_file = self.runtime_dir / f"{prefix}.request.json"
            command_file.touch()
            request_file.write_text(request.model_dump_json(), encoding="utf-8")
            command = [
                *self.runner_command,
                "--root",
                str(self.root),
                "--session-id",
                session_id,
                "--request-file",
                str(request_file),
                "--status-file",
                str(status_file),
                "--command-file",
                str(command_file),
                "--log-file",
                str(log_file),
            ]
            try:
                process = self.process_factory(
                    command,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env=self.process_env,
                )
            except OSError as exc:
                raise HTTPException(status_code=503, detail=f"无法启动 HIL-SERL: {exc}") from exc
            self._process = process
            self._session_id = session_id
            self._status_file = status_file
            self._command_file = command_file
            self._log_file = log_file
            checkpoint = request.checkpoint_path or str(self.root / "checkpoints")
            return HilSerlStartResponse(
                session_id=session_id, state="validating", checkpoint_path=checkpoint
            )

    def command(self, command: str) -> HilSerlStatus:
        with self._lock:
            if not self.active() or self._command_file is None:
                raise HTTPException(status_code=404, detail="HIL-SERL 训练会话不存在")
            status = self.status()
            if command == "scene_ready" and not status.scene_waiting:
                raise HTTPException(status_code=409, detail="当前不在等待场景确认")
            if command not in {"scene_ready", "stop"}:
                raise HTTPException(status_code=400, detail="不支持的 HIL-SERL 命令")
            with self._command_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"command": command, "at": time.time_ns()}) + "\n")
            if command == "stop" and self._process is not None:
                self.process_stopper(self._process)
        return self.status()

    def _inside_root(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if path == self.root or self.root not in path.parents:
            raise HTTPException(status_code=400, detail="文件不在 HIL-SERL 目录内")
        return path

    @staticmethod
    def _artifact(kind: str, path: Path, latest: bool = False) -> HilSerlArtifact:
        stat = path.stat()
        files = list(path.glob("*.pkl")) if path.is_dir() else []
        source_count = (
            sum(1 for item in path.iterdir() if (item / "record/bundles.jsonl").is_file())
            if kind == "source" and path.is_dir()
            else None
        )
        size = (
            stat.st_size
            if path.is_file()
            else sum(item.stat().st_size for item in files)
            if kind in {"demo", "replay"}
            else None
        )
        digest = hashlib.sha256(f"{kind}:{path}".encode()).hexdigest()[:20]
        name = (
            f"{path.parent.name}/{path.name}"
            if kind in {"model", "bc_model"} and path.name.startswith("checkpoint_")
            else path.name
        )
        return HilSerlArtifact(
            id=digest,
            kind=kind,
            name=name,
            path=str(path),
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            size_bytes=size,
            file_count=(
                source_count if source_count is not None else len(files) if path.is_dir() else 1
            ),
            latest=latest,
        )

    def artifacts(self) -> HilSerlArtifacts:
        checkpoints = self.root / "checkpoints"
        demo_root = self.root / "demo_data"
        run_paths = (
            [p for p in checkpoints.iterdir() if p.is_dir() and any(p.glob("checkpoint_*"))]
            if checkpoints.is_dir()
            else []
        )
        archive = checkpoints / "archive"
        if archive.is_dir():
            run_paths.extend(
                path
                for path in archive.iterdir()
                if path.is_dir() and any(path.glob("checkpoint_*"))
            )
        model_paths = [
            checkpoint
            for run in run_paths
            for checkpoint in run.glob("checkpoint_*")
            if checkpoint.is_dir()
        ]
        model_paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        bc_paths = [p for p in model_paths if "bc" in p.parent.name.lower()]
        rl_paths = [p for p in model_paths if p not in bc_paths]
        demo_paths = (
            [p for p in demo_root.iterdir() if p.is_dir() and any(p.glob("*.pkl"))]
            if demo_root.is_dir()
            else []
        )
        demo_paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        replay_paths: list[Path] = []
        if checkpoints.is_dir():
            candidates = list(checkpoints.glob("*/buffer/*.pkl"))
            if archive.is_dir():
                candidates.extend(archive.glob("*/buffer/*.pkl"))
            replay_paths = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[:120]
        source_paths = [
            path
            for path in self.source_roots
            if path.is_dir()
            and any((item / "record/bundles.jsonl").is_file() for item in path.iterdir())
        ]
        source_paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return HilSerlArtifacts(
            root=str(self.root),
            models=[
                self._artifact("model", path, index == 0) for index, path in enumerate(rl_paths)
            ],
            bc_models=[
                self._artifact("bc_model", path, index == 0) for index, path in enumerate(bc_paths)
            ],
            demos=[
                self._artifact("demo", path, index == 0) for index, path in enumerate(demo_paths)
            ],
            replays=[
                self._artifact("replay", path, index == 0)
                for index, path in enumerate(replay_paths)
            ],
            source_folders=[
                self._artifact("source", path, index == 0)
                for index, path in enumerate(source_paths)
            ],
        )

    def resolve_artifact(self, value: str) -> Path:
        path = self._inside_root(value)
        if not path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        allowed = (self.root / "checkpoints", self.root / "demo_data")
        if not any(base == path or base in path.parents for base in allowed):
            raise HTTPException(status_code=400, detail="不允许访问该路径")
        return path

    def delete_artifact(self, value: str) -> None:
        path = self.resolve_artifact(value)
        with self._lock:
            if self.active():
                current = self.status()
                used = {current.checkpoint_path, current.demo_path}
                if str(path) in used or any(
                    Path(item).is_relative_to(path) or path.is_relative_to(Path(item))
                    for item in used
                    if item
                ):
                    raise HTTPException(status_code=409, detail="训练正在使用该文件，不能删除")
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
