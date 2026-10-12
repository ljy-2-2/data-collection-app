#!/usr/bin/env python3
"""Supervise the HIL-SERL Learner/Actor pair for the web control surface."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

TELEOP_RUNTIME = Path(__file__).resolve().parents[2] / "vendor" / "teleop_runtime"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


class Supervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.root = args.root.resolve()
        self.session_id = args.session_id
        self.request = json.loads(args.request_file.read_text(encoding="utf-8"))
        self.status_file = args.status_file
        self.command_file = args.command_file
        self.log_file = args.log_file
        self.log = self.log_file.open("a", encoding="utf-8", buffering=1)
        self.stop_event = threading.Event()
        self.children: list[subprocess.Popen] = []
        self.command_offset = 0
        self.temporary_paths: list[Path] = []
        self.status_payload = {
            "state": "validating",
            "mode": self.request["mode"],
            "message": "正在检查训练配置……",
            "session_id": self.session_id,
            "started_at": datetime.now(UTC).isoformat(),
            "scene_waiting": False,
        }

    def status(self, state: str, message: str, **values) -> None:
        self.status_payload.update(state=state, message=message, **values)
        write_json(self.status_file, self.status_payload)
        self.log.write(f"[platform] {message}\n")

    def stop_children(self) -> None:
        for process in reversed(self.children):
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def wait_file(
        self, process: subprocess.Popen, marker: Path, role: str, timeout: int = 240
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if marker.is_file():
                return
            code = process.poll()
            if code is not None:
                raise RuntimeError(f"{role} 启动失败（{code}），请查看日志")
            time.sleep(0.2)
        if self.stop_event.is_set():
            raise InterruptedError
        raise RuntimeError(f"{role} 在 {timeout} 秒内未就绪")

    def _latest_demo(self) -> Path | None:
        root = self.root / "demo_data"
        choices = (
            [path for path in root.iterdir() if path.is_dir() and any(path.glob("*.pkl"))]
            if root.is_dir()
            else []
        )
        return max(choices, key=lambda path: path.stat().st_mtime, default=None)

    def _latest_bc(self) -> Path | None:
        root = self.root / "checkpoints"
        choices = (
            [
                path
                for path in root.iterdir()
                if path.is_dir() and "bc" in path.name.lower() and any(path.glob("checkpoint_*"))
            ]
            if root.is_dir()
            else []
        )
        return max(choices, key=lambda path: path.stat().st_mtime, default=None)

    @staticmethod
    def _checkpoint_step(path: Path) -> int:
        try:
            return int(path.name.removeprefix("checkpoint_"))
        except ValueError:
            return -1

    def _selected_checkpoint(self, value: str | Path) -> tuple[Path, Path]:
        path = Path(value).expanduser().resolve()
        if path.is_dir() and path.name.startswith("checkpoint_"):
            return path.parent, path
        choices = [item for item in path.glob("checkpoint_*") if item.is_dir()]
        if not path.is_dir() or not choices:
            raise RuntimeError(f"无效的 checkpoint: {path}")
        return path, max(choices, key=self._checkpoint_step)

    @staticmethod
    def _clone_tree(source: Path, destination: Path) -> None:
        def link_or_copy(src: str, dst: str) -> str:
            try:
                os.link(src, dst)
                return dst
            except OSError:
                return shutil.copy2(src, dst)

        shutil.copytree(source, destination, copy_function=link_or_copy)

    def prepare(self, run_dir: Path) -> tuple[Path, Path, Path | None, Path | None]:
        mode = self.request["mode"]
        demo = (
            Path(self.request["demo_path"]).expanduser().resolve()
            if self.request.get("demo_path")
            else self._latest_demo()
        )
        if demo is None:
            raise RuntimeError("没有可用的 demo data")
        bot_python = Path(
            os.environ.get("BOT_PY", str(Path.home() / "miniforge3/envs/bot/bin/python"))
        ).expanduser()
        if not bot_python.is_file():
            raise RuntimeError(f"bot Python 不存在: {bot_python}")
        if not demo.is_dir():
            raise RuntimeError(f"demo data 文件夹不存在: {demo}")
        if not any(demo.glob("*.pkl")):
            task_slug = re.sub(r"[^\w.-]+", "_", self.request["task_name"]).strip("_")
            task_slug = task_slug or "agibot_can_pick"
            destination = self.root / "demo_data" / f"{task_slug}_{datetime.now():%Y%m%d_%H%M%S}"
            self.status("converting", "正在将 Ego/GDK 数据转换为 HIL-SERL pickle……")
            result = subprocess.run(
                [
                    str(bot_python),
                    str(self.root / "examples/convert_agibot_right_to_hilserl.py"),
                    "--input",
                    str(demo),
                    "--output",
                    str(destination),
                    "--terminal-reward",
                    "1",
                ],
                cwd=self.root,
                stdout=self.log,
                stderr=subprocess.STDOUT,
            )
            if result.returncode:
                raise RuntimeError(f"demo data 转换失败（{result.returncode}）")
            demo = destination
        if not demo.is_dir() or not any(demo.glob("*.pkl")):
            raise RuntimeError(f"demo 目录中没有 pickle: {demo}")
        replay = (
            Path(self.request["replay_path"]).expanduser().resolve()
            if self.request.get("replay_path")
            else None
        )
        if replay is not None and not replay.exists():
            raise RuntimeError(f"replay pickle 不存在: {replay}")

        if mode == "fresh":
            name = f"agibot_can_pick_{datetime.now():%Y%m%d_%H%M%S}"
            checkpoint = self.root / "checkpoints" / name
            checkpoint.mkdir(parents=True, exist_ok=False)
        elif mode == "bc":
            task_slug = re.sub(r"[^\w.-]+", "_", self.request["task_name"]).strip("_")
            task_slug = task_slug or "agibot_can_pick"
            name = f"{task_slug}_bc_{datetime.now():%Y%m%d_%H%M%S}"
            checkpoint = self.root / "checkpoints" / name
            checkpoint.mkdir(parents=True, exist_ok=False)
        else:
            if not self.request.get("checkpoint_path"):
                raise RuntimeError("续训必须选择 checkpoint")
            source_root, selected = self._selected_checkpoint(self.request["checkpoint_path"])
            task_slug = re.sub(r"[^\w.-]+", "_", self.request["task_name"]).strip("_")
            name = task_slug or "agibot_can_pick"
            checkpoint = self.root / "checkpoints" / name
            if checkpoint != source_root:
                if checkpoint.exists():
                    raise RuntimeError(f"任务 checkpoint 已存在: {checkpoint}")
                checkpoint.mkdir(parents=True)
                self._clone_tree(selected, checkpoint / selected.name)
                for data_name in ("buffer", "demo_buffer"):
                    source = source_root / data_name
                    if source.is_dir():
                        self._clone_tree(source, checkpoint / data_name)
            # A replay already inside the selected checkpoint is part of the
            # inherited buffer. Passing it again would duplicate those samples.
            if replay is not None and (replay == source_root or source_root in replay.parents):
                replay = None

        bc = None
        if mode == "fresh" and self.request.get("use_bc", True):
            selected_bc = (
                Path(self.request["bc_checkpoint_path"]).expanduser().resolve()
                if self.request.get("bc_checkpoint_path")
                else self._latest_bc()
            )
            if selected_bc is None:
                raise RuntimeError("已选择 BC 预训练，但没有可用的 BC checkpoint")
            bc_root, bc_checkpoint = self._selected_checkpoint(selected_bc)
            if selected_bc == bc_root:
                bc = bc_root
            else:
                bc = run_dir / "selected-bc"
                bc.mkdir(parents=True, exist_ok=False)
                self._clone_tree(bc_checkpoint, bc / bc_checkpoint.name)
                self.temporary_paths.append(bc)
        return checkpoint, demo, bc, replay

    def poll_commands(self, scene_request: Path) -> None:
        try:
            with self.command_file.open(encoding="utf-8") as stream:
                stream.seek(self.command_offset)
                rows = stream.readlines()
                self.command_offset = stream.tell()
        except OSError:
            return
        for row in rows:
            try:
                command = json.loads(row).get("command")
            except json.JSONDecodeError:
                continue
            if command == "stop":
                self.stop_event.set()
            elif command == "scene_ready" and self.status_payload.get("scene_waiting"):
                scene_request.write_text("ready\n", encoding="utf-8")
                self.status("running", "已确认场景，等待 Actor 开始采样……", scene_waiting=False)

    def update_bc_progress(self, progress_file: Path, total: int) -> None:
        try:
            tail = self.log_file.read_bytes()[-128 * 1024 :].decode(errors="replace")
        except OSError:
            return
        matches = re.findall(r"(\d+)\s*/\s*(\d+)", tail)
        current = next(
            (int(value) for value, found_total in reversed(matches) if int(found_total) == total),
            0,
        )
        write_json(
            progress_file,
            {"phase": "BC training", "current": min(current, total), "total": total},
        )

    def run(self) -> int:
        try:
            self.status("validating", "正在检查 demo、checkpoint 和训练环境……")
            run_dir = self.status_file.parent / self.status_file.stem.replace(".status", "")
            run_dir.mkdir(parents=True, exist_ok=True)
            checkpoint, demo, bc, replay = self.prepare(run_dir)
            learner_ready = run_dir / "learner-ready"
            actor_ready = run_dir / "actor-ready"
            learner_progress = run_dir / "learner-progress.json"
            actor_progress = run_dir / "actor-progress.json"
            reset_waiting = run_dir / "reset-waiting"
            scene_request = run_dir / "reset-request"
            reset_confirmed = run_dir / "reset-confirmed"
            self.status_payload.update(
                checkpoint_path=str(checkpoint),
                demo_path=str(demo),
                learner_progress_file=str(learner_progress),
                actor_progress_file=str(actor_progress),
            )
            if self.request["mode"] == "bc":
                self.update_bc_progress(learner_progress, self.request["train_steps"])
                env = os.environ.copy()
                env.update(
                    PYTHONUNBUFFERED="1",
                    BC_DEMO_PATH=str(demo),
                    BC_CHECKPOINT_PATH=str(checkpoint),
                    BC_TRAIN_STEPS=str(self.request["train_steps"]),
                )
                self.status(
                    "training_bc",
                    "正在训练 BC 预训练模型……",
                    checkpoint_path=str(checkpoint),
                    demo_path=str(demo),
                )
                process = subprocess.Popen(
                    [str(self.root / "examples/experiments/agibot_can_pick/run_bc_pretrain.sh")],
                    cwd=self.root,
                    env=env,
                    stdout=self.log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self.children.append(process)
                while process.poll() is None and not self.stop_event.is_set():
                    self.poll_commands(scene_request)
                    self.update_bc_progress(learner_progress, self.request["train_steps"])
                    time.sleep(0.2)
                if self.stop_event.is_set():
                    self.status("stopping", "正在停止 BC 训练……")
                    self.stop_children()
                    self.status("finished", "BC 训练已停止")
                    return 0
                if process.returncode:
                    raise RuntimeError(f"BC 训练失败（{process.returncode}）")
                write_json(
                    learner_progress,
                    {
                        "phase": "BC training",
                        "current": self.request["train_steps"],
                        "total": self.request["train_steps"],
                    },
                )
                self.status("finished", "BC 预训练模型已保存")
                return 0
            common = [
                f"--ip={self.request.get('learner_ip', '127.0.0.1')}",
                f"--checkpoint_path={checkpoint}",
            ]
            learner_command = [
                str(self.root / "examples/experiments/agibot_can_pick/run_learner.sh"),
                f"--demo_path={demo}",
                f"--ready_file={learner_ready}",
                f"--progress_file={learner_progress}",
                "--debug",
                *common,
            ]
            if bc is not None:
                learner_command.append(f"--bc_checkpoint_path={bc}")
            if replay is not None and checkpoint not in replay.parents:
                learner_command.append(f"--replay_path={replay}")
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["HILSERL_NONINTERACTIVE_RESUME"] = "1"
            runtime_python_paths = [
                str(TELEOP_RUNTIME),
                str(TELEOP_RUNTIME / "src"),
            ]
            runtime_python_paths.extend(
                item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item
            )
            env["PYTHONPATH"] = os.pathsep.join(
                dict.fromkeys(runtime_python_paths)
            )
            if self.request["mode"] == "fresh":
                env["HILSERL_FRESH_START"] = "1"
            else:
                env.pop("HILSERL_FRESH_START", None)

            self.status("starting_learner", "正在启动 Learner 并加载数据……")
            learner = subprocess.Popen(
                learner_command,
                cwd=self.root,
                env=env,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.children.append(learner)
            self.wait_file(learner, learner_ready, "Learner", self.request["startup_timeout"])

            actor_env = env.copy()
            actor_env.update(
                AGIBOT_GUI_CONFIRMED="YES",
                AGIBOT_RESET_WAITING_FILE=str(reset_waiting),
                AGIBOT_RESET_REQUEST_FILE=str(scene_request),
                AGIBOT_RESET_CONFIRMED_FILE=str(reset_confirmed),
            )
            actor_command = [
                str(self.root / "examples/experiments/agibot_can_pick/run_actor.sh"),
                f"--ready_file={actor_ready}",
                f"--progress_file={actor_progress}",
                *common,
            ]
            self.status("starting_actor", "Learner 已就绪，正在启动 Actor……")
            actor = subprocess.Popen(
                actor_command,
                cwd=self.root,
                env=actor_env,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.children.append(actor)
            self.wait_file(actor, actor_ready, "Actor", self.request["startup_timeout"])
            self.status("resetting", "Actor 已连接，机器人正在复位……")

            while not self.stop_event.is_set():
                self.poll_commands(scene_request)
                if (
                    reset_waiting.is_file()
                    and not reset_confirmed.is_file()
                    and not self.status_payload.get("scene_waiting")
                ):
                    self.status(
                        "waiting_scene",
                        "复位完成：按 Pico A，或点击“场景已就绪”",
                        scene_waiting=True,
                    )
                if reset_confirmed.is_file() and self.status_payload.get("state") != "running":
                    self.status(
                        "running", "采样中：Y 成功，B 失败，Grip 人工接管", scene_waiting=False
                    )
                failed = next(
                    (
                        (name, process.returncode)
                        for name, process in (("Learner", learner), ("Actor", actor))
                        if process.poll() is not None
                    ),
                    None,
                )
                if failed:
                    raise RuntimeError(f"{failed[0]} 已退出（{failed[1]}）")
                time.sleep(0.2)
            self.status("stopping", "正在安全停止 Actor 和 Learner……", scene_waiting=False)
            self.stop_children()
            self.status("finished", "训练已停止，最新模型和数据已保存", scene_waiting=False)
            return 0
        except InterruptedError:
            self.stop_children()
            self.status("finished", "训练已停止", scene_waiting=False)
            return 0
        except Exception as exc:
            self.log.write(f"[platform error] {exc}\n")
            self.stop_children()
            self.status("error", str(exc), scene_waiting=False)
            return 1
        finally:
            for path in self.temporary_paths:
                shutil.rmtree(path, ignore_errors=True)
            self.log.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--command-file", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    args = parser.parse_args()
    supervisor = Supervisor(args)

    def stop(_signum, _frame):
        supervisor.stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
