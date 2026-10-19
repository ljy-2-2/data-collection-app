"""Tianji A/B collection, reusing the app's Episode writer and upload queue."""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .collection_uploader import READY_MARKER
from .collection_gate import collection_available, collection_context
from .platform_auth import current_platform_username

APP_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(APP_ROOT / "fusion_camera_stream"))
sys.path.insert(0, str(APP_ROOT / "fusion_data_collection"))
from FusionDataCollector.collector import Buttons, Episode, dump, finite_number, readiness  # noqa: E402

CAMERA_MAPPING = {"usb-1": "head_color", "usb-2": "right_color", "usb-4": "left_color"}


class TianjiCollection:
    def __init__(self, manager, output=None, episode_factory=Episode):
        self.manager = manager
        self.output = Path(output or APP_ROOT / "teleop_control/runtime/tianji/episodes")
        self.episode_factory = episode_factory
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.monitor = self.job = None
        self.episode = None
        self.latest = None
        self.armed = False
        self.phase = "idle"
        self.error = ""
        self.completed = 0
        self.uploader = None
        self.buttons = Buttons()
        self.session = ""
        self.task = ""
        self.controlled_sides = ("left", "right")
        self.upload_sides = ("left", "right")
        self.parameters = {}
        self.owner = "unknown"
        self.last_episode = None
        self.last_gripper_fault = None
        self.gripper_fault_active = False
        self.gripper_action_cache = {}
        self.gripper_action_session = None

    def arm(self, request):
        with self.lock:
            if self.active():
                raise RuntimeError("上一轮数据尚在保存，请稍后再开始遥操作")
            self.session = uuid.uuid4().hex
            self.task = request["task"]
            mode = request.get("mode", "dual")
            self.controlled_sides = ("left", "right") if mode == "dual" else (mode,)
            upload_mode = request.get("upload_mode") or mode
            self.upload_sides = ("left", "right") if upload_mode == "dual" else (upload_mode,)
            self.parameters = dict(request.get("parameters", {}))
            self.owner = current_platform_username()
            self.buttons = Buttons()
            self.latest = None
            self.gripper_action_cache = {}
            self.gripper_action_session = None
            self.armed, self.phase, self.error = True, "ready", ""
            self.done.clear()
            if not self.monitor or not self.monitor.is_alive():
                self.monitor = threading.Thread(
                    target=self._watch, daemon=True, name="tianji-record-watch"
                )
                self.monitor.start()

    def active(self):
        return bool(self.job and self.job.is_alive())

    def _gripper_action_reference(self, row):
        """G2-style recording cache; never write a target into motor control state."""
        if row.get("session") != self.gripper_action_session:
            self.gripper_action_cache.clear()
            self.gripper_action_session = row.get("session")
        stamp = row.get("pc_monotonic_ns")
        motors = (row.get("grippers") or {}).get("motors") or {}
        live = (row.get("mode") == "live" and row.get("teleop_state") == "running"
                and not (row.get("grippers") or {}).get("fault")
                and finite_number(stamp) and 0 <= time.monotonic_ns() - stamp <= 300_000_000)
        references = {}
        for side in self.controlled_sides:
            motor = motors.get(side) or {}
            fb = motor.get("feedback") or {}
            fresh = (live and finite_number(fb.get("position"))
                     and finite_number(fb.get("received"))
                     and 0 <= stamp / 1e9 - fb["received"] <= .15
                     and fb.get("state") in (0, 1))
            target_available = (fresh and finite_number(motor.get("target_rad"))
                                and motor.get("state") in ("ACTIVE", "ENABLING"))
            if fresh and side not in self.gripper_action_cache:
                self.gripper_action_cache[side] = dict(
                    position_rad=fb["position"], source="initial_feedback",
                    source_pc_monotonic_ns=round(fb["received"] * 1e9))
            if target_available:
                self.gripper_action_cache[side] = dict(
                    position_rad=motor["target_rad"], source="last_controller_target",
                    source_pc_monotonic_ns=stamp)
            references[side] = dict(
                self.gripper_action_cache.get(side, dict(position_rad=None, source="unavailable",
                                                         source_pc_monotonic_ns=None)),
                controller_target_available=bool(target_available),
            )
        return references

    def accept(self, row):
        # Runs in the SDK IPC reader: never wait for cameras, disk or uploads here.
        with self.lock:
            self.latest = row
            grippers = row.get("grippers") or {}
            fault = bool(grippers.get("fault"))
            if fault and not self.gripper_fault_active:
                # Retain the first fault frame after control stops, without disk
                # I/O in the SDK reader or replacing it with recovery feedback.
                self.last_gripper_fault = {
                    "pc_monotonic_ns": row.get("pc_monotonic_ns"),
                    "pc_unix_ns": row.get("pc_unix_ns"),
                    "teleop_state": row.get("teleop_state"),
                    "grippers": copy.deepcopy(grippers),
                }
            self.gripper_fault_active = fault
            if not self.armed:
                return
            row = dict(row, gripper_action_reference=self._gripper_action_reference(row))
            self.latest = row
            action = self.buttons.accept(row)
            if self.active() and self.episode:
                self.episode.submit(row)
                if action == "stop":
                    self.episode.stop("B/Y")
                if not self.episode.stopping.is_set():
                    reason = readiness(row, required_sides=self.controlled_sides)
                    if row.get("teleop_state") != "running":
                        reason = "遥操作已停止或发生故障"
                    if reason:
                        self.episode.stop("feedback_not_ready", reason)
            elif action == "start":
                try:
                    self.start()
                except (ValueError, RuntimeError) as exc:
                    self.error = str(exc)

    def start(self):
        if not collection_available():
            return
        with self.lock:
            if self.active():
                return
            if not self.armed or not self.latest or self.latest.get("teleop_state") != "running":
                raise ValueError("请先开始遥操作，等待手柄和机械臂反馈就绪")
            reason = readiness(self.latest, required_sides=self.controlled_sides)
            if reason:
                raise ValueError(reason)
            context = collection_context()
            args = SimpleNamespace(
                output=str(self.output),
                task=self.task,
                max_seconds=600,
                retain_native=False,
                controlled_sides=self.controlled_sides,
                robot_type="tianji",
                parameters=self.parameters,
                record_actions=True,
                gripper_action_policy="initial_feedback_then_last_target",
                recover_camera=False,
                adopt_camera_stream=True,
                on_stream_released=self.manager.resume_cameras_after_episode,
            )
            self.episode = self.episode_factory(
                args,
                CAMERA_MAPPING,
                self.latest,
                client_factory=self.manager.adopt_camera_stream,
                episode_index=self.completed + 1,
            )
            self.phase, self.error = "starting", ""
            # Capture session ownership before this background operation begins.
            info = {
                "import_task_id": "tianji-" + self.session,
                "import_folder": self.task,
                "teleop_session_id": self.session,
                "requested_cameras": list(CAMERA_MAPPING.values()),
                "source": "teleop",
                "collection_trigger": "controller",
                "author": self.owner,
                "upload_sides": list(self.upload_sides),
                **context,
            }
            self.job = threading.Thread(
                target=self._record, args=(self.episode, info), name="tianji-episode", daemon=True
            )
            self.job.start()

    def _record(self, episode, info):
        try:
            if episode.stopping.is_set():
                raise RuntimeError(episode.error or "开始采集前已收到结束指令")
            episode.run()
            if episode.error:
                raise RuntimeError(episode.error)
            metadata = json.loads((episode.directory / "meta_info.json").read_text())
            if not metadata.get("complete") or not metadata.get("actions_recorded"):
                raise RuntimeError("本轮数据不完整，已保留本地文件，不加入上传队列")
            metadata["author"] = info["author"]
            metadata["teleop_session_id"] = info["teleop_session_id"]
            metadata["upload_sides"] = info["upload_sides"]
            if "upload_destination" in info:
                metadata["upload_destination"] = info["upload_destination"]
            dump(episode.directory / "meta_info.json", metadata)
            # Last write: the uploader must never see a partially written Episode.
            dump(episode.directory / READY_MARKER, info)
            with self.lock:
                self.completed += 1
                self.phase = "saved"
        except Exception as exc:
            episode.error = episode.error or str(exc)
            episode.finished.set()
            with self.lock:
                self.error, self.phase = str(exc), "incomplete"
        finally:
            with self.lock:
                self.last_episode = str(episode.directory) if episode.directory else None
            if self.armed and not self.done.is_set():
                self.manager.resume_cameras_after_episode()

    def stop(self, reason="B/Y", error=None):
        with self.lock:
            if self.active() and self.episode:
                self.episode.stop(reason, error)

    def disarm(self, reason="teleop_stopped"):
        with self.lock:
            self.armed = False
            self.stop(reason, "遥操作已结束，本轮未通过 B 键正常收尾")

    def _watch(self):
        while not self.done.wait(0.1):
            with self.lock:
                if self.active() and self.episode and not self.episode.finished.is_set():
                    if (
                        not self.latest
                        or time.monotonic_ns() - self.latest["pc_monotonic_ns"] > 300_000_000
                    ):
                        self.episode.stop("teleop_timeout", "遥操作反馈中断超过 300 ms")

    def snapshot(self):
        with self.lock:
            ep = self.episode
            phase = ep.state if self.active() and ep and not ep.finished.is_set() else self.phase
            if ep and ep.stopping.is_set() and self.active():
                phase = "finalizing"
            if not self.armed and not self.active() and phase == "ready":
                phase = "idle"
            started = ep.started_monotonic if ep else None
            return {
                "state": phase,
                "armed": self.armed,
                "busy": self.active(),
                "recording": phase == "recording",
                "error": self.error,
                "completed": self.completed,
                "episode": ep.directory.name if ep and ep.directory else None,
                "elapsed_s": max(0, time.monotonic() - started) if started and self.active() else 0,
                "frames": dict(ep.frame_counts) if ep else {},
                "last_episode": self.last_episode,
                "last_gripper_fault": copy.deepcopy(self.last_gripper_fault),
                "upload": self.uploader.status()
                if self.uploader
                else {"pending": 0, "uploaded": 0, "failed": 0},
            }

    def close(self):
        self.disarm("app_closed")
        self.done.set()
        if self.job:
            self.job.join(timeout=25)
            if self.job.is_alive():
                raise RuntimeError("数据仍在保存，请稍后再返回配置")
        if self.monitor:
            self.monitor.join(timeout=1)
