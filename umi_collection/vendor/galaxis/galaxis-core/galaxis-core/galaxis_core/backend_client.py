"""Backend API client for robot-facing endpoints."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from uuid import uuid4

import requests
import yaml

logger = logging.getLogger(__name__)


def create_backend(
    *,
    offline_mode: bool = False,
    task_file: str = "",
    base_url: str = "",
    api_key: str = "",
) -> BackendBase:
    """Factory: return the right backend implementation."""
    if offline_mode:
        return OfflineBackend(task_file)
    return ApiBackend(base_url, api_key)


class BackendBase(ABC):
    """Interface every backend must implement.

    All methods return parsed JSON on success or ``None`` on failure.
    Failures are logged but never raised — callers decide whether to block.
    """

    @abstractmethod
    def get_robot_self(self) -> dict | None: ...

    @abstractmethod
    def list_episodes(self) -> list | None: ...

    @abstractmethod
    def get_episode(self, episode_id: str) -> dict | None: ...

    @abstractmethod
    def start_episode(self, episode_id: str) -> dict | None: ...

    @abstractmethod
    def finish_episode(self, episode_id: str) -> dict | None: ...

    @abstractmethod
    def cancel_episode(self, episode_id: str, reason: str = "") -> dict | None: ...

    @abstractmethod
    def repeat_last_episode(self) -> dict | None: ...

    @abstractmethod
    def complete_subtask(self, episode_id: str, subtask_id: str) -> dict | None: ...

    @abstractmethod
    def skip_subtask(self, episode_id: str, subtask_id: str) -> dict | None: ...

    @abstractmethod
    def create_execution(self, episode_id: str, subtask_id: str) -> str | None: ...

    @abstractmethod
    def start_execution(self, episode_id: str, subtask_id: str, execution_id: str) -> dict | None: ...

    @abstractmethod
    def finish_execution(self, episode_id: str, subtask_id: str, execution_id: str) -> dict | None: ...

    @abstractmethod
    def cancel_execution(self, episode_id: str, subtask_id: str, execution_id: str) -> dict | None: ...

    @abstractmethod
    def update_robot_status(self, payload: dict) -> dict | None: ...

    def register_episode(self, enriched_task: dict) -> None:
        """Register an externally-provided episode for backends that need it.

        Default is a no-op — API backends already know their episodes.
        """


# ---------------------------------------------------------------------------
# HTTP backend (original behaviour)
# ---------------------------------------------------------------------------

class ApiBackend(BackendBase):
    """Talks to the Phase2 backend via robot-facing REST endpoints."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self._consecutive_failures = 0
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        })

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    REQUEST_TIMEOUT = 10.0  # seconds

    def _request(self, method: str, path: str, json_body: dict | None = None) -> dict | list | None:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, json=json_body, timeout=self.REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                self._consecutive_failures += 1
                lvl = logging.DEBUG if self._consecutive_failures > 3 else logging.ERROR
                logger.log(lvl, "API %s %s → %s: %s", method, path, resp.status_code, resp.text)
                return None
            self._consecutive_failures = 0
            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                logger.error("API %s %s returned invalid JSON", method, path)
                return None
        except requests.exceptions.RequestException as exc:
            self._consecutive_failures += 1
            lvl = logging.DEBUG if self._consecutive_failures > 3 else logging.ERROR
            logger.log(lvl, "API %s %s failed: %s", method, path, exc)
            return None

    # --- GET helpers ---

    def get_robot_self(self) -> dict | None:
        return self._request("GET", "/robot/me")

    def list_episodes(self) -> list | None:
        return self._request("GET", "/robot/episodes")

    def get_episode(self, episode_id: str) -> dict | None:
        return self._request("GET", f"/robot/episodes/{episode_id}")

    # --- Episode lifecycle ---

    def start_episode(self, episode_id: str) -> dict | None:
        return self._request("POST", f"/robot/episodes/{episode_id}/start", {
            "occurred_at": self._now_iso(),
        })

    def finish_episode(self, episode_id: str) -> dict | None:
        return self._request("POST", f"/robot/episodes/{episode_id}/finish", {
            "occurred_at": self._now_iso(),
        })

    def cancel_episode(self, episode_id: str, reason: str = "") -> dict | None:
        logger.info("Cancelling episode %s: %s", episode_id, reason)
        return self._request("POST", f"/robot/episodes/{episode_id}/cancel")

    def repeat_last_episode(self) -> dict | None:
        return self._request("POST", "/robot/episodes/repeat-last", {})

    # --- Subtask actions ---

    def complete_subtask(self, episode_id: str, subtask_id: str) -> dict | None:
        return self._request(
            "POST", f"/robot/episodes/{episode_id}/subtasks/{subtask_id}/complete",
        )

    def skip_subtask(self, episode_id: str, subtask_id: str) -> dict | None:
        return self._request(
            "POST", f"/robot/episodes/{episode_id}/subtasks/{subtask_id}/skip",
        )

    # --- Execution lifecycle ---

    def create_execution(self, episode_id: str, subtask_id: str) -> str | None:
        data = self._request(
            "POST", f"/robot/episodes/{episode_id}/subtasks/{subtask_id}/executions",
        )
        if data:
            return data.get("execution_id")
        return None

    def start_execution(self, episode_id: str, subtask_id: str, execution_id: str) -> dict | None:
        return self._request(
            "POST",
            f"/robot/episodes/{episode_id}/subtasks/{subtask_id}/executions/{execution_id}/start",
            {"occurred_at": self._now_iso()},
        )

    def finish_execution(self, episode_id: str, subtask_id: str, execution_id: str) -> dict | None:
        return self._request(
            "POST",
            f"/robot/episodes/{episode_id}/subtasks/{subtask_id}/executions/{execution_id}/finish",
            {"occurred_at": self._now_iso()},
        )

    def cancel_execution(self, episode_id: str, subtask_id: str, execution_id: str) -> dict | None:
        return self._request(
            "POST",
            f"/robot/episodes/{episode_id}/subtasks/{subtask_id}/executions/{execution_id}/cancel",
        )

    # --- Robot status ---

    def update_robot_status(self, payload: dict) -> dict | None:
        return self._request("PUT", "/robot/status", payload)


