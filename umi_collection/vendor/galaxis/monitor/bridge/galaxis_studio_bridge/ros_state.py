"""ROS 2 状态采集器。

在独立线程中运行 rclpy 订阅采集栈与 VR 桥接话题,聚合为 JSON 友好的状态字典;
页面通过 REST/WS 读取最新快照。

- 相机数量自适应:优先读取采集栈写入的采集 profile(active_cameras),
  ROS 模式下再做一次实际话题探测兜底。
- 编码器自适应:profile 的 active_arms / /collection_profile 广播决定显示左手/右手。
- VR 只保留位姿、追踪状态与消息帧率,不采集电量。
注意:订阅的话题名(如 /galaxis/gripper/*)是采集栈既有 ROS 接口,
改名会导致监看失效,因此保留原名;本代码不展示这些内部名称。
无 ROS 2 环境时自动降级为模拟数据源(``--mock``),用于界面开发与演示。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .config import Settings, preview_stream_url_for

logger = logging.getLogger("galaxis.studio.ros")

_CAMERA_KEYS = ("left", "right", "center")
_DEFAULT_PROFILE = {
    "collection_profile": "dual",
    "active_cameras": ["left", "right", "center"],
    "active_arms": ["left", "right"],
}


# ---------------------------------------------------------------------------
# 消息帧率估算:2 秒滑动窗口内的消息计数
# ---------------------------------------------------------------------------
class RateTracker:
    def __init__(self, window: float = 2.0) -> None:
        self.window = window
        self._stamps: deque[float] = deque()

    def _expire(self, now: float) -> None:
        cutoff = now - self.window
        while self._stamps and self._stamps[0] < cutoff:
            self._stamps.popleft()

    def tick(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        self._stamps.append(now)
        self._expire(now)
        return self._calculate()

    def rate(self, now: float | None = None) -> float:
        """只读查询当前速率,不写入样本。"""
        now = time.time() if now is None else now
        self._expire(now)
        return self._calculate()

    def _calculate(self) -> float:
        if len(self._stamps) < 2:
            return 0.0
        span = self._stamps[-1] - self._stamps[0]
        if span <= 0:
            return 0.0
        return round((len(self._stamps) - 1) / span, 1)


# ---------------------------------------------------------------------------
# 解析函数
# ---------------------------------------------------------------------------
def _parse_json_string(msg: Any) -> Any:
    try:
        return json.loads(msg.data)
    except (json.JSONDecodeError, AttributeError):
        return None


def _parse_joint_state(msg: Any) -> dict[str, Any]:
    names = list(getattr(msg, "name", []) or [])
    positions = list(getattr(msg, "position", []) or [])
    velocities = list(getattr(msg, "velocity", []) or [])
    return {
        "name": names[0] if names else "",
        # Preserve the recorded joint angle; round only after UI degree conversion.
        "position": float(positions[0]) if positions else 0.0,
        "velocity": round(float(velocities[0]), 4) if velocities else 0.0,
    }


def _parse_pico(msg: Any) -> dict[str, Any]:
    return {
        "pico_id": getattr(msg, "pico_id", ""),
        "seq": int(getattr(msg, "seq", 0)),
        "device_time_ns": int(getattr(msg, "device_time_ns", 0)),
        "pc_monotonic_ns": int(getattr(msg, "pc_monotonic_ns", 0)),
        "position": [
            round(float(msg.position.x), 4),
            round(float(msg.position.y), 4),
            round(float(msg.position.z), 4),
        ],
        "rotation": [
            round(float(msg.rotation.x), 4),
            round(float(msg.rotation.y), 4),
            round(float(msg.rotation.z), 4),
            round(float(msg.rotation.w), 4),
        ],
        "tracked": bool(getattr(msg, "tracked", True)),
        "valid": bool(getattr(msg, "valid", True)),
    }


def load_collection_profile(path: str | None) -> dict[str, Any]:
    """读取采集栈设备设置写出的采集 profile(与采集栈同一套规则)。

    文件不存在时返回默认双相机/双手 profile,保证页面始终可用。
    """
    profile = dict(_DEFAULT_PROFILE)
    if not path:
        return profile
    p = Path(path)
    if not p.is_file():
        return profile
    try:
        import yaml  # 延迟导入,避免无 PyYAML 环境报错

        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取采集 profile 失败,使用默认配置: %s", exc)
        return profile
    if isinstance(loaded, dict):
        profile.update(loaded)
    cameras = profile.get("active_cameras") or []
    if not isinstance(cameras, list):
        cameras = list(_DEFAULT_PROFILE["active_cameras"])
    cameras = [c for c in cameras if c in _CAMERA_KEYS]
    arms = profile.get("active_arms") or []
    if not isinstance(arms, list):
        arms = list(_DEFAULT_PROFILE["active_arms"])
    arms = [a for a in arms if a in ("left", "right")]
    profile["active_cameras"] = cameras or list(_DEFAULT_PROFILE["active_cameras"])
    profile["active_arms"] = arms or list(_DEFAULT_PROFILE["active_arms"])
    return profile


class RosStateCollector:
    """聚合 ROS 2 话题状态为最新快照。线程安全。"""

    def __init__(self, settings: Settings, mock: bool = False) -> None:
        self._settings = settings
        self._mock = mock
        self._lock = threading.RLock()
        self._start_ts = time.time()
        self._profile = load_collection_profile(settings.profile_path)
        self._state: dict[str, Any] = {
            "mode": "mock" if mock else "ros",
            "ros_connected": False,
            "recording": False,
            "task_status": "",
            "task_list": [],
            "task_name": "",
            "collection_profile": self._profile.get("collection_profile", "dual"),
            "active_cameras": list(self._profile["active_cameras"]),
            "active_arms": list(self._profile["active_arms"]),
            "episode_list": [],
            "metadata": None,
            "episode_elapsed": 0.0,
            "subtask_elapsed": 0.0,
            "storage_used": 0,
            "storage_free": 0,
            "episodes_completed_total": 0,
            "last_completed_at": None,
            "last_completed_uuid": "",
            "vr_hmd": None,
            "vr_left": None,
            "vr_right": None,
            "encoder_left": None,
            "encoder_right": None,
            "encoder_left_id": "",
            "encoder_right_id": "",
            "logs": [],
            "camera_info": {},
            "recording_gate_level": 2,
            "camera_quality_issues": [],
        }
        self._rates: dict[str, RateTracker] = {
            "vr_hmd": RateTracker(),
            "vr_left": RateTracker(),
            "vr_right": RateTracker(),
            "encoder_left": RateTracker(),
            "encoder_right": RateTracker(),
        }
        self._last_monitor_update: dict[str, float] = {}
        self._pico_diagnostic_at: float | None = None
        # recording_saved and recording_completed intentionally carry the same
        # round path. Listen to both for compatibility, but count each round
        # exactly once (HDF5 completion may arrive much later).
        self._counted_recordings: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------ state

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = json.loads(json.dumps(self._state, default=str))
            now = time.time()
            # A cached healthy diagnostic must never outlive a stopped bridge.
            state["pico_bridge"] = (
                state.get("pico_bridge", {})
                if self._pico_diagnostic_at is not None
                and time.monotonic() - self._pico_diagnostic_at < 3.0 else {}
            )
        state["ts"] = now
        state["cameras"] = [
            {
                "key": key,
                "label": self._settings.camera_labels.get(key, key.upper()),
                "topic": self._settings.camera_topics[key],
                "direct_url": preview_stream_url_for(
                    self._settings, self._settings.camera_topics[key]
                ),
                # The Python proxy remains a fallback only; the normal preview
                # uses web_video_server's 30 FPS C++ conversion directly.
                "proxy_url": f"/proxy/stream?topic={self._settings.camera_topics[key]}",
                "width": state.get('camera_info', {}).get(key, {}).get('width'),
                "height": state.get('camera_info', {}).get(key, {}).get('height'),
            }
            for key in state["active_cameras"]
            if key in self._settings.camera_topics
        ]
        state["task"] = self._task_summary(state)
        state["episode"] = self._episode_summary(state)
        state["encoders"] = self._encoder_summary(state)
        state["vr"] = self._vr_summary(state)
        state["storage_gb"] = {
            "used": round(state["storage_used"] / (1024 ** 3), 2),
            "free": round(state["storage_free"] / (1024 ** 3), 2),
            "total": round((state["storage_used"] + state["storage_free"]) / (1024 ** 3), 2)
            if state["storage_used"] or state["storage_free"]
            else None,
        }
        return state

    def log_event(self, level: str, message: str) -> None:
        with self._lock:
            self._state["logs"].append({"t": time.time(), "level": level, "msg": message})
            del self._state["logs"][:-40]

    # ------------------------------------------------------------------ rclpy

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ros-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        if self._mock:
            self._run_mock()
            return
        try:
            self._run_ros()
        except Exception as exc:  # noqa: BLE001
            logger.exception("ROS 监视连接失败: %s", exc)
            # Real sessions must never display synthetic device values after
            # a ROS failure. Mock data is allowed only with explicit mock mode.
            with self._lock:
                self._state['ros_connected'] = False
                self._state['recording_gate_level'] = 2
                self._state['monitor_error'] = str(exc)
                self._state['pico_bridge'] = {}
                self._pico_diagnostic_at = None

    def _run_ros(self) -> None:
        import rclpy  # 延迟导入:无 ROS 环境时不影响其余功能

        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy

        rclpy.init(args=[])
        node = Node("galaxis_studio_bridge")
        default_qos = QoSProfile(depth=10)
        # PICO 发布者使用 BEST_EFFORT,必须匹配才能收到 VR 数据
        pico_qos = QoSProfile(
            depth=10, reliability=ReliabilityPolicy.BEST_EFFORT
        )

        def import_msg_type(type_name: str) -> Any:
            # 形如 pkg/msg/Type:分别取包、子模块、类型名后导入
            pkg, sub, msg_cls = type_name.split("/")
            module = __import__(f"{pkg}.{sub}", fromlist=[msg_cls])
            return getattr(module, msg_cls)

        self._discover_cameras(node)
        from rclpy.qos import DurabilityPolicy
        from sensor_msgs.msg import CameraInfo
        from std_msgs.msg import UInt8
        from diagnostic_msgs.msg import DiagnosticArray
        for side in self._state['active_cameras']:
            node.create_subscription(CameraInfo, f'/{side}_camera/camera_info',
                lambda msg, role=side: self._on_camera_info(role, msg), pico_qos)
        node.create_subscription(UInt8, '/recording_gate/gate_level',
            lambda msg: self._on_sample('recording_gate_level', int(msg.data)),
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                       reliability=ReliabilityPolicy.RELIABLE))
        node.create_subscription(DiagnosticArray, '/diagnostics',
            self._on_camera_quality, default_qos)

        subscriptions = (
            ("topic_episodes", "std_msgs/msg/String", _parse_json_string, "episode_list"),
            ("topic_tasks", "std_msgs/msg/String", _parse_json_string, "task_list"),
            ("topic_recording", "std_msgs/msg/Bool", lambda m: bool(m.data), "recording"),
            (
                "topic_recording_completed",
                "std_msgs/msg/String",
                lambda m: m.data,
                "recording_completed",
            ),
            (
                "topic_recording_saved",
                "std_msgs/msg/String",
                lambda m: m.data,
                "recording_saved",
            ),
            ("topic_storage_used", "std_msgs/msg/Int64", lambda m: int(m.data), "storage_used"),
            ("topic_storage_free", "std_msgs/msg/Int64", lambda m: int(m.data), "storage_free"),
            ("topic_status", "std_msgs/msg/String", lambda m: m.data, "task_status"),
            ("topic_metadata", "std_msgs/msg/String", _parse_json_string, "metadata"),
            ("topic_recording_elapsed", "std_msgs/msg/Float64", lambda m: float(m.data), "recording_elapsed"),
            ("topic_episode_elapsed", "std_msgs/msg/Float64", lambda m: float(m.data), "episode_elapsed"),
            ("topic_subtask_elapsed", "std_msgs/msg/Float64", lambda m: float(m.data), "subtask_elapsed"),
            ("topic_collection_profile", "std_msgs/msg/String", lambda m: m.data, "collection_profile"),
            ("topic_encoder_left_state", "sensor_msgs/msg/JointState", _parse_joint_state, "encoder_left"),
            ("topic_encoder_right_state", "sensor_msgs/msg/JointState", _parse_joint_state, "encoder_right"),
            ("topic_encoder_left_id", "std_msgs/msg/String", lambda m: m.data, "encoder_left_id"),
            ("topic_encoder_right_id", "std_msgs/msg/String", lambda m: m.data, "encoder_right_id"),
            ("topic_hmd_state", "galaxis_pico_msgs/msg/PicoHmd", _parse_pico, "vr_hmd"),
            ("topic_left_state", "galaxis_pico_msgs/msg/PicoController", _parse_pico, "vr_left"),
            ("topic_right_state", "galaxis_pico_msgs/msg/PicoController", _parse_pico, "vr_right"),
        )

        for attr, type_name, parser, key in subscriptions:
            topic = getattr(self._settings, attr)
            qos = pico_qos if key in ("vr_hmd", "vr_left", "vr_right") else default_qos
            try:
                msg_type = import_msg_type(type_name)
                node.create_subscription(
                    msg_type,
                    topic,
                    lambda m, k=key, p=parser: self._on_ros_message(k, p, m),
                    qos,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("跳过话题 %s (%s): %s", topic, type_name, exc)

        # Callbacks only copy small telemetry messages. They share the default
        # mutually exclusive group, so multiple workers add scheduling/GIL
        # contention without parallelizing work on the ARM desktop.
        executor = SingleThreadedExecutor()
        executor.add_node(node)
        logger.info(
            "ROS 采集器已启动:相机 %s · 编码器 %s · 话题 %d 个",
            self._state["active_cameras"],
            self._state["active_arms"],
            len(subscriptions),
        )
        while not self._stop_event.is_set():
            try:
                executor.spin_once(timeout_sec=0.2)
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        node.destroy_node()

    def _discover_cameras(self, node: Any) -> None:
        """Keep selected cameras even before DDS discovers their publishers.

        Discovery is asynchronous. Filtering once at startup used to remove a
        slower center relay permanently, including its CameraInfo subscription;
        the preview then returned 404 even after images began arriving. The
        saved profile, not a transient graph snapshot, defines enabled cameras.
        """
        try:
            names = {name for name, _ in node.get_topic_names_and_types()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("话题探测失败,使用 profile 相机列表: %s", exc)
            return
        pending = [
            key for key in self._state["active_cameras"]
            if self._settings.camera_topics.get(key) not in names
        ]
        if pending:
            logger.info("等待已选相机话题出现: %s", pending)

    def _on_camera_info(self, role, msg):
        with self._lock:
            self._state['camera_info'][role] = dict(width=int(msg.width), height=int(msg.height))

    def _on_camera_quality(self, msg):
        gate_entries = [s for s in msg.status if s.name.startswith('recording_gate/')]
        if gate_entries:
            issues = []
            for status in gate_entries:
                level = status.level
                if isinstance(level, (bytes, bytearray, memoryview)):
                    level = int.from_bytes(level, 'little')
                if not level or status.name.startswith(('recording_gate/camera_quality/',
                                                        'recording_gate/pico_timing/')):
                    continue
                name = status.name.rsplit('/', 1)[-1]
                label = name
                if 'gripper_joint_state' in name:
                    label = '左磁编码器' if name.startswith('left_') else '右磁编码器'
                elif '_source_samples' in name:
                    label = {'hmd':'头显','left':'左手柄','right':'右手柄'}.get(name.split('_')[0],name)
                issue = f'{label}：{status.message}'
                if issue not in issues:
                    issues.append(issue)
            with self._lock:
                self._state['recording_gate_issues'] = issues
        for status in msg.status:
            if status.name == 'pico_bridge':
                with self._lock:
                    self._state['pico_bridge'] = {
                        **{v.key: v.value for v in status.values},
                        'message': status.message,
                    }
                    self._pico_diagnostic_at = time.monotonic()
        prefixes = ('recording_gate/camera_quality/', 'recording_gate/pico_timing/')
        entries = [s for s in msg.status if s.name.startswith(prefixes)]
        if entries:
            issues = []
            for status in entries:
                # DiagnosticStatus.level is a ROS byte, which can be b'\x00'.
                # bool(b'\x00') is True: it must not turn healthy checks into alerts.
                level = status.level
                if isinstance(level, (bytes, bytearray, memoryview)):
                    level = int.from_bytes(level, 'little')
                if not level:
                    continue
                prefix = next(p for p in prefixes if status.name.startswith(p))
                condition = status.name[len(prefix):]
                role = condition.split('_', 1)[0]
                label = {'left': '左腕相机', 'center': '中心相机', 'right': '右腕相机'}.get(role, '相机')
                if prefix.endswith('/pico_timing/'):
                    label = {'left': '左手柄', 'right': '右手柄'}.get(role, 'PICO')
                reason = status.message.strip()
                if reason.lower() == 'ok':
                    continue
                issue = f'{label}：{reason or "采集检查未通过"}'
                if issue not in issues:
                    issues.append(issue)
            with self._lock:
                self._state['camera_quality_issues'] = issues

    def _on_sample(self, key: str, value: Any) -> None:
        now = time.time()
        with self._lock:
            self._state["ros_connected"] = True
            if key in self._rates:
                self._rates[key].tick(now)
            if key == "episode_list":
                self._ingest_episodes(value)
            elif key == "task_list":
                self._ingest_tasks(value)
            elif key == "task_status":
                self._state[key] = value
                task_name = _task_from_status(value)
                # 回合结束后状态机会短暂回退为 No Task;保持当前命令指定的任务名
                if task_name and task_name.lower() != "no task":
                    self._state["task_name"] = task_name
            elif key in ("recording_completed", "recording_saved"):
                self._on_completed(value)
            elif key == "collection_profile":
                self._on_collection_profile(value)
            elif key in ("vr_hmd", "vr_left", "vr_right"):
                if self._state[key] is None:
                    # 首帧到达时间用于诊断 VR 链路建立慢的问题
                    logger.info(
                        "首次收到 VR 数据 %s(桥接启动后 %.1fs)",
                        key, time.time() - self._start_ts,
                    )
                self._state[key] = self._hold_vr_pose(key, value)
            else:
                self._state[key] = value

    def _on_ros_message(self, key: str, parser: Callable[[Any], Any], message: Any) -> None:
        """Count every source sample while limiting UI value conversion to 30 Hz."""
        if key not in self._rates:
            self._on_sample(key, parser(message))
            return

        now = time.time()
        with self._lock:
            self._state["ros_connected"] = True
            self._rates[key].tick(now)
            previous = self._last_monitor_update.get(key, 0.0)
            if now - previous < (1.0 / 30.0):
                return
            self._last_monitor_update[key] = now

        # Parse poses and joint arrays outside the lock so REST/WS snapshots do
        # not wait behind float/list conversion.  The source-rate sample above
        # still counts every ROS message received.
        value = parser(message)
        with self._lock:
            if key in ("vr_hmd", "vr_left", "vr_right"):
                if self._state[key] is None:
                    logger.info(
                        "首次收到 VR 数据 %s(桥接启动后 %.1fs)",
                        key,
                        time.time() - self._start_ts,
                    )
                self._state[key] = self._hold_vr_pose(key, value)
            else:
                self._state[key] = value

    def _hold_vr_pose(self, key: str, new: Any) -> Any:
        """位姿缺失或异常归零时保留最后有效位姿。

        PICO 端的 tracked/valid 标志可能比位姿更早变为 false，甚至在持续
        输出正常非零位姿时仍为 false。这两个标志只用于页面显示追踪状态，
        不应阻止左右手柄的新位姿更新。
        """
        if not isinstance(new, dict):
            return new
        prev = self._state.get(key)
        if not isinstance(prev, dict):
            return new
        position = new.get("position") or []
        lost = not position or all(abs(float(v)) < 1e-9 for v in position)
        if lost and (prev.get("position") or prev.get("rotation")):
            kept = dict(new)
            kept["position"] = prev.get("position") or kept.get("position")
            kept["rotation"] = prev.get("rotation") or kept.get("rotation")
            return kept
        return new

    # ------------------------------------------------------------------ ingest

    def _ingest_episodes(self, episodes: Any) -> None:
        if not isinstance(episodes, list):
            episodes = []
        self._state["episode_list"] = episodes
        # 回合计数只由录制完成事件累计,不能用 episode 列表长度(启动时后端
        # 会预创建一个尚未开始的 episode,直接计数会平白多出 1 个回合)
        if not self._state["task_name"] and episodes:
            for ep in episodes:
                task = ep.get("task") or {}
                name = task.get("name") or ep.get("task_name")
                if name:
                    self._state["task_name"] = str(name)
                    break

    def _ingest_tasks(self, tasks: Any) -> None:
        if not isinstance(tasks, list):
            tasks = []
        self._state["task_list"] = tasks
        if not self._state["task_name"] and tasks:
            first = tasks[0] or {}
            name = first.get("name") or first.get("task_name") or first.get("id")
            if name:
                self._state["task_name"] = str(name)

    def _on_collection_profile(self, value: str) -> None:
        self._state["collection_profile"] = value or "dual"
        arms = {
            "dual": ["left", "right"],
            "left": ["left"],
            "single-left": ["left"],
            "right": ["right"],
            "single-right": ["right"],
        }.get(str(value).strip().lower(), self._profile["active_arms"])
        self._state["active_arms"] = arms

    def _on_completed(self, payload: str) -> None:
        recording_id = str(payload or "").strip()
        if recording_id and recording_id in self._counted_recordings:
            return
        if recording_id:
            self._counted_recordings.add(recording_id)
        self._state["episodes_completed_total"] += 1
        self._state["last_completed_at"] = time.time()
        self._state["last_completed_uuid"] = recording_id
        self.log_event("ok", f"Episode 完成,累计 {self._state['episodes_completed_total']}")

    # ------------------------------------------------------------------ summary

    @staticmethod
    def _task_summary(state: dict[str, Any]) -> dict[str, Any]:
        name = state.get("task_name") or ""
        if not name:
            tasks = state.get("task_list") or []
            if tasks:
                first = tasks[0] or {}
                name = str(first.get("name") or first.get("task_name") or first.get("id") or "")
        return {"name": name, "kind": name or "未确认"}

    @staticmethod
    def _episode_summary(state: dict[str, Any]) -> dict[str, Any]:
        episodes = state.get("episode_list") or []
        current = None
        if episodes:
            active_status = {"in_progress", "recording", "started", "collecting", "ready"}
            for ep in episodes:
                if str(ep.get("status", "")).lower() in active_status:
                    current = ep
                    break
            if current is None:
                for ep in episodes:
                    subtasks = ep.get("subtasks") or []
                    if any(str(st.get("status", "")).lower() in active_status for st in subtasks):
                        current = ep
                        break
            if current is None:
                current = episodes[-1]
        completed = state.get("episodes_completed_total", 0)
        task_name = state.get("task_name") or "任务"
        current_name = ""
        if current:
            current_name = str(current.get("name") or current.get("id") or "")
        # 回合命名规则:<任务名> #<3 位序号>,与数据按任务分目录存储的约定一致
        # 序号从 1 开始:未开始时显示即将录制的 001,完成 n 个后显示 n+1
        next_round = max(completed + 1, 1)
        label = f"{task_name} #{next_round:03d}" if current else "—"
        return {
            "current": current,
            "current_name": current_name,
            "label": label,
            "task_name": task_name,
            "list_total": len(episodes),
            "completed_total": completed,
            "elapsed_sec": state.get("recording_elapsed", state.get("episode_elapsed", 0.0)),
            "subtask_elapsed_sec": state.get("subtask_elapsed", 0.0),
        }

    def _encoder_summary(self, state: dict[str, Any]) -> dict[str, Any]:
        arms = state.get("active_arms") or []
        result = {}
        for side in ("left", "right"):
            data = state.get(f"encoder_{side}")
            rate_hz = self._rates[f"encoder_{side}"].rate()
            # 根据实际情况调节:profile 启用该侧,或已实际收到该侧编码器数据
            # (传感器最多两个,插上哪一个就显示哪一个)
            active = side in arms or data is not None
            if not active:
                result[side] = {"active": False, "rate_hz": 0.0}
                continue
            if data is None:
                result[side] = {"active": True, "online": False, "rate_hz": rate_hz}
                continue
            result[side] = {
                "active": True,
                "online": rate_hz > 0,
                "device_id": state.get(f"encoder_{side}_id", ""),
                "name": data.get("name", ""),
                "position": data.get("position", 0.0),  # single-finger radians
                "opening_angle": 2.0 * data.get("position", 0.0),
                "opening_angle_units": "radians",
                "velocity": data.get("velocity", 0.0),
                "rate_hz": rate_hz,
            }
        return result

    def _vr_summary(self, state: dict[str, Any]) -> dict[str, Any]:
        def device(data: dict | None, rate_key: str) -> dict[str, Any]:
            if data is None:
                return {"online": False, "rate_hz": 0.0}
            rate_hz = self._rates[rate_key].rate()
            return {
                # A previous pose must not make an interrupted PICO stream look
                # live forever (or prematurely unblock collection controls).
                "online": rate_hz > 0,
                "pico_id": data.get("pico_id", ""),
                "seq": data.get("seq", 0),
                "tracked": bool(data.get("tracked", True)),
                "valid": bool(data.get("valid", True)),
                "position": data.get("position", [0.0, 0.0, 0.0]),
                "rotation": data.get("rotation", [0.0, 0.0, 0.0, 1.0]),
                "rate_hz": rate_hz,
            }

        return {
            "hmd": device(state.get("vr_hmd"), "vr_hmd"),
            "left": device(state.get("vr_left"), "vr_left"),
            "right": device(state.get("vr_right"), "vr_right"),
        }

    # ------------------------------------------------------------------ mock

    def _run_mock(self) -> None:
        """模拟数据源:一个正在进行的采集会话,便于开发与演示页面。"""
        import math

        base = time.time()
        tick = 0
        task_name = self._state["task_name"] or "演示任务"
        with self._lock:
            self._state["ros_connected"] = True
            self._state["episodes_completed_total"] = 12
            self._state["recording"] = True
            self._state["task_name"] = task_name
            self._state["collection_profile"] = "dual"
            self._state["episode_list"] = [
                {"id": f"ep-{i:03d}", "name": f"{task_name}-{i:03d}", "status": "completed"}
                for i in range(12)
            ] + [
                {
                    "id": "ep-013",
                    "name": f"{task_name}-013",
                    "status": "in_progress",
                    "subtasks": [{"id": "st-1", "status": "in_progress"}],
                }
            ]
            self.log_event("ok", f"模拟模式启动:任务「{task_name}」正在采集 ep-013")
        while not self._stop_event.is_set():
            time.sleep(0.25)
            tick += 1
            t = tick * 0.25
            with self._lock:
                self._state["recording"] = True
                self._state["episode_elapsed"] = round(t, 1)
                self._state["subtask_elapsed"] = round(t, 1)
                self._state["storage_used"] = int(48.2 * 1024 ** 3 + tick * 2e7)
                self._state["storage_free"] = int(951.8 * 1024 ** 3 - tick * 2e7)
                seq = int(t * 30)
                self._state["vr_hmd"] = _mock_pico("galaxis-pico4-ultra", seq, base)
                self._state["vr_left"] = _mock_pico("L", seq, base)
                self._state["vr_right"] = _mock_pico("R", seq, base)
                for _ in range(8):  # 每个模拟周期注入 8 次心跳(≈30 Hz)
                    for key in ("vr_hmd", "vr_left", "vr_right"):
                        self._rates[key].tick()
                # 编码器模拟:约 100 Hz 的正弦角度
                for side, sid in (("left", "L001"), ("right", "R002")):
                    angle = 0.35 + 0.25 * math.sin(t * 2.0 + (0.0 if side == "left" else math.pi / 2))
                    self._state[f"encoder_{side}"] = {
                        "name": f"{side}_joint",
                        "position": round(angle, 4),
                        "velocity": round(0.5 * math.cos(t * 2.0), 4),
                    }
                    self._state[f"encoder_{side}_id"] = sid
                    for _ in range(25):  # 每个模拟周期注入 25 次心跳(≈100 Hz)
                        self._rates[f"encoder_{side}"].tick()
            if tick == 120:
                with self._lock:
                    self._state["episodes_completed_total"] = 13
                    self.log_event("ok", "Episode 完成,累计 13")
            if tick == 260:
                with self._lock:
                    self._state["episodes_completed_total"] = 14
                    self.log_event("ok", "Episode 完成,累计 14")


def _task_from_status(status: str) -> str:
    """从 /task_sequence_manager/status 中提取任务名。

    状态格式示例:
        received_task:Galaxis
        completed_task:Galaxis. ...
    """
    if not status:
        return ""
    for prefix in ("received_task:", "completed_task:"):
        match = re.search(rf"{prefix}\s*([^\n.]+)", status)
        if match:
            return match.group(1).strip()
    return ""


def _mock_pico(pid: str, seq: int, base: float) -> dict[str, Any]:
    import math

    t = seq / 30.0
    return {
        "pico_id": pid,
        "seq": seq,
        "device_time_ns": int(base * 1e9),
        "pc_monotonic_ns": int(base * 1e9),
        "position": [
            round(0.3 * math.sin(t), 4),
            round(0.2 * math.cos(t), 4),
            round(0.9 + 0.05 * math.sin(t / 2), 4),
        ],
        "rotation": [0.0, 0.0, round(math.sin(t / 4), 4), round(math.cos(t / 4), 4)],
        "tracked": True,
        "valid": True,
    }
