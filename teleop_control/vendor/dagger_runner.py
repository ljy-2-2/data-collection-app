#!/usr/bin/env python3
"""Headless, project-local DAgger session orchestrator for the web control card."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from collection_ipc import CollectionClient
from remote_collector import collection_context
from dagger_dataset import (
    save_correction_windows,
    save_full_episode,
    synchronize_episode_frames,
)

HERE = Path(__file__).resolve().parent
ACTIVE_STATES = {"starting", "inference", "teleop", "switching", "stopping", "saving"}


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False))
    os.replace(temporary, path)


def wait_json(path: Path, state: str, process: subprocess.Popen, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        if last and last.get("state") == state:
            return last
        if process.poll() is not None:
            raise RuntimeError(f"采集进程提前退出（{process.returncode}）")
        time.sleep(0.1)
    raise TimeoutError(f"等待 {state} 超时；最后状态：{last}")


def wait_file(path: Path, process: subprocess.Popen, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise RuntimeError(f"{label}在就绪前退出（{process.returncode}）")
        time.sleep(0.1)
    raise TimeoutError(f"{label}在 {timeout:g} 秒内未就绪")


def wait_camera_frames(
    preview_dir: Path,
    camera_names: tuple[str, ...],
    process: subprocess.Popen,
    timeout: float,
) -> None:
    """Require one newly collected frame from every model camera."""
    deadline = time.monotonic() + timeout
    missing = list(camera_names)
    while time.monotonic() < deadline:
        missing = []
        for camera in camera_names:
            path = preview_dir / f"{camera}.jpg"
            try:
                fresh = path.stat().st_size > 0
            except OSError:
                fresh = False
            if not fresh:
                missing.append(camera)
        if not missing:
            return
        if process.poll() is not None:
            raise RuntimeError(f"采集进程在相机就绪前退出（{process.returncode}）")
        time.sleep(0.1)
    labels = {
        "head_color": "头部相机",
        "hand_right_color": "右手相机",
    }
    missing_text = "、".join(labels.get(name, name) for name in missing)
    raise TimeoutError(
        f"{missing_text}在 {timeout:g} 秒内没有同步画面；"
        "请点击检查设备清理旧相机进程后重试"
    )


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
    except ProcessLookupError:
        pass


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.platform_context = collection_context()
        self.runtime = Path(tempfile.mkdtemp(prefix="data-platform-dagger-"))
        self.collector_status = self.runtime / "collector.json"
        self.collector_socket = self.runtime / "collector.sock"
        self.model_socket = self.runtime / "model.sock"
        self.teleop_active = self.runtime / "teleop-mode.txt"
        self.teleop_inactive = self.runtime / "teleop-inactive.json"
        self.inference_active = self.runtime / "inference-mode.txt"
        self.xr_events = self.runtime / "xr-events.jsonl"
        self.event_offset = 0
        self.command_offset = 0
        self.state = "starting"
        self.message = "正在初始化相机、XR 遥操作和模型推理（尚未录制）"
        self.session_dir: Path | None = None
        self.collector: subprocess.Popen | None = None
        self.teleop: subprocess.Popen | None = None
        self.model: subprocess.Popen | None = None
        self.inference: subprocess.Popen | None = None
        self.control: CollectionClient | None = None
        self.log = Path(args.log_file).open("a", buffering=1)
        self.stop_requested = False
        self.inference_env = os.environ.copy()
        python_paths = [str(Path(args.collection_project).expanduser())]
        python_paths.extend(
            item for item in self.inference_env.get("PYTHONPATH", "").split(os.pathsep) if item
        )
        self.inference_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_paths))

    def publish(self, state: str | None = None, message: str | None = None, **extra) -> None:
        if state is not None:
            self.state = state
        if message is not None:
            self.message = message
        atomic_json(
            Path(self.args.status_file),
            {
                "state": self.state,
                "message": self.message,
                "session_id": self.args.session_id,
                "session_dir": str(self.session_dir) if self.session_dir else None,
                "log_file": self.args.log_file,
                "updated_at_ns": time.time_ns(),
                **extra,
            },
        )

    def spawn(self, command: list[str], *, env: dict | None = None) -> subprocess.Popen:
        print("[dagger] launch:", " ".join(command), file=self.log, flush=True)
        return subprocess.Popen(
            command,
            cwd=HERE,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

    def start(self) -> None:
        checkpoint = Path(self.args.checkpoint).expanduser()
        if not checkpoint.is_dir():
            raise FileNotFoundError(f"模型目录不存在：{checkpoint}")
        validator = self.spawn(
            [
                self.args.model_python,
                str(HERE / "validate_diffusion_checkpoint.py"),
                str(checkpoint),
            ]
        )
        if validator.wait() != 0:
            raise RuntimeError("模型检查失败；请查看 DAgger 运行日志")
        raw_root = Path(self.args.output).expanduser() / ".dagger-raw"
        raw_root.mkdir(parents=True, exist_ok=True)
        self.collector = self.spawn(
            [
                self.args.gdk_python,
                str(HERE / "dagger_collector.py"),
                "--socket",
                str(self.collector_socket),
                "--status-file",
                str(self.collector_status),
                "--task",
                self.args.task,
                "--out",
                str(raw_root),
                "--config",
                str(HERE / "gdk_data_collection/config/dagger_right_arm.yaml"),
                "--collection-project",
                self.args.collection_project,
                "--right-only",
                "--image-width",
                "640",
                "--image-height",
                "480",
                "--preview-dir",
                self.args.preview_dir,
            ]
        )
        wait_json(self.collector_status, "ready", self.collector, 30)
        self.control = CollectionClient(str(self.collector_socket), timeout_s=10)

        self.teleop_active.write_text("inactive")
        self.xr_events.touch()
        teleop_ready = self.runtime / "teleop-ready.json"
        self.teleop = self.spawn(
            [
                self.args.gdk_python,
                "-u",
                str(HERE / "dagger_send_runtime.py"),
                "--hz",
                "100",
                "--scale",
                "0.5",
                "--max-joint-vel",
                "0.5",
                "--collect-socket",
                str(self.collector_socket),
                "--control-side",
                "right",
                "--yes",
                "--external-record-control",
                "--activation-file",
                str(self.teleop_active),
                "--inactive-ready-file",
                str(self.teleop_inactive),
                "--gui-event-file",
                str(self.xr_events),
                "--action-log-hz",
                "30",
                "--controller-timeout",
                "0.5",
                "--ready-file",
                str(teleop_ready),
            ]
        )
        wait_file(teleop_ready, self.teleop, 30, "XR 遥操作")

        self.model = self.spawn(
            [
                self.args.model_python,
                str(HERE / "diffusion_policy_server.py"),
                "--checkpoint",
                str(checkpoint),
                "--socket",
                str(self.model_socket),
                "--image-fit",
                "center-crop",
                "--prefetch-actions",
                "7",
                "--num-inference-steps",
                "10",
                "--sampling-seed",
                "1000",
            ]
        )
        deadline = time.monotonic() + 60
        while not self.model_socket.exists():
            if self.model.poll() is not None:
                raise RuntimeError(f"模型服务启动失败（{self.model.returncode}）")
            if time.monotonic() >= deadline:
                raise TimeoutError("模型服务启动超时")
            time.sleep(0.1)

        initialized = self.runtime / "inference-initialized.json"
        inference_ready = self.runtime / "inference-ready.json"
        inactive_ready = self.runtime / "inference-inactive.json"
        self.inference_active.write_text("inactive")
        self.inference = self.spawn(
            [
                self.args.gdk_python,
                str(HERE / "agibot_diffusion_infer.py"),
                "--model-socket",
                str(self.model_socket),
                "--collect-socket",
                str(self.collector_socket),
                "--ready-file",
                str(inference_ready),
                "--initialized-file",
                str(initialized),
                "--inactive-ready-file",
                str(inactive_ready),
                "--activation-file",
                str(self.inference_active),
                "--hz",
                "30",
                "--training-hz",
                "30",
                "--max-joint-vel",
                "1.0",
                "--max-normalized-state",
                "2.0",
                # Preview, recording and inference intentionally remain live
                # together. Hand-camera frames are large and can arrive a few
                # milliseconds after their bundle, so wait up to 20 ms while
                # retaining the exact-timestamp integrity check.
                "--camera-retries",
                "4",
                "--camera-retry-sleep-ms",
                "5",
                "--quintic-steps",
                "5",
                "--action-filter-alpha",
                "0.2",
                "--handoff-steps",
                "4",
                "--yes",
            ],
            env=self.inference_env,
        )
        wait_file(initialized, self.inference, 60, "模型推理")

        # Do not include checkpoint/model startup in the full DAgger episode.
        # Start the collector only after inference has loaded successfully, but
        # before activating it, so the first policy command is still recorded.
        self.publish(message="模型推理已加载，正在开始录制")
        self.control.send("start", pc_ts_ns=time.time_ns())
        recording = wait_json(self.collector_status, "recording", self.collector, 30)
        self.session_dir = Path(recording["episode_dir"])
        self.control.send("marker", name="session_start", mode="policy", pc_ts_ns=time.time_ns())

        self.publish(message="录制已开始，正在等待头部和右手相机同步画面")
        wait_camera_frames(
            Path(self.args.preview_dir),
            ("head_color", "hand_right_color"),
            self.collector,
            15,
        )

        inference_ready.unlink(missing_ok=True)
        self.inference_active.write_text("active")
        wait_file(inference_ready, self.inference, 20, "模型首条命令")
        self.publish("inference", "模型推理中")

    def takeover(self) -> None:
        if self.state != "inference":
            return
        self.publish("switching", "正在切到人工接管")
        self.control.send("marker", name="takeover", mode="expert", pc_ts_ns=time.time_ns())
        inactive = self.runtime / "inference-inactive.json"
        inactive.unlink(missing_ok=True)
        self.inference_active.write_text("inactive")
        wait_file(inactive, self.inference, 3, "停止策略命令")
        self.teleop_active.write_text("active")
        self.publish("teleop", "人工接管中；持续记录 expert 动作")

    def resume(self) -> None:
        if self.state != "teleop":
            return
        self.publish("switching", "正在从当前位置平滑恢复模型推理")
        self.teleop_inactive.unlink(missing_ok=True)
        self.teleop_active.write_text("inactive")
        wait_file(self.teleop_inactive, self.teleop, 3, "停止人工控制命令")
        self.control.send("marker", name="resume", mode="policy", pc_ts_ns=time.time_ns())
        ready = self.runtime / "inference-ready.json"
        ready.unlink(missing_ok=True)
        self.inference_active.write_text("active")
        wait_file(ready, self.inference, 10, "恢复模型推理")
        self.publish("inference", "模型推理中；已从当前机器人状态重新规划")

    def end(
        self,
        save_mode: str = "corrections",
        pre_seconds: float | None = None,
        post_seconds: float | None = None,
    ) -> None:
        if self.state not in {"inference", "teleop"}:
            return
        self.publish("stopping", "正在停止控制并保存数据")
        self.teleop_active.write_text("inactive")
        self.inference_active.write_text("inactive")
        self.control.send("marker", name="session_end", mode="idle", pc_ts_ns=time.time_ns())
        stop_process(self.inference)
        self.inference = None
        stop_process(self.model)
        self.model = None
        stop_process(self.teleop)
        self.teleop = None
        self.control.send("stop", pc_ts_ns=time.time_ns())
        saved = wait_json(self.collector_status, "saved", self.collector, 1800)
        self.session_dir = Path(saved["episode_dir"])
        self.control.send("shutdown", pc_ts_ns=time.time_ns())
        self.control.close()
        self.control = None
        try:
            self.collector.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stop_process(self.collector)
        self.collector = None
        pre_seconds = self.args.pre_seconds if pre_seconds is None else pre_seconds
        post_seconds = self.args.post_seconds if post_seconds is None else post_seconds
        self.publish("saving", "正在保存完整轨迹和纠错片段")
        episodes: list[tuple[Path, str]] = []
        notes: list[str] = []
        output_root = Path(self.args.output).expanduser()
        try:
            synchronization = synchronize_episode_frames(self.session_dir)
        except (OSError, ValueError) as exc:
            self.publish("finished", f"数据同步检查失败：{exc}")
            return
        dropped = synchronization["dropped_frames"]
        if dropped:
            notes.append(f"已移除 {dropped} 个相机/状态不完整帧")
        try:
            episodes.extend(
                (episode, "corrections")
                for episode in save_correction_windows(
                    self.session_dir, output_root, pre_seconds, post_seconds,
                )
            )
        except ValueError as exc:
            notes.append(str(exc))
        try:
            episodes.append(
                (save_full_episode(self.session_dir, output_root), "full")
            )
        except (OSError, ValueError) as exc:
            self.publish("finished", f"数据保存失败：{exc}")
            return
        for episode, save_mode in episodes:
            marker = episode / ".data-platform-ready.json"
            atomic_json(
                marker,
                {
                    "ready_at_ns": time.time_ns(),
                    "import_task_id": f"dagger-session-{self.args.session_id}",
                    "import_folder": f"dagger-{self.args.session_id[:8]}",
                    "teleop_session_id": self.args.session_id,
                    "requested_cameras": [],
                    "source": "intervention",
                    "collection_trigger": "dagger",
                    "dagger_save_mode": save_mode,
                    **self.platform_context,
                },
            )
        message = f"已保存 {len(episodes)} 段数据，正在同步到数据列表"
        if notes:
            message += "；" + "；".join(notes)
        self.publish("finished", message, saved_count=len(episodes))

    def commands(self) -> list[dict]:
        path = Path(self.args.command_file)
        if not path.exists():
            return []
        with path.open() as stream:
            stream.seek(self.command_offset)
            rows = stream.readlines()
            self.command_offset = stream.tell()
        commands: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row)
                if isinstance(payload, dict):
                    commands.append(payload)
            except json.JSONDecodeError:
                pass
        return commands

    def xr_commands(self) -> list[dict]:
        with self.xr_events.open() as stream:
            stream.seek(self.event_offset)
            rows = stream.readlines()
            self.event_offset = stream.tell()
        result = []
        for row in rows:
            try:
                event = json.loads(row).get("event")
            except json.JSONDecodeError:
                continue
            if event == "grip_takeover":
                result.append({"command": "takeover"})
            elif event == "resume_inference":
                result.append({"command": "resume"})
            elif event == "end_session":
                result.append({"command": "end", "save_mode": "corrections"})
        return result

    def run(self) -> int:
        self.publish()
        try:
            self.start()
            while not self.stop_requested:
                for payload in self.xr_commands() + self.commands():
                    command = payload.get("command")
                    if command == "takeover":
                        self.takeover()
                    elif command == "resume":
                        self.resume()
                    elif command == "end":
                        self.end(
                            str(payload.get("save_mode", "corrections")),
                            payload.get("pre_seconds"),
                            payload.get("post_seconds"),
                        )
                    elif command == "shutdown" and self.state not in ACTIVE_STATES:
                        self.stop_requested = True
                for process, label in (
                    (self.teleop, "XR 遥操作"),
                    (self.model, "模型服务"),
                    (self.inference, "模型推理"),
                ):
                    if (
                        self.state in {"inference", "teleop"}
                        and process is not None
                        and process.poll() is not None
                    ):
                        raise RuntimeError(f"{label}异常退出（{process.returncode}）")
                time.sleep(0.05)
            return 0
        except Exception as exc:
            self.publish("error", str(exc))
            return 1
        finally:
            if self.control is not None:
                try:
                    self.control.send("stop", pc_ts_ns=time.time_ns())
                    self.control.send("shutdown", pc_ts_ns=time.time_ns())
                except Exception:
                    pass
                self.control.close()
            for process in (self.inference, self.model, self.teleop, self.collector):
                stop_process(process)
            self.log.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--pre-seconds", type=float, default=2.0)
    parser.add_argument("--post-seconds", type=float, default=2.0)
    parser.add_argument("--output", required=True)
    parser.add_argument("--collection-project", required=True)
    parser.add_argument("--gdk-python", required=True)
    parser.add_argument("--model-python", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--command-file", required=True)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--preview-dir", required=True)
    args = parser.parse_args()
    if args.pre_seconds < 0 or args.post_seconds < 0:
        parser.error("纠正前后秒数不能为负数")
    return args


if __name__ == "__main__":
    raise SystemExit(Runner(parse_args()).run())
