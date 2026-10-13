#!/usr/bin/env python3
import json
import time
from datetime import datetime, timezone
from enum import Enum

import rclpy
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy

from std_msgs.msg import String, Bool, Float64, UInt8
from std_srvs.srv import Trigger
from airoa_data_msgs.srv import StringTrigger
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

from galaxis_core.recording_readiness import START_SERVICE_TIMEOUT_S
from galaxis_core.backend_client import create_backend
from galaxis_core.recording_gate import EscalationLevel


class TaskSequenceState(Enum):
    COMFIRM_TASK = 0
    WAIT_SUBTASK = 1
    RECORD_SUBTASK = 2
    COMPLETE_TASK = 3
    REWIND_SUBTASK = 4


class ActionCommand(Enum):
    ACCEPT = "accept"
    NEXT_ROUND = "next_round"
    FINISH_SESSION = "finish_session"
    REJECT = "reject"
    CANCEL = "cancel"
    REWIND = "rewind"
    FAIL_ADVANCE = "fail_advance"


class TaskSequenceManager(Node):
    """State-machine node that orchestrates data-collection episodes.

    Threading model
    ---------------
    The node runs on a ``MultiThreadedExecutor``.  All service servers,
    subscriptions, and the timer use the **default** callback group
    (``MutuallyExclusiveCallbackGroup``), so they are serialised — only one
    executes at a time.  Service *clients* use a ``ReentrantCallbackGroup``
    so their response futures can be resolved on a separate executor thread
    while a handler thread sleeps in ``wait_for_trigger_future_done``.

    This means shared state (``self.status``, ``self.tasks``, etc.) is never
    accessed concurrently and does not require additional locking.
    """

    RECOVERY_TIMEOUT_SEC = 10.0

    def __init__(self):
        super().__init__("task_sequence_manager")

        # Service-client callback group — reentrant so futures resolve while
        # a handler thread blocks in wait_for_trigger_future_done().
        self.reent_cg = ReentrantCallbackGroup()

        self.__setup_parameters()
        self.__setup_variables()
        self.__setup_publishers()
        self.__setup_subscribers()
        self.__setup_servers()
        self.__setup_clients()

        # TODO: create_timer uses the node clock — under use_sim_time=True
        # the timer won't fire until /clock is published.  Consider switching
        # to a threading.Thread loop for sim-time robustness.
        self.create_timer(1, self.process_step)

    def __setup_parameters(self):
        # Declare parameters
        self.declare_parameter("base_url", "http://localhost:8000/api")
        self.declare_parameter("api_key", "")
        self.declare_parameter("offline_mode", False)
        self.declare_parameter("task_file", "")
        self.declare_parameter("auto_repeat_episode", True)
        self.declare_parameter("continuous_round_mode", False)
        self.declare_parameter("use_recording_gate", False)
        self.declare_parameter("runner_organization", "FIXME")
        self.declare_parameter("runner_name", "")

        # Retrieve parameters
        gp = self.get_parameter
        self.base_url = gp("base_url").value
        api_key = gp("api_key").value
        self.offline_mode = bool(gp("offline_mode").value)
        task_file = gp("task_file").value
        self.auto_repeat_episode = gp("auto_repeat_episode").value
        self.continuous_round_mode = bool(gp("continuous_round_mode").value)
        self._use_gate = gp("use_recording_gate").value
        self.runner_organization = gp("runner_organization").value
        offline_mode = self.offline_mode

        self.backend = create_backend(
            offline_mode=offline_mode, task_file=task_file,
            base_url=self.base_url, api_key=api_key,
        )

        # Resolve FIXME placeholder from backend
        if self.runner_organization == "FIXME":
            if offline_mode:
                self.runner_organization = "offline"
                self.get_logger().info("Using 'offline' for runner_organization (offline mode)")
            else:
                try:
                    data = self.backend.get_robot_self()
                    resolved = (data or {}).get("organization_name")
                    self.runner_organization = str(resolved) if resolved else "unknown"
                    self.get_logger().info(f"Resolved runner_organization: {self.runner_organization}")
                except Exception as e:
                    self.runner_organization = "unknown"
                    self.get_logger().warn(f"Failed to fetch runner_organization from backend: {e}")

    def __setup_variables(self):
        self.is_recording = None
        self.start_time = None
        self.end_time = None
        self.task_start_time = None
        self.status = TaskSequenceState.COMFIRM_TASK
        self.has_subtasks_successed = None
        self.cur_subtask_index = 0
        self.prev_subtask_index = None
        self.tasks = {}
        self.new_tasks = {}
        self.meta_data = {"labels": [], "segments": []}
        self._segment_count = 0
        # 会话内回合序号偏移:录制 ID 的回合数 = 任务 round_index + 偏移,每次开始录制 +1
        self._round_offset = 0
        self.rosparam_txt = ""  # TODO: global param dump not directly available in ROS2. Need workaround.
        self.subtask_execution_id = None  # Track current execution for status reporting
        self.current_episode_subtask_id = None  # episode_sub_task natural ID for API paths
        self._completed_episode_ids: set[str] = set()  # Episodes finished/cancelled this session
        # Gate disabled → always allow (level 0); enabled → fail-closed (level 2)
        self._gate_level: int = 2 if self._use_gate else 0
        self._recovery_done = False
        self._recovery_start_time = None  # time.monotonic() timestamp
        self._clock_warned = False

    def _now_sec(self) -> float:
        """Return current time in seconds, falling back to wall clock if sim time is 0."""
        try:
            t = self.get_clock().now().nanoseconds / 1e9
            if t > 0:
                return t
        except Exception:
            pass
        # use_sim_time=True but /clock not available — fall back
        if not self._clock_warned:
            self.get_logger().warn(
                "Sim clock returned 0 — falling back to wall clock for timestamps"
            )
            self._clock_warned = True
        return time.time()

    @property
    def cur_subtask(self) -> dict | None:
        if (
            self.tasks
            and "subtasks" in self.tasks
            and 0 <= self.cur_subtask_index < len(self.tasks["subtasks"])
        ):
            return self.tasks["subtasks"][self.cur_subtask_index]
        return None

    def __setup_publishers(self):
        # QoS for latched-like publisher
        latched_qos = QoSProfile(depth=1)
        latched_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        latched_qos.reliability = ReliabilityPolicy.RELIABLE

        # Publishers
        # Retain the last state for command-line monitoring started after the
        # state transition.  Without this, a new `ros2 topic echo --once`
        # misses the event and incorrectly reports that collection is silent.
        status_qos = QoSProfile(depth=1)
        status_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        status_qos.reliability = ReliabilityPolicy.RELIABLE
        self.execution_status_pub = self.create_publisher(
            String, "~/status", status_qos
        )
        self.next_task_pub = self.create_publisher(String, "~/next_task", 10)
        self._diag_pub = self.create_publisher(DiagnosticArray, "/diagnostics", 10)
        self._block_reason_pub = self.create_publisher(String, "~/recording_block_reason", 10)
        self._subtask_elapsed_pub = self.create_publisher(Float64, "~/subtask_elapsed_sec", 10)
        self._episode_elapsed_pub = self.create_publisher(Float64, "~/episode_elapsed_sec", 10)

    def __setup_subscribers(self):
        # Subscriptions
        self.create_subscription(String, "/task_receiver/tasks", self.tasks_callback, 10)
        self.create_subscription(Bool, "/record_manager/recording", self.rosbag_status_callback, 10)
        self.create_subscription(String, "/metadata_handler/metadata_json", self.metadata_json_callback, 10)

        # Recording gate (opt-in, latched)
        if self._use_gate:
            gate_qos = QoSProfile(depth=1)
            gate_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
            gate_qos.reliability = ReliabilityPolicy.RELIABLE
            self.create_subscription(
                UInt8, "/recording_gate/gate_level",
                self._gate_level_callback, gate_qos,
            )
            self.get_logger().info(
                "Recording gate enabled (fail-closed until gate topic arrives)"
            )
        else:
            self.get_logger().info(
                "Recording gate disabled — all recordings allowed"
            )

    def __setup_clients(self):
        # Service clients
        self.start_recording_client = self.create_client(
            StringTrigger, "/record_manager/start_recording", callback_group=self.reent_cg
        )
        self.stop_recording_client = self.create_client(
            Trigger, "/record_manager/stop_recording", callback_group=self.reent_cg
        )
        self.cancel_recording_client = self.create_client(
            Trigger, "/record_manager/cancel_recording", callback_group=self.reent_cg
        )
        self.delete_recording_client = self.create_client(
            Trigger, "/record_manager/delete_recording", callback_group=self.reent_cg
        )

        self.set_runner_client = self.create_client(
            StringTrigger, "/metadata_handler/set_runner", callback_group=self.reent_cg
        )
        self.add_label_client = self.create_client(
            StringTrigger, "/metadata_handler/add_label", callback_group=self.reent_cg
        )
        self.set_episode_uuid_client = self.create_client(
            StringTrigger, "/metadata_handler/set_episode_uuid", callback_group=self.reent_cg
        )
        self.set_episode_client = self.create_client(
            StringTrigger, "/metadata_handler/set_episode", callback_group=self.reent_cg
        )
        self.add_segment_client = self.create_client(
            StringTrigger, "/metadata_handler/add_segment", callback_group=self.reent_cg
        )
        self.remove_last_segment_client = self.create_client(
            Trigger, "/metadata_handler/remove_last_segment", callback_group=self.reent_cg
        )
        self.override_last_segment_success_client = self.create_client(
            StringTrigger, "/metadata_handler/override_last_segment_success", callback_group=self.reent_cg
        )

        # Skip blocking wait — services will be checked on first use.
        # (wait_for_service in __init__ blocks the node before the executor
        #  starts and can prevent DDS discovery from completing on respawns)
        self.get_logger().info("Service clients created (will wait on first use)")

    def __setup_servers(self):
        # Services (server)
        self.create_service(Trigger, "/data_collection/accept", self.accept)
        self.create_service(Trigger, "/data_collection/reject", self.reject)
        self.create_service(Trigger, "/data_collection/cancel_episode", self.cancel_episode)
        self.create_service(StringTrigger, "/data_collection/cancel_episode_with_reason", self.cancel_episode_with_reason)
        self.create_service(Trigger, "/data_collection/rewind", self.rewind_episode)
        self.create_service(Trigger, "/data_collection/fail_advance", self.fail_advance)
        self.create_service(Trigger, "/data_collection/repeat", self.repeat_episode_srv)
        self.create_service(Trigger, "/data_collection/next_round", self.next_round)
        self.create_service(Trigger, "/data_collection/finish_session", self.finish_session)
        # Primitive services
        self.create_service(Trigger, "/data_collection/list_subtasks", self.list_subtasks_srv)
        self.create_service(StringTrigger, "/data_collection/start_recording", self.start_recording_srv)
        self.create_service(StringTrigger, "/data_collection/stop_recording", self.stop_recording_srv)
        self.create_service(StringTrigger, "/data_collection/start_subtask", self.start_subtask_srv)
        self.create_service(StringTrigger, "/data_collection/stop_subtask", self.stop_subtask_srv)

    def accept(self, request, response):
        self._start_error = ""
        response.success = self.transit_state(ActionCommand.ACCEPT)
        response.message = (
            self._start_error if not response.success and self._start_error
            else f"current state: {self.status.name}"
        )
        self.process_step()
        return response

    def reject(self, request, response):
        response.success = self.transit_state(ActionCommand.REJECT)
        response.message = f"current state: {self.status.name}"
        self.process_step()
        return response

    def cancel_episode(self, request, response):
        reason = "manual cancellation"
        self.get_logger().warning(f"Cancel episode requested: {reason}")
        self._publish_recording_block(reason)
        response.success = self.transit_state(ActionCommand.CANCEL, reason=reason)
        response.message = f"current state: {self.status.name}"
        self.process_step()
        return response

    def cancel_episode_with_reason(self, request, response):
        reason = request.message or "manual cancellation"
        self.get_logger().warning(f"Cancel episode requested: {reason}")
        self._publish_recording_block(reason)
        response.success = self.transit_state(ActionCommand.CANCEL, reason=reason)
        response.message = f"current state: {self.status.name}"
        self.process_step()
        return response

    def rewind_episode(self, request, response):
        response.success = self.transit_state(ActionCommand.REWIND)
        response.message = f"current state: {self.status.name}"
        self.process_step()
        return response

    def next_round(self, request, response):
        """Save the current round and pause until the next ACCEPT."""
        self._last_service_error = ""
        response.success = self.transit_state(ActionCommand.NEXT_ROUND)
        response.message = (self._last_service_error if not response.success and self._last_service_error
                            else f"current state: {self.status.name}")
        self.process_step()
        return response

    def finish_session(self, request, response):
        """Save the current round without automatically starting another one."""
        self._last_service_error = ""
        response.success = self.transit_state(ActionCommand.FINISH_SESSION)
        response.message = (self._last_service_error if not response.success and self._last_service_error
                            else f"current state: {self.status.name}")
        self.process_step()
        return response

    def fail_advance(self, request, response):
        response.success = self.transit_state(ActionCommand.FAIL_ADVANCE)
        response.message = f"current state: {self.status.name}"
        self.process_step()
        return response

    def repeat_episode_srv(self, request, response):
        """Explicitly repeat the last episode. Only valid in COMFIRM_TASK."""
        if self.status != TaskSequenceState.COMFIRM_TASK:
            response.success = False
            response.message = f"repeat only valid in COMFIRM_TASK, current: {self.status.name}"
            return response
        # TODO: allow repeat even when an episode is queued (e.g. it was
        #       cancelled or removed on the backend side).
        if self.new_tasks.get("episodeId"):
            response.success = False
            response.message = f"episode already queued: {self.new_tasks['episodeId']}"
            return response
        from galaxis_core.task_receiver import TaskReceiver

        repeated = self.backend.repeat_last_episode()
        if not repeated:
            response.success = False
            response.message = "No episode to repeat"
            return response
        raw = self.backend.get_episode(repeated["id"])
        if not raw:
            response.success = False
            response.message = f"Failed to fetch repeated episode {repeated['id']}"
            return response
        self.new_tasks = TaskReceiver.enrich_episode(raw, active_operator=self._fetch_active_operator())
        response.success = True
        response.message = f"Repeated episode: {repeated['id']}"
        self.get_logger().info(f"Episode repeated: {repeated['id']}")
        self.process_step()
        return response

    # --- Primitive service handlers ---
    def list_subtasks_srv(self, request, response):
        response.success, response.message = self._do_list_subtasks()
        return response

    def start_recording_srv(self, request, response):
        episode_id = request.message.strip() if request.message else ""
        response.success, response.message = self._do_start_recording(episode_id)
        self.process_step()
        return response

    def stop_recording_srv(self, request, response):
        save = request.message.strip().lower() == "true"
        response.success, response.message = self._do_stop_recording(save)
        self.process_step()
        return response

    def start_subtask_srv(self, request, response):
        response.success, response.message = self._do_start_subtask(request.message.strip())
        self.process_step()
        return response

    def stop_subtask_srv(self, request, response):
        success = request.message.strip().lower() == "true"
        response.success, response.message = self._do_stop_subtask(success)
        self.process_step()
        return response

    # --- Subscriptions ---
    def tasks_callback(self, msg: String):
        if msg.data == "null":
            self.new_tasks = {}
            return

        # check json format
        try:
            json_data = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().warning(f"Failed to decode task JSON: {e}")
            return

        # Empty message = no active episode (normal idle state)
        if not json_data:
            self.new_tasks = {}
            return

        task_required_keys = set(
            {"episodeId", "taskId", "assignedRobotId", "createdUserId", "status", "task", "subtasks"}
        )
        if not task_required_keys.issubset(json_data.keys()):
            missing_keys = task_required_keys - json_data.keys()
            self.get_logger().warning(f"Received task message is missing required keys: {missing_keys}")
            return

        task_info_required_keys = set({"id", "name", "description"})
        if not task_info_required_keys.issubset(json_data["task"].keys()):
            missing_keys = task_info_required_keys - json_data["task"].keys()
            self.get_logger().warning(f"Received task info is missing required keys: {missing_keys}")
            return

        # Skip episodes we already finished/cancelled this session
        ep_id = json_data.get("episodeId")
        if ep_id and ep_id in self._completed_episode_ids:
            return

        self.backend.register_episode(json_data)

        self.new_tasks = json_data

    def rosbag_status_callback(self, msg: Bool):
        self.is_recording = msg.data

    def _gate_level_callback(self, msg: UInt8):
        prev = self._gate_level
        self._gate_level = msg.data
        if prev < EscalationLevel.HARD_STOP and msg.data >= EscalationLevel.HARD_STOP:
            reason = "Recording gate hard-stop (level 2)"
            self.get_logger().warning(f"{reason} — cancelling episode")
            self._publish_recording_block(reason)
            if self.status in (
                TaskSequenceState.RECORD_SUBTASK,
                TaskSequenceState.WAIT_SUBTASK,
                TaskSequenceState.COMPLETE_TASK,
            ):
                self.transit_state(ActionCommand.CANCEL, reason=reason)
        elif prev == EscalationLevel.OK and msg.data >= EscalationLevel.BLOCK_START:
            reason = f"Recording gate blocked (level {msg.data})"
            self.get_logger().warning(f"{reason} — new starts disallowed")
            self._publish_recording_block(reason, level=DiagnosticStatus.WARN)
        elif prev >= EscalationLevel.BLOCK_START and msg.data == EscalationLevel.OK:
            self.get_logger().info("Recording gate opened — subtask starts allowed")

    def _publish_recording_block(self, reason: str, level: int = DiagnosticStatus.ERROR):
        """Publish diagnostic + reason topic when recording is blocked or stopped."""
        status = DiagnosticStatus()
        status.level = level
        status.name = "task_sequence_manager: Recording Block"
        status.message = reason
        status.hardware_id = ""
        status.values = [
            KeyValue(key="state", value=self.status.name),
            KeyValue(key="gate_level", value=str(self._gate_level)),
        ]
        if self.tasks and self.tasks.get("episodeId"):
            status.values.append(
                KeyValue(key="episode_id", value=self.tasks["episodeId"])
            )

        msg = DiagnosticArray()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.status = [status]
        self._diag_pub.publish(msg)

        self._block_reason_pub.publish(String(data=reason))

    def metadata_json_callback(self, msg: String):
        try:
            self.meta_data = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().error(f"Failed to decode metadata JSON: {e}")

    # --- service helpers (blocking) ---
    def wait_for_trigger_future_done(
        self,
        future: rclpy.Future,
        timeout: float = 5.0,
        sleep_time: float = 0.01,
        error_situation: str = "service call failed",
    ) -> bool:
        # wait for trigger/string trigger future done with timeout
        self._last_service_error = ""
        elapsed = 0.0
        while not future.done() and rclpy.ok() and elapsed < timeout:
            time.sleep(sleep_time)
            elapsed += sleep_time

        if not future.done():
            self._last_service_error = f"{error_situation}: timed out after {timeout}s"
            self.get_logger().error(self._last_service_error)
            return False

        if future.result() is None or not future.result().success:
            message = future.result().message if future.result() is not None else "unknown error"
            self._last_service_error = f"{error_situation}: {message}"
            self.get_logger().error(self._last_service_error)
            return False

        return True

    def start_recording(self) -> tuple[bool, str]:
        self.tasks = self.new_tasks  # Sync tasks
        if not self.tasks or "task" not in self.tasks:
            return False, "no valid tasks available"

        # 录制目录:<任务名>/<任务名>_<回合序号>_<时间戳>。
        # 回合序号由任务 round_index(采集入口按磁盘已有回合累加)+ 会话内偏移组成,
        # 无论重启/重复采集多少次都不会覆盖或清理历史回合。
        task_name = str(self.tasks.get("task", {}).get("name") or "task")
        task_name = task_name.replace("/", "_")
        base_round = int(self.tasks.get("task", {}).get("round_index") or 0)
        # 回合编号从 1 开始:任务未携带 round_index 时首回合为 001
        round_seq = (base_round if base_round > 0 else 1) + self._round_offset
        self._round_offset += 1
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        recording_id = f"{task_name}/{task_name}_{round_seq:03d}_{timestamp}"
        req = StringTrigger.Request()
        req.message = recording_id
        future = self.start_recording_client.call_async(req)

        if not self.wait_for_trigger_future_done(future, timeout=START_SERVICE_TIMEOUT_S, error_situation="Start recording service call failed"):
            if future.done() and future.result() is not None:
                return False, future.result().message or "record_manager service call failed"
            return False, "record_manager service call failed"
        return True, ""

    def stop_recording(self) -> bool:
        req = Trigger.Request()
        future = self.stop_recording_client.call_async(req)
        # Stopping flushes MCAP and verifies metadata; slow disks can take
        # longer than the generic five-second control-service deadline.
        # HD finalization has a duration-based deadline up to one hour. Keep
        # this service deadline outside it so failures return their real reason.
        return self.wait_for_trigger_future_done(future, timeout=30.0, error_situation="Stop recording service call failed")

    def cancel_recording(self) -> bool:
        req = Trigger.Request()
        future = self.cancel_recording_client.call_async(req)
        return self.wait_for_trigger_future_done(future, error_situation="Cancel recording service call failed")

    def delete_recording(self) -> bool:
        req = Trigger.Request()
        future = self.delete_recording_client.call_async(req)
        return self.wait_for_trigger_future_done(future, error_situation="Delete recording service call failed")

    def _fetch_active_operator(self) -> dict | None:
        """Best-effort fetch of the live teleop operator via ``GET /robot/me``.

        Returned to ``enrich_episode`` so its ``user_id`` can populate
        ``recordedBy`` — backend stamps ``episode.recorded_by`` only at
        ``/start`` time, so the episode payload itself is too late.
        """
        try:
            data = self.backend.get_robot_self()
        except Exception as e:
            self.get_logger().warn(f"Failed to fetch active_operator: {e}")
            return None
        return (data or {}).get("active_operator")

    def _set_episode_uuid(self, episode_id: str) -> bool:
        req = StringTrigger.Request()
        req.message = episode_id
        future = self.set_episode_uuid_client.call_async(req)
        return self.wait_for_trigger_future_done(future, error_situation="Set episode UUID service call failed")

    def record_runner(self) -> bool:
        name = (
            self.tasks.get("recordedBy")
            or str(self.get_parameter("runner_name").value)
            or self.tasks.get("createdUserId", "unknown")
        )
        runner = {
            "type": "operator",
            "organization": self.runner_organization,
            "name": name,
        }
        req = StringTrigger.Request()
        req.message = json.dumps(runner)
        future = self.set_runner_client.call_async(req)
        return self.wait_for_trigger_future_done(future, error_situation="Set runner service call failed")

    def record_label_and_segment(
        self, label: str, start_time: float, end_time: float, success: bool
    ) -> bool:
        send_success = True

        # save label (plain string)
        label_idx = len(self.meta_data["labels"])

        req = StringTrigger.Request()
        req.message = label
        future = self.add_label_client.call_async(req)
        send_success = send_success and self.wait_for_trigger_future_done(
            future, error_situation="Add label service call failed"
        )

        # save segment
        segment_dict = {
            "start_time": start_time,
            "end_time": end_time,
            "label_idx": label_idx,
            "success": success,
        }

        req = StringTrigger.Request()
        req.message = json.dumps(segment_dict)
        future = self.add_segment_client.call_async(req)
        send_success = send_success and self.wait_for_trigger_future_done(
            future, error_situation="Add segment service call failed"
        )

        if send_success:
            self._segment_count += 1

        return send_success

    # --- Primitive operations ---
    def _do_list_subtasks(self) -> tuple[bool, str]:
        if self.status == TaskSequenceState.COMFIRM_TASK:
            return False, "Cannot list subtasks: no episode in progress (state=CONFIRM_TASK)"
        if not self.tasks or "subtasks" not in self.tasks:
            return False, "Cannot list subtasks: no task data available"

        result = []
        for i, st in enumerate(self.tasks["subtasks"]):
            entry = {
                "id": st["id"],
                "name": st.get("name", ""),
                "orderIndex": st.get("orderIndex", i),
                "success": self.has_subtasks_successed[i] if self.has_subtasks_successed else None,
                "is_current": i == self.cur_subtask_index,
            }
            result.append(entry)
        return True, json.dumps(result)

    def _do_start_recording(self, episode_id: str = "") -> tuple[bool, str]:
        if self.status != TaskSequenceState.COMFIRM_TASK:
            return False, f"Cannot start recording: invalid state {self.status.name} (expected CONFIRM_TASK)"
        if self._gate_level >= EscalationLevel.BLOCK_START:
            reason = f"Cannot start recording: recording gate closed (level {self._gate_level})"
            self._publish_recording_block(reason)
            return False, reason

        if episode_id:
            # Fetch specific episode by ID and enrich it
            from galaxis_core.task_receiver import TaskReceiver

            raw = self.backend.get_episode(episode_id)
            if not raw:
                return False, f"Cannot start recording: episode '{episode_id}' not found"
            self.new_tasks = TaskReceiver.enrich_episode(raw, active_operator=self._fetch_active_operator())

        if not self.new_tasks:
            # No active episode from task_receiver — pick the last available
            from galaxis_core.task_receiver import TaskReceiver

            episodes = self.backend.list_episodes()
            # Exclude Episodes already completed by this manager before
            # deciding whether repeat-last is required. Otherwise the backend
            # can return only the just-finished Episode and continuous mode
            # would stop after its first round.
            episodes = [
                e for e in episodes
                if e.get("id") not in self._completed_episode_ids
            ]
            if not episodes and self.auto_repeat_episode:
                repeated = self.backend.repeat_last_episode()
                if repeated:
                    episodes = [repeated]
            if not episodes:
                return False, "Cannot start recording: no episodes available"
            raw = self.backend.get_episode(episodes[-1]["id"])
            if not raw:
                return False, "Cannot start recording: failed to fetch first available episode"
            self.new_tasks = TaskReceiver.enrich_episode(raw, active_operator=self._fetch_active_operator())

        if not self.new_tasks.get("subtasks"):
            return False, "Cannot start recording: episode has no subtasks"

        ok, reason = self.start_recording()
        if not ok:
            return False, f"Cannot start recording: {reason}"
        self.record_runner()
        self._set_episode_uuid(self.tasks["episodeId"])
        self.backend.start_episode(self.tasks["episodeId"])
        # Initialize variables for subtasks
        self.has_subtasks_successed = [None for _ in self.tasks["subtasks"]]
        self.start_time = None
        self.task_start_time = None
        self.prev_subtask_index = None
        self.cur_subtask_index = 0
        self.subtask_execution_id = None
        self.current_episode_subtask_id = None
        self.status = TaskSequenceState.WAIT_SUBTASK
        return True, "Recording started, transitioned to WAIT_SUBTASK"

    def _do_start_round(self) -> tuple[bool, str]:
        """Start an Episode and, in continuous mode, its first segment."""
        ok, message = self._do_start_recording()
        if not ok or not self.continuous_round_mode:
            return ok, message
        if self.cur_subtask is None:
            return False, "Cannot start round: episode has no internal round label"
        return self._do_start_subtask(self.cur_subtask["id"])

    def _do_next_round(self) -> tuple[bool, str]:
        """Finish and save the current round, then pause before the next one."""
        if not self.continuous_round_mode:
            return False, "Next round is only available in continuous round mode"
        if self.status != TaskSequenceState.RECORD_SUBTASK:
            return False, f"Cannot advance round: invalid state {self.status.name}"

        ok, message = self._do_stop_subtask(True)
        if not ok:
            return False, message

        # Continuous mode intentionally treats the first internal label as the
        # entire Episode, regardless of how many legacy subtasks the task has.
        self.status = TaskSequenceState.COMPLETE_TASK
        ok, message = self._do_stop_recording(save=True)
        if not ok:
            return False, message

        # Stay in CONFIRM_TASK after saving. The next ACCEPT/A press invokes
        # _do_start_round(), which uses repeat-last when no Episode is queued.
        return True, "Round saved; paused until the next ACCEPT"

    def _do_finish_session(self) -> tuple[bool, str]:
        """Finish and save the active continuous round, then stay idle."""
        if not self.continuous_round_mode:
            return False, "Finish session is only available in continuous round mode"
        if self.status != TaskSequenceState.RECORD_SUBTASK:
            return False, f"Cannot finish session: invalid state {self.status.name}"

        ok, message = self._do_stop_subtask(True)
        if not ok:
            return False, message
        self.status = TaskSequenceState.COMPLETE_TASK
        return self._do_stop_recording(save=True)

    def _do_start_subtask(self, subtask_id: str) -> tuple[bool, str]:
        if self.status != TaskSequenceState.WAIT_SUBTASK:
            return False, f"Cannot start subtask: invalid state {self.status.name} (expected WAIT_SUBTASK)"
        if not self.tasks or "subtasks" not in self.tasks:
            return False, "Cannot start subtask: no task data available"

        # Find subtask by ID
        found_index = None
        for i, st in enumerate(self.tasks["subtasks"]):
            if st["id"] == subtask_id:
                found_index = i
                break
        if found_index is None:
            return False, f"Cannot start subtask: subtask ID '{subtask_id}' not found"

        self.cur_subtask_index = found_index
        self.start_time = self._now_sec()
        if self.task_start_time is None:
            self.task_start_time = self.start_time
        # Create execution + start via backend
        self.current_episode_subtask_id = self.cur_subtask["id"]
        self.subtask_execution_id = self.backend.create_execution(
            self.tasks["episodeId"], self.current_episode_subtask_id,
        )
        if self.subtask_execution_id:
            self.backend.start_execution(
                self.tasks["episodeId"],
                self.current_episode_subtask_id,
                self.subtask_execution_id,
            )
        else:
            self.get_logger().warning(
                f"Failed to create execution for subtask '{self.current_episode_subtask_id}' "
                f"— backend will not track this attempt"
            )
        self.status = TaskSequenceState.RECORD_SUBTASK
        return True, f"Started subtask '{self.cur_subtask.get('name', '')}', transitioned to RECORD_SUBTASK"

    def _do_stop_subtask(self, success: bool) -> tuple[bool, str]:
        if self.status != TaskSequenceState.RECORD_SUBTASK:
            return False, f"Cannot stop subtask: invalid state {self.status.name} (expected RECORD_SUBTASK)"

        self.end_time = self._now_sec()
        self.record_label_and_segment(
            self.cur_subtask["name"],
            self.start_time,
            self.end_time,
            success=success,
        )
        # Finish execution via backend
        if self.subtask_execution_id and self.current_episode_subtask_id:
            self.backend.finish_execution(
                self.tasks["episodeId"],
                self.current_episode_subtask_id,
                self.subtask_execution_id,
            )
            if success:
                self.backend.complete_subtask(
                    self.tasks["episodeId"],
                    self.current_episode_subtask_id,
                )
        self.has_subtasks_successed[self.cur_subtask_index] = success
        self.prev_subtask_index = self.cur_subtask_index
        # Check if all subtasks resolved
        if all(s is not None for s in self.has_subtasks_successed):
            self.status = TaskSequenceState.COMPLETE_TASK
        else:
            self.status = TaskSequenceState.WAIT_SUBTASK
        label = "success" if success else "failed"
        return True, f"Stopped subtask ({label}), transitioned to {self.status.name}"

    def _do_stop_recording(self, save: bool, reason: str = "") -> tuple[bool, str]:
        if self.status not in (TaskSequenceState.WAIT_SUBTASK, TaskSequenceState.COMPLETE_TASK):
            return False, f"Cannot stop recording: invalid state {self.status.name} (expected WAIT_SUBTASK or COMPLETE_TASK)"

        if save:
            if self._segment_count == 0:
                return False, "Cannot save recording: no segments recorded"
            success_all = all(s for s in self.has_subtasks_successed if s is not None)

            # set episode
            episode = {
                "start_time": self.task_start_time,
                "end_time": self.end_time,
                "success": success_all,
                "label": self.tasks["task"]["name"],
            }
            req = StringTrigger.Request()
            req.message = json.dumps(episode)
            future = self.set_episode_client.call_async(req)
            if not self.wait_for_trigger_future_done(future, error_situation="Set episode service call failed"):
                detail = getattr(self, "_last_service_error", "") or "Set episode metadata failed"
                self._publish_recording_block(detail)
                return False, detail
            if not self.stop_recording():
                detail = getattr(self, "_last_service_error", "") or "Stop recording service call failed"
                self._publish_recording_block(detail)
                return False, detail
            # Do not report a completed episode until the recorder confirms
            # that the bag and metadata have actually been saved.
            self.backend.finish_episode(self.tasks["episodeId"])
        else:
            self.get_logger().warning(f"Cancelling episode: {reason}")
            self.backend.cancel_episode(self.tasks["episodeId"], reason=reason)
            self.cancel_recording()

        # Mark episode as done so it won't be picked again
        episode_id = self.tasks.get("episodeId")
        if episode_id:
            self._completed_episode_ids.add(episode_id)
        self.tasks = {}
        self.new_tasks = {}
        self.status = TaskSequenceState.COMFIRM_TASK
        label = "saved" if save else "discarded"
        return True, f"Recording {label}, transitioned to CONFIRM_TASK"

    def transit_state(self, action: ActionCommand, reason: str = "") -> bool:
        if self.status == TaskSequenceState.COMFIRM_TASK:
            if action == ActionCommand.ACCEPT:
                ok, msg = self._do_start_round()
                self._start_error = "" if ok else msg
                if not ok:
                    self.get_logger().warning(msg)
                return ok
            else:
                return False
        elif self.status == TaskSequenceState.WAIT_SUBTASK:
            if self.cur_subtask is None:
                self.get_logger().warning("cur_subtask is None in WAIT_SUBTASK")
                return False
            if action == ActionCommand.ACCEPT:
                ok, msg = self._do_start_subtask(self.cur_subtask["id"])
                if not ok:
                    self.get_logger().warning(msg)
                return ok
            elif action == ActionCommand.REJECT:
                # Skip subtask via backend (no primitive equivalent)
                result = self.backend.skip_subtask(self.tasks["episodeId"], self.cur_subtask["id"])
                if result is None:
                    self.get_logger().warning(
                        f"Failed to skip subtask '{self.cur_subtask['id']}' via backend"
                    )
                self.has_subtasks_successed[self.cur_subtask_index] = False
                self.prev_subtask_index = self.cur_subtask_index
                self.cur_subtask_index += 1
                if self.cur_subtask_index == len(self.tasks["subtasks"]):
                    self.status = TaskSequenceState.COMPLETE_TASK
                else:
                    self.status = TaskSequenceState.WAIT_SUBTASK
                return True
            elif action == ActionCommand.CANCEL:
                ok, msg = self._do_stop_recording(save=False, reason=reason)
                if not ok:
                    self.get_logger().warning(msg)
                return ok
            elif (
                action == ActionCommand.REWIND
                and self.prev_subtask_index is not None
                and self._segment_count > 0
            ):
                self.status = TaskSequenceState.REWIND_SUBTASK
                return True
        elif self.status == TaskSequenceState.RECORD_SUBTASK:
            if action == ActionCommand.NEXT_ROUND:
                ok, msg = self._do_next_round()
                if not ok:
                    self.get_logger().warning(msg)
                return ok
            elif action == ActionCommand.FINISH_SESSION:
                ok, msg = self._do_finish_session()
                if not ok:
                    self.get_logger().warning(msg)
                return ok
            elif action == ActionCommand.ACCEPT:
                ok, msg = self._do_stop_subtask(True)
                if not ok:
                    self.get_logger().warning(msg)
                    return False
                # Auto-advance: move to next sequential subtask
                if self.prev_subtask_index is None:
                    self.get_logger().error("Bug: prev_subtask_index is None during auto-advance")
                    self.cur_subtask_index += 1  # fallback: advance from current position
                else:
                    self.cur_subtask_index = self.prev_subtask_index + 1
                if self.cur_subtask_index >= len(self.tasks["subtasks"]):
                    self.status = TaskSequenceState.COMPLETE_TASK
                    ok2, msg2 = self._do_stop_recording(save=True)
                    if not ok2:
                        self.get_logger().warning(msg2)
                    return ok2
                else:
                    self.status = TaskSequenceState.WAIT_SUBTASK
                    ok2, msg2 = self._do_start_subtask(self.cur_subtask["id"])
                    if not ok2:
                        self.get_logger().warning(msg2)
                    return ok2
            elif action == ActionCommand.REJECT:
                ok, msg = self._do_stop_subtask(False)
                if not ok:
                    self.get_logger().warning(msg)
                    return False
                # Stay on same subtask for retry
                self.cur_subtask_index = self.prev_subtask_index
                if self.cur_subtask is None:
                    self.get_logger().warning("cur_subtask is None after reject")
                    return False
                self.status = TaskSequenceState.WAIT_SUBTASK
                ok2, msg2 = self._do_start_subtask(self.cur_subtask["id"])
                if not ok2:
                    self.get_logger().warning(msg2)
                return ok2
            elif action == ActionCommand.FAIL_ADVANCE:
                # Stop current subtask as failed, advance to next (don't retry)
                ok, msg = self._do_stop_subtask(False)
                if not ok:
                    self.get_logger().warning(msg)
                    return False
                # Auto-advance: move to next sequential subtask
                if self.prev_subtask_index is None:
                    self.get_logger().error("Bug: prev_subtask_index is None during fail_advance")
                    self.cur_subtask_index += 1
                else:
                    self.cur_subtask_index = self.prev_subtask_index + 1
                if self.cur_subtask_index >= len(self.tasks["subtasks"]):
                    self.status = TaskSequenceState.COMPLETE_TASK
                    return True
                else:
                    self.status = TaskSequenceState.WAIT_SUBTASK
                    ok2, msg2 = self._do_start_subtask(self.cur_subtask["id"])
                    if not ok2:
                        self.get_logger().warning(msg2)
                    return ok2
            elif action == ActionCommand.CANCEL:
                # Stop current subtask, cancel episode, stop and discard recording
                ok, msg = self._do_stop_subtask(False)
                if not ok:
                    self.get_logger().warning(msg)
                    return False
                self.status = TaskSequenceState.COMPLETE_TASK
                ok, msg = self._do_stop_recording(save=False, reason=reason)
                if not ok:
                    self.get_logger().warning(msg)
                return ok
            else:
                return False
        elif self.status == TaskSequenceState.COMPLETE_TASK:
            if action == ActionCommand.ACCEPT:
                ok, msg = self._do_stop_recording(save=True)
                if not ok:
                    self.get_logger().warning(msg)
                return ok
            elif action == ActionCommand.REJECT:
                ok, msg = self._do_stop_recording(save=False, reason="rejected by operator")
                if not ok:
                    self.get_logger().warning(msg)
                return ok
            elif action == ActionCommand.CANCEL:
                ok, msg = self._do_stop_recording(save=False, reason=reason)
                if not ok:
                    self.get_logger().warning(msg)
                return ok
            elif (
                action == ActionCommand.REWIND
                and self.prev_subtask_index is not None
                and self._segment_count > 0
            ):
                self.status = TaskSequenceState.REWIND_SUBTASK
                return True
            else:
                return False
        elif self.status == TaskSequenceState.REWIND_SUBTASK:
            if action == ActionCommand.ACCEPT:
                future = self.override_last_segment_success_client.call_async(StringTrigger.Request(message="True"))
                suc = self.wait_for_trigger_future_done(
                    future, error_situation="Override last segment success service call failed"
                )
                if suc:
                    self.has_subtasks_successed[self.prev_subtask_index] = True
                    self.cur_subtask_index = self.prev_subtask_index + 1
                    if self.cur_subtask_index == len(self.tasks["subtasks"]):
                        self.status = TaskSequenceState.COMPLETE_TASK
                    else:
                        self.status = TaskSequenceState.WAIT_SUBTASK
                    return True
                else:
                    return False

            elif action == ActionCommand.REJECT:
                future = self.override_last_segment_success_client.call_async(StringTrigger.Request(message="False"))
                suc = self.wait_for_trigger_future_done(
                    future, error_situation="Override last segment success service call failed"
                )
                if suc:
                    self.has_subtasks_successed[self.prev_subtask_index] = False
                    self.cur_subtask_index = self.prev_subtask_index
                    self.status = TaskSequenceState.WAIT_SUBTASK
                    return True
                else:
                    return False

            elif action == ActionCommand.REWIND:
                future = self.remove_last_segment_client.call_async(Trigger.Request())
                suc = self.wait_for_trigger_future_done(future)

                if suc:
                    self._segment_count -= 1
                    self.has_subtasks_successed[self.prev_subtask_index] = None
                    self.cur_subtask_index = self.prev_subtask_index
                    self.prev_subtask_index = None
                    self.status = TaskSequenceState.WAIT_SUBTASK
                    return True
                else:
                    return False
        else:
            raise NotImplementedError("Unhandled TaskSequenceState in try_transit_state")

        return False

    # --- Startup recovery ---------------------------------------------------

    def _attempt_startup_recovery(self) -> bool:
        """Check for in-progress episodes after a restart.

        Returns ``True`` when a decision has been made (or recovery is
        skipped) and ``False`` while still waiting for signals.
        """
        # Safety guard: user already triggered an action during the wait window
        if self.status != TaskSequenceState.COMFIRM_TASK:
            self.get_logger().info("Startup recovery skipped: state already advanced")
            return True

        # Start the timeout clock on first call
        if self._recovery_start_time is None:
            self._recovery_start_time = time.monotonic()

        elapsed = time.monotonic() - self._recovery_start_time
        timed_out = elapsed >= self.RECOVERY_TIMEOUT_SEC

        # Resolve is_recording (None means no message received yet)
        is_recording = self.is_recording
        if is_recording is None and not timed_out:
            return False  # still waiting for record_manager signal

        if is_recording is None and timed_out:
            self.get_logger().warning(
                "Startup recovery: timed out waiting for is_recording signal, assuming not recording"
            )
            is_recording = False

        # Resolve active episode from new_tasks
        has_active_episode = bool(self.new_tasks and self.new_tasks.get("episodeId"))

        if not has_active_episode and not timed_out:
            return False  # give task_receiver more time

        if not has_active_episode:
            # Normal startup — nothing to recover
            return True

        # A Ready Episode (status=0) is queued work, not an interrupted
        # recording. Keep it preloaded so the operator can start it with A.
        if not is_recording and self.new_tasks.get("status", 0) == 0:
            self.get_logger().info(
                f"Startup recovery: keeping Ready episode "
                f"'{self.new_tasks['episodeId']}' queued"
            )
            return True

        # Active episode exists — decide based on recording state
        if is_recording:
            self._do_resume_episode()
        else:
            self._do_cancel_stale_episode()
        return True

    def _do_resume_episode(self):
        """Resume an in-progress episode that was interrupted by a restart."""
        self.tasks = self.new_tasks

        # Parse subtask statuses: status != 0 → resolved (True), status == 0 → pending (None)
        self.has_subtasks_successed = [
            True if st.get("status", 0) != 0 else None
            for st in self.tasks["subtasks"]
        ]

        # Find first pending subtask
        first_pending = None
        for i, s in enumerate(self.has_subtasks_successed):
            if s is None:
                first_pending = i
                break

        if first_pending is not None:
            self.cur_subtask_index = first_pending
            self.status = TaskSequenceState.WAIT_SUBTASK
        else:
            self.cur_subtask_index = len(self.tasks["subtasks"]) - 1
            self.status = TaskSequenceState.COMPLETE_TASK

        self.task_start_time = self._now_sec()
        self.start_time = None
        self.end_time = None
        self.prev_subtask_index = None
        self.subtask_execution_id = None
        self.current_episode_subtask_id = None

        self.record_runner()

        self.get_logger().warning(
            f"Startup recovery: resumed episode '{self.tasks['episodeId']}' "
            f"at subtask index {self.cur_subtask_index} (state={self.status.name})"
        )

    def _do_cancel_stale_episode(self):
        """Cancel a stale in-progress episode that is no longer being recorded."""
        episode_id = self.new_tasks["episodeId"]
        self.backend.cancel_episode(
            episode_id, reason="stale episode (not recording at startup)",
        )
        self._completed_episode_ids.add(episode_id)
        self.get_logger().error(
            f"Startup recovery: cancelled stale episode '{episode_id}' "
            f"(not recording)"
        )
        self.new_tasks = {}

    _ps_count = 0

    def process_step(self):
        if not self._recovery_done:
            if self.offline_mode:
                self._recovery_done = True  # No stale episodes to recover in offline mode
            elif not self._attempt_startup_recovery():
                return  # Still waiting for signals
            else:
                self._recovery_done = True
                self.get_logger().info(f"Recovery done, state={self.status.name}")

        # Publish static/control data
        status_msg = String()
        if self.status == TaskSequenceState.COMFIRM_TASK:
            SH_task = self.new_tasks.get("task", {}).get("name", "No Task")
            status_msg.data = f"received_task:{SH_task}"
        elif self.status == TaskSequenceState.WAIT_SUBTASK:
            PA_task = self.cur_subtask.get("name", "No Subtask") if self.cur_subtask else "No Subtask"
            status_msg.data = f"next subtask:{PA_task}. \n wait for start or skip command."
        elif self.status == TaskSequenceState.RECORD_SUBTASK:
            PA_task = self.cur_subtask.get("name", "No Subtask") if self.cur_subtask else "No Subtask"
            status_msg.data = f"executing :{PA_task}"
        elif self.status == TaskSequenceState.COMPLETE_TASK:
            SH_task = self.tasks.get("task", {}).get("name", "No Task")
            status_msg.data = f"completed_task:{SH_task}. \n wait for save or discard command."
        elif self.status == TaskSequenceState.REWIND_SUBTASK:
            if self.prev_subtask_index is not None:
                last_PA_task = self.tasks["subtasks"][self.prev_subtask_index].get("name", "No Subtask")
            else:
                last_PA_task = "No Subtask"
            status_msg.data = f"rewind last subtask:{last_PA_task}?\n wait for accept, reject or rewind command."

        self.execution_status_pub.publish(status_msg)

        # Publish next subtask info
        next_task_msg = String()
        if self.status in (TaskSequenceState.WAIT_SUBTASK, TaskSequenceState.RECORD_SUBTASK):
            next_task_msg.data = json.dumps(self.cur_subtask) if self.cur_subtask else "{}"
        elif self.status == TaskSequenceState.COMFIRM_TASK:
            subtasks = self.new_tasks.get("subtasks", [])
            next_task_msg.data = json.dumps(subtasks[0]) if subtasks else "{}"
        else:
            next_task_msg.data = "{}"
        self.next_task_pub.publish(next_task_msg)

        # Publish elapsed-time for recording gate duration enforcement
        subtask_msg = Float64()
        if self.start_time is not None and self.status == TaskSequenceState.RECORD_SUBTASK:
            subtask_msg.data = self._now_sec() - self.start_time
        self._subtask_elapsed_pub.publish(subtask_msg)

        episode_msg = Float64()
        if self.task_start_time is not None and self.status != TaskSequenceState.COMFIRM_TASK:
            episode_msg.data = self._now_sec() - self.task_start_time
        self._episode_elapsed_pub.publish(episode_msg)


def main():
    from galaxis_core.sentry_setup import init_sentry
    init_sentry()
    rclpy.init()
    node = TaskSequenceManager()

    executor = MultiThreadedExecutor()
    try:
        rclpy.spin(node, executor=executor)
    except KeyboardInterrupt:
        print("Keyboard interrupt received, shutting down task sequence manager.")
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