# ---------------------------------------------------------------------------
# Offline backend (YAML-seeded, in-memory)
# ---------------------------------------------------------------------------

_REQUIRED_EPISODE_KEYS = {"episodeId", "taskId", "task", "subtasks"}
_REQUIRED_TASK_KEYS = {"name"}
_REQUIRED_SUBTASK_KEYS = {"id", "name"}


class OfflineBackend(BackendBase):
    """Serves episodes from a YAML task file with no network access."""

    def __init__(self, task_file: str):
        if task_file:
            with open(task_file) as f:
                cfg = yaml.safe_load(f)
            self._robot = cfg["robot"]
            self._user = cfg.get("user", {"id": "unknown_operator"})
            self._tasks = cfg.get("tasks", [])
        else:
            # Bridge mode: no task file, episodes registered dynamically
            self._robot = {"id": "sim-bridge"}
            self._user = {"id": "sim-bridge-operator"}
            self._tasks = []
        self._episodes: dict[str, dict] = {}
        self._executions: dict[str, dict] = {}
        self._active_episode_id: str | None = None
        self._episode_counter = 0
        # 会话戳:保证每次启动生成的 Episode ID 全局唯一,历史回合文件永不冲突
        self._session_stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        if self._tasks:
            self._create_episode_from_task(self._tasks[0])

    def _create_episode_from_task(self, task: dict) -> dict:
        self._episode_counter += 1
        episode_id = f"offline-ep-{self._session_stamp}-{self._episode_counter:03d}"
        subtasks = []
        for st in task.get("subtasks", []):
            subtasks.append({
                "id": st["id"],
                "subtask_id": st["id"],
                "name": st["name"],
                "order_index": st.get("order_index", 0),
                "status": 0,
            })
        episode = {
            "id": episode_id,
            "task_id": task["id"],
            "task_name": task["name"],
            "task_description": task.get("description", ""),
            "task_version_id": "",
            "robot_id": self._robot["id"],
            "user_id": self._user["id"],
            "status": "pending",
            "subtasks": subtasks,
            # 任务定义中由采集入口写入的下一个回合序号,跨会话累积
            "task_round_index": int(task.get("round_index") or 0),
        }
        self._episodes[episode_id] = episode
        self._active_episode_id = episode_id
        return episode

    def register_episode(self, enriched_task: dict) -> None:
        """Register an externally-provided episode (enriched format from topic).

        When the sim_bridge publishes a task via /task_receiver/tasks,
        the backend doesn't know about that episode yet. This method
        registers it so start_episode / create_execution / etc. work.
        """
        missing = _REQUIRED_EPISODE_KEYS - enriched_task.keys()
        if missing:
            logger.error("register_episode: missing keys %s, skipping", missing)
            return

        missing_task = _REQUIRED_TASK_KEYS - enriched_task["task"].keys()
        if missing_task:
            logger.error("register_episode: task missing keys %s, skipping", missing_task)
            return

        for i, st in enumerate(enriched_task.get("subtasks", [])):
            missing_st = _REQUIRED_SUBTASK_KEYS - st.keys()
            if missing_st:
                logger.error("register_episode: subtask[%d] missing keys %s, skipping", i, missing_st)
                return

        episode_id = enriched_task["episodeId"]
        if episode_id in self._episodes:
            logger.debug("register_episode: episode %s already registered, skipping", episode_id)
            return

        subtasks = []
        for st in enriched_task.get("subtasks", []):
            subtasks.append({
                "id": st["id"],
                "subtask_id": st.get("subtask_id", st["id"]),
                "name": st["name"],
                "order_index": st.get("orderIndex", 0),
                "status": 0,
            })
        episode = {
            "id": episode_id,
            "task_id": enriched_task["taskId"],
            "task_version_id": enriched_task.get("taskVersionId", ""),
            "task_name": enriched_task["task"]["name"],
            "task_description": enriched_task["task"].get("description", ""),
            "robot_id": enriched_task.get("assignedRobotId", self._robot["id"]),
            "user_id": enriched_task.get("createdUserId", self._user["id"]),
            "status": "pending",
            "subtasks": subtasks,
        }
        self._episodes[episode_id] = episode
        self._active_episode_id = episode_id

    # --- GET helpers ---

    def get_robot_self(self) -> dict | None:
        return {"id": self._robot["id"], "active_episode_id": self._active_episode_id}

    def list_episodes(self) -> list | None:
        return list(self._episodes.values())

    def get_episode(self, episode_id: str) -> dict | None:
        return self._episodes.get(episode_id)

    # --- Episode lifecycle ---

    def start_episode(self, episode_id: str) -> dict | None:
        ep = self._episodes.get(episode_id)
        if ep:
            ep["status"] = "in_progress"
        return ep

    def finish_episode(self, episode_id: str) -> dict | None:
        ep = self._episodes.get(episode_id)
        if ep:
            ep["status"] = "finished"
        return ep

    def cancel_episode(self, episode_id: str, reason: str = "") -> dict | None:
        logger.info("Cancelling episode %s: %s", episode_id, reason)
        ep = self._episodes.get(episode_id)
        if ep:
            ep["status"] = "cancelled"
        return ep

    def repeat_last_episode(self) -> dict | None:
        if not self._episodes:
            return None
        # Find last completed episode so repeated calls clone from the same
        # source rather than chaining clone-of-clone-of-clone.
        last_ep = None
        for ep in reversed(list(self._episodes.values())):
            if ep["status"] in ("finished", "cancelled"):
                last_ep = ep
                break
        if last_ep is None:
            # No completed episodes yet — fall back to last created (first-run)
            last_ep = list(self._episodes.values())[-1]
        task_id = last_ep["task_id"]
        # Look in pre-loaded tasks first
        for t in self._tasks:
            if t["id"] == task_id:
                return self._create_episode_from_task(t)
        # Fall back to reconstructing from the episode (bridge mode)
        task = {
            "id": task_id,
            "name": last_ep.get("task_name", task_id),
            "description": last_ep.get("task_description", ""),
            "subtasks": [
                {"id": st["id"], "name": st["name"], "order_index": st.get("order_index", i)}
                for i, st in enumerate(last_ep.get("subtasks", []))
            ],
        }
        return self._create_episode_from_task(task)

    # --- Subtask actions ---

    def complete_subtask(self, episode_id: str, subtask_id: str) -> dict | None:
        ep = self._episodes.get(episode_id)
        if ep:
            for st in ep["subtasks"]:
                if st["id"] == subtask_id:
                    st["status"] = 1
                    break
        return ep

    def skip_subtask(self, episode_id: str, subtask_id: str) -> dict | None:
        ep = self._episodes.get(episode_id)
        if ep:
            for st in ep["subtasks"]:
                if st["id"] == subtask_id:
                    st["status"] = 2
                    break
        return ep

    # --- Execution lifecycle ---

    def create_execution(self, episode_id: str, subtask_id: str) -> str | None:
        exec_id = str(uuid4())
        self._executions[exec_id] = {
            "id": exec_id,
            "episode_id": episode_id,
            "subtask_id": subtask_id,
            "status": "created",
        }
        return exec_id

    def start_execution(self, episode_id: str, subtask_id: str, execution_id: str) -> dict | None:
        exc = self._executions.get(execution_id)
        if exc:
            exc["status"] = "in_progress"
        return exc

    def finish_execution(self, episode_id: str, subtask_id: str, execution_id: str) -> dict | None:
        exc = self._executions.get(execution_id)
        if exc:
            exc["status"] = "finished"
        return exc

    def cancel_execution(self, episode_id: str, subtask_id: str, execution_id: str) -> dict | None:
        exc = self._executions.get(execution_id)
        if exc:
            exc["status"] = "cancelled"
        return exc

    # --- Robot status ---

    def update_robot_status(self, payload: dict) -> dict | None:
        return {}


# Backwards-compatible alias — new code should use create_backend() instead.
BackendClient = ApiBackend
