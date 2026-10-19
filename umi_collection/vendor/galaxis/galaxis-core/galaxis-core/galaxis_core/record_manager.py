#!/usr/bin/env python3
import json
import os
import shutil
import signal
import argparse
import subprocess
import sys
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import time
from typing import Optional, Tuple

import rclpy
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from rosgraph_msgs.msg import Clock
from std_msgs.msg import Bool, Float64, Int64, String
from std_srvs.srv import Trigger
from airoa_data_msgs.srv import StringTrigger

from galaxis_core.backend_client import create_backend
from galaxis_core.ownership import normalize_tree_access, resolve_record_owner
from galaxis_core.recording_readiness import (
    McapReadiness, required_recording_topics, wait_for_recording_ready,
)

STResponse = StringTrigger.Response
STRequest = StringTrigger.Request


class RecordManager(Node):
    def __init__(self):
        super().__init__("record_manager")
        self.rosbag_process: Optional[subprocess.Popen] = None
        self.is_recording = False
        self._recording_started_monotonic = None
        self._recording_elapsed_sec = 0.0
        self._recording_start_lock = threading.Lock()
        self.recent_record_dir: Optional[str] = None
        self.task_name: Optional[str] = None
        self.reent_cg = ReentrantCallbackGroup()
        self._hdf5_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hdf5-finalizer"
        )
        self._hdf5_jobs: set[Future] = set()
        self._hdf5_jobs_lock = threading.Lock()
        self._hdf5_process_lock = threading.Lock()
        self._hdf5_process: Optional[subprocess.Popen] = None
        self._hdf5_shutdown = threading.Event()

        self.__setup_parameters()
        self.__setup_publishers()
        self.__setup_clients()
        self.__setup_services()
        self._recover_hdf5_finalizations()

    def __setup_parameters(self):
        # fixed parameters
        self.declare_parameter("base_url", "http://localhost:8000/api")
        self.declare_parameter("api_key", "")
        self.declare_parameter("offline_mode", False)
        self.declare_parameter("task_file", "")
        self.declare_parameter("site", "")
        self.declare_parameter("location", "")
        self.declare_parameter("record_topics", ["/tf"])
        self.declare_parameter("record_base_dir", "/root/datasets/rosbags")
        self.declare_parameter("required_free_space", 100)  # in GB
        self.declare_parameter("rosbag_params", ["--storage", "mcap"])
        self.declare_parameter("record_format", "rosbag")
        self.declare_parameter("video_mp4_enabled", False)
        self.video_mp4_enabled = bool(self.get_parameter("video_mp4_enabled").value)
        self.declare_parameter("collection_gate_file", "")
        self.collection_gate_file = str(self.get_parameter("collection_gate_file").value)
        self.declare_parameter("qos_overrides_file", "")

        profile = self._fetch_robot_profile()
        self.robot_id = profile["id"]
        self.site = str(self.get_parameter("site").value)
        self.location = str(self.get_parameter("location").value)
        self.record_topics = list(self.get_parameter("record_topics").value)
        self.record_base_dir = str(self.get_parameter("record_base_dir").value)
        required_space_gb = int(self.get_parameter("required_free_space").value)
        self.required_free_space = required_space_gb * 1024 * 1024 * 1024  # Convert to bytes
        self.rosbag_params = list(self.get_parameter("rosbag_params").value)
        self.record_format = str(self.get_parameter("record_format").value)
        if self.record_format not in ("rosbag", "hdf5"):
            raise ValueError(
                f"Unsupported record_format {self.record_format!r}; "
                "expected 'rosbag' or 'hdf5'"
            )
        # use_sim_time is a built-in ROS 2 parameter (declared by Node base class)
        self.use_sim_time = self.get_parameter("use_sim_time").value
        self._sim_time_verified = False  # set True after /clock confirmed
        self.qos_overrides_file = str(self.get_parameter("qos_overrides_file").value)
        if self.qos_overrides_file:
            # A comments-only/empty overrides file parses to None and makes
            # `ros2 bag record --qos-profile-overrides-path` crash (seen on
            # jazzy: NoneType has no .items()). Only pass the flag when the
            # file actually contains entries; otherwise fall back to default QoS.
            overrides = None
            try:
                import yaml

                with open(self.qos_overrides_file) as f:
                    overrides = yaml.safe_load(f)
            except Exception as exc:
                self.get_logger().warn(
                    f"Could not read QoS overrides file {self.qos_overrides_file}: {exc}"
                )
            if isinstance(overrides, dict) and overrides:
                self.get_logger().info(f"QoS overrides file: {self.qos_overrides_file}")
            else:
                self.get_logger().info(
                    "QoS overrides file has no entries — using default QoS"
                )
                self.qos_overrides_file = ""

        # v2.0 metadata parameters — auto-fetched from backend when set to "FIXME"
        self.declare_parameter("robot_type", "FIXME")
        self.declare_parameter("environment_type", "real_world")
        self.declare_parameter("runner_organization", "FIXME")
        self.declare_parameter("devices", "[]")  # JSON string param

        # Resolve FIXME placeholders: backend → config → "unknown"
        robot_type = str(self.get_parameter("robot_type").value)
        if robot_type == "FIXME":
            resolved = profile.get("model") or "unknown"
            self.set_parameters([rclpy.Parameter("robot_type", value=resolved)])
            self.get_logger().info(f"Resolved robot_type: {resolved}")

        runner_org = str(self.get_parameter("runner_organization").value)
        if runner_org == "FIXME":
            resolved = profile.get("organization_name") or "unknown"
            self.set_parameters([rclpy.Parameter("runner_organization", value=resolved)])
            self.get_logger().info(f"Resolved runner_organization: {resolved}")

        # Override location from backend if not set in config
        if not self.location and profile.get("location_name"):
            self.location = profile["location_name"]
            self.get_logger().info(f"Resolved location from backend: {self.location}")

        # dynamic parameters for teleop interface metadata
        self.declare_parameter("teleop_interface", "unknown")
        self.declare_parameter("teleop_interface_url", "unknown")
        self.declare_parameter("teleop_interface_hash", "unknown")
        self.declare_parameter("teleop_interface_branch", "unknown")

        os.makedirs(self.record_base_dir, exist_ok=True)
        # The recorder itself runs as root for ROS/device access. Persisted
        # recordings belong to the operator who launched the collection CLI.
        # Fall back to the mount owner for direct/legacy launches that do not
        # provide the explicit identity.
        self.record_owner_uid, self.record_owner_gid = resolve_record_owner(
            self.record_base_dir
        )
        self.get_logger().info(
            "Completed recordings will belong to "
            f"UID {self.record_owner_uid}, GID {self.record_owner_gid}"
        )

        self.record_url = "https://github.com/airoa-org/galaxis_core"
        self.record_hash = os.getenv("GIT_HASH", "unknown")
        self.record_branch = os.getenv("GIT_BRANCH", "unknown")

    def _fetch_robot_profile(self) -> dict:
        """Fetch robot profile from /robot/me.

        Returns dict with ``id``, ``model``, and ``organization_id`` keys.
        Missing or unreachable values fall back to defaults.
        """
        base_url = str(self.get_parameter("base_url").value)
        api_key = str(self.get_parameter("api_key").value)
        offline_mode = bool(self.get_parameter("offline_mode").value)
        task_file = str(self.get_parameter("task_file").value)
        defaults = {"id": "unknown", "model": None, "organization_name": None, "location_name": None}
        if not api_key and not offline_mode:
            self.get_logger().warn("No api_key configured and offline_mode is off")
            return defaults
        try:
            client = create_backend(
                offline_mode=offline_mode, task_file=task_file,
                base_url=base_url, api_key=api_key,
            )
            data = client.get_robot_self()
            if data:
                profile = {
                    "id": str(data.get("id", "unknown")),
                    "model": data.get("model"),
                    "organization_name": data.get("organization_name"),
                    "location_name": data.get("location_name"),
                }
                self.get_logger().info(f"Fetched robot profile from API: {profile}")
                return profile
            self.get_logger().warn("API returned no robot data")
            return defaults
        except Exception as e:
            self.get_logger().warn(f"Failed to fetch robot profile: {e}")
            return defaults

    def _check_clock_available(self, timeout_sec: float = 2.0) -> bool:
        """Check if /clock topic is actively publishing messages."""
        event = threading.Event()

        def _on_clock(msg):
            event.set()

        sub = self.create_subscription(Clock, "/clock", _on_clock, 10)
        try:
            ok = event.wait(timeout=timeout_sec)
            if ok:
                self.get_logger().info("/clock is active — enabling --use-sim-time")
            else:
                self.get_logger().warn(
                    f"/clock not received within {timeout_sec}s"
                )
            return ok
        finally:
            self.destroy_subscription(sub)

    def __setup_publishers(self):
        latched_qos = QoSProfile(depth=1)
        latched_qos.reliability = ReliabilityPolicy.RELIABLE
        latched_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL

        self.rosbag_manager_mode_pub = self.create_publisher(Bool, "~/recording", latched_qos)
        self.recording_elapsed_pub = self.create_publisher(Float64, "~/recording_elapsed_sec", latched_qos)
        self.storage_free_space_pub = self.create_publisher(Int64, "~/free", 10)
        self.storage_used_space_pub = self.create_publisher(Int64, "~/used", 10)

        completed_qos = QoSProfile(depth=10)
        completed_qos.reliability = ReliabilityPolicy.RELIABLE
        completed_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.recording_completed_pub = self.create_publisher(
            String, "~/recording_completed", completed_qos
        )
        # A stopped/saved round is distinct from an upload-ready recording in
        # HDF5 mode: the lossless MCAP is already safe when the stop service
        # returns, while conversion and validation continue in the background.
        # The monitor counts this event; StorageNode deliberately continues to
        # consume only recording_completed.
        self.recording_saved_pub = self.create_publisher(
            String, "~/recording_saved", completed_qos
        )
        self.recording_cancelled_pub = self.create_publisher(
            String, "~/recording_cancelled", completed_qos
        )

        self.create_timer(1.0, self.publish_storage_statistics)
        self.create_timer(1.0, self.publish_recording_state)

    def __setup_clients(self):
        self.init_meta_client = self.create_client(
            Trigger, "/metadata_handler/initialize_metadata", callback_group=self.reent_cg
        )
        self.add_file_client = self.create_client(
            StringTrigger, "/metadata_handler/add_file", callback_group=self.reent_cg
        )
        self.set_robot_client = self.create_client(
            StringTrigger, "/metadata_handler/set_robot", callback_group=self.reent_cg
        )
        self.set_environment_client = self.create_client(
            StringTrigger, "/metadata_handler/set_environment", callback_group=self.reent_cg
        )
        self.extend_programs_client = self.create_client(
            StringTrigger, "/metadata_handler/extend_programs", callback_group=self.reent_cg
        )
        self.set_devices_client = self.create_client(
            StringTrigger, "/metadata_handler/set_devices", callback_group=self.reent_cg
        )
        self.get_verified_metadata_client = self.create_client(
            Trigger, "/metadata_handler/get_verified_metadata", callback_group=self.reent_cg
        )

        for c in [
            self.init_meta_client,
            self.add_file_client,
            self.set_robot_client,
            self.set_environment_client,
            self.extend_programs_client,
            self.set_devices_client,
            self.get_verified_metadata_client,
        ]:
            while not c.wait_for_service(timeout_sec=1.0):
                self.get_logger().info(f"Waiting for service {c.srv_name} to become available...")

    def __setup_services(self):
        self.create_service(StringTrigger, "~/start_recording", self.start_recording_srv, callback_group=self.reent_cg)
        self.create_service(Trigger, "~/stop_recording", self.stop_recording_srv, callback_group=self.reent_cg)
        self.create_service(Trigger, "~/cancel_recording", self.cancel_recording_srv, callback_group=self.reent_cg)
        self.create_service(Trigger, "~/delete_recording", self.delete_recording_srv)
        self.create_service(
            StringTrigger, "~/toggle_recording", self.toggle_recording_srv, callback_group=self.reent_cg
        )

    def publish_storage_statistics(self):
        total, used, free = shutil.disk_usage(self.record_base_dir)

        free_msg = Int64()
        free_msg.data = int(free)
        self.storage_free_space_pub.publish(free_msg)

        used_msg = Int64()
        used_msg.data = int(used)
        self.storage_used_space_pub.publish(used_msg)

    def publish_recording_state(self):
        msg = Bool()
        msg.data = self.is_recording
        self.rosbag_manager_mode_pub.publish(msg)
        if self._recording_started_monotonic is not None:
            self._recording_elapsed_sec = max(0.0, time.monotonic() - self._recording_started_monotonic)
            if not self.is_recording:
                self._recording_started_monotonic = None
        elapsed = Float64()
        elapsed.data = self._recording_elapsed_sec
        self.recording_elapsed_pub.publish(elapsed)

    def wait_for_future_done(self, future: rclpy.Future, timeout: float = 1.0, sleep_time: float = 0.01):
        elapsed = 0.0
        while not future.done() and rclpy.ok() and elapsed < timeout:
            time.sleep(sleep_time)
            elapsed += sleep_time

    def start_recording(self, message: Optional[str]) -> Tuple[bool, str]:
        if not self._recording_start_lock.acquire(blocking=False):
            return False, "录制器正在启动，请等待本次启动完成"
        try:
            if self.is_recording or self.rosbag_process is not None:
                return False, "Rosbag is already recording."
            if getattr(self, "video_mp4_enabled", False):
                from galaxis_core.recording_priority import CapturePriority
                self._capture_priority = CapturePriority(self.record_base_dir)
                try:
                    self._capture_priority.acquire()
                except RuntimeError as exc:
                    return False, str(exc)
            return self._start_recording(message)
        finally:
            if not self.is_recording and self.rosbag_process is None:
                self._release_capture_priority()
            self._recording_start_lock.release()

    def _release_capture_priority(self):
        priority = getattr(self, "_capture_priority", None)
        if priority is not None:
            priority.release()

    def _start_recording(self, message: Optional[str]) -> Tuple[bool, str]:
        # The desktop supervisor refreshes this lease; controller callbacks
        # never perform network requests or create a recording while offline.
        gate_file = getattr(self, "collection_gate_file", "")
        self.collection_author = None
        self.collection_destination = None
        if gate_file:
            try:
                with open(gate_file, encoding="utf-8") as handle:
                    gate = json.load(handle)
                if gate.get("available") is not True or not 0 <= time.time() - gate["checked_at"] < 10:
                    return False, ""
                self.collection_author = gate["username"]
                self.collection_destination = gate.get('upload_destination')
            except (OSError, ValueError, KeyError, TypeError):
                return False, ""
        task_name = message or datetime.now(timezone.utc).strftime("rosbag-%Y%m%d-%H%M%S")
        total, used, free = shutil.disk_usage(self.record_base_dir)
        self.get_logger().info(f"Free space: {free}, Required: {self.required_free_space}")

        if free < self.required_free_space:
            self.get_logger().warn("Not enough space to start recording.")
            return False, "Not enough space to start recording"

        if self.is_recording or self.rosbag_process is not None:
            self.get_logger().warn("Rosbag is already recording.")
            return False, "Rosbag is already recording."

        record_dir = os.path.join(self.record_base_dir, task_name)
        os.makedirs(self.record_base_dir, exist_ok=True)
        # 录制目录可能带任务大类子目录(如 <任务名>/<回合>),确保父目录存在
        os.makedirs(os.path.dirname(record_dir), exist_ok=True)
        # 任务大类目录同样归操作员所有(777),避免宿主侧出现 root 锁定目录
        try:
            parent_dir = os.path.dirname(record_dir)
            if parent_dir != self.record_base_dir:
                owner_uid, owner_gid = resolve_record_owner(self.record_base_dir)
                os.chown(parent_dir, owner_uid, owner_gid)
                os.chmod(parent_dir, 0o777)
        except OSError:
            pass

        payload_type, payload_filename = self._recording_payload(task_name)
        staging_dir = os.path.join(record_dir, ".rosbag_staging") if self.record_format == "hdf5" else record_dir
        try:
            probe = McapReadiness(
                staging_dir, required_recording_topics(self.record_topics), self.record_topics,
            )
        except ImportError as exc:
            return False, f"录制就绪检查依赖缺失，请更新采集镜像: {exc}"


        # initialize metadata
        init_metadata_req = Trigger.Request()
        init_metadata_future = self.init_meta_client.call_async(init_metadata_req)
        self.wait_for_future_done(init_metadata_future)

        if init_metadata_future.result() is None or not init_metadata_future.result().success:
            self.get_logger().error("Failed to initialize metadata.")
            return False, "Failed to initialize metadata."
        self.get_logger().info("Metadata initialized.")

        # add file entry
        add_file_req = StringTrigger.Request()
        add_file_req.message = json.dumps(
            {
                "type": payload_type,
                "name": payload_filename,
            }
        )
        add_file_future = self.add_file_client.call_async(add_file_req)
        self.wait_for_future_done(add_file_future)

        if add_file_future.result() is None or not add_file_future.result().success:
            self.get_logger().error("Failed to add file entry to metadata.")
            return False, "Failed to add file entry to metadata."

        self.get_logger().info("File entry added to metadata.")

        # set robot
        set_robot_req = StringTrigger.Request()
        robot_type = str(self.get_parameter("robot_type").value)
        set_robot_req.message = json.dumps({"type": robot_type, "id": self.robot_id})
        set_robot_future = self.set_robot_client.call_async(set_robot_req)
        self.wait_for_future_done(set_robot_future)

        if set_robot_future.result() is None or not set_robot_future.result().success:
            self.get_logger().error("Failed to set robot in metadata.")
            return False, "Failed to set robot in metadata."

        self.get_logger().info("Robot set in metadata.")

        # set environment
        set_environment_req = StringTrigger.Request()
        environment_type = str(self.get_parameter("environment_type").value)
        env = {"type": environment_type, "site": self.site}
        if self.location:
            env["location"] = self.location
        set_environment_req.message = json.dumps(env)
        set_environment_future = self.set_environment_client.call_async(set_environment_req)
        self.wait_for_future_done(set_environment_future)

        if set_environment_future.result() is None or not set_environment_future.result().success:
            self.get_logger().error("Failed to set environment in metadata.")
            return False, "Failed to set environment in metadata."

        self.get_logger().info("Environment set in metadata.")

        # extend programs
        extend_programs_req = StringTrigger.Request()
        extend_programs_req.message = json.dumps(
            [
                {
                    "role": "interface",
                    "name": self.get_parameter("teleop_interface").value,
                    "source": {
                        "git": {
                            "uri": self.get_parameter("teleop_interface_url").value,
                            "hash": self.get_parameter("teleop_interface_hash").value,
                            "branch": self.get_parameter("teleop_interface_branch").value,
                        }
                    },
                },
                {
                    "role": "data_collection",
                    "name": "record_manager",
                    "source": {
                        "git": {
                            "uri": self.record_url,
                            "hash": self.record_hash,
                            "branch": self.record_branch,
                        }
                    },
                },
            ]
        )
        extend_programs_future = self.extend_programs_client.call_async(extend_programs_req)
        self.wait_for_future_done(extend_programs_future)

        if extend_programs_future.result() is None or not extend_programs_future.result().success:
            self.get_logger().error("Failed to extend programs in metadata.")
            return False, "Failed to extend programs in metadata."

        self.get_logger().info("Programs extended in metadata.")

        # set devices
        set_devices_req = StringTrigger.Request()
        devices_json = str(self.get_parameter("devices").value)
        set_devices_req.message = devices_json
        set_devices_future = self.set_devices_client.call_async(set_devices_req)
        self.wait_for_future_done(set_devices_future)

        if set_devices_future.result() is None or not set_devices_future.result().success:
            self.get_logger().error("Failed to set devices in metadata.")
            return False, "Failed to set devices in metadata."

        self.get_logger().info("Devices set in metadata.")

        # Start the selected recording backend.
        from galaxis_core.camera_quality import load_contract
        try:
            camera_contract = load_contract()
        except Exception as exc:
            return False, f'无法读取相机采集配置：{exc}'
        # 降低录制进程的 CPU/IO 优先级,避免录制启动与持续写入挤占视频流和
        # ROS 实时数据,导致监看页面在录制开始时卡顿
        if self.record_format == "hdf5":
            # rosbag2 writes into a temporary child directory. The final HDF5
            # file is published only after lossless conversion and validation.
            os.makedirs(record_dir, exist_ok=False)
        command = []
        if shutil.which("nice") and shutil.which("ionice"):
            command += ["nice", "-n", "10", "ionice", "-c", "3"]
        command.extend(
            self._recording_command(record_dir, payload_filename)
        )

        self.get_logger().info(f"Recording command: {' '.join(command)}")

        try:
            self.rosbag_process = subprocess.Popen(command)
        except FileNotFoundError as exc:
            if self.record_format == "hdf5" and os.path.isdir(record_dir):
                shutil.rmtree(record_dir)
            self.get_logger().error(f"Failed to start recorder: {exc}")
            return False, "recording command not found"

        # Popen only proves the process was spawned, not that ROS data was recorded.
        # Episode/subtask start_time is assigned by the caller after this returns.
        try:
            wait_for_recording_ready(
                self.rosbag_process, probe,
                keep_running=lambda: rclpy.ok() and not self._hdf5_shutdown.is_set(),
            )
            if camera_contract:
                # Snapshot the exact configuration for this episode, so a
                # later restart/recovery cannot validate against another setup.
                Path(record_dir, 'camera-contract.json').write_text(
                    json.dumps(camera_contract, ensure_ascii=False), encoding='utf-8')
        except Exception as exc:
            self.get_logger().error(f"Recording did not become ready: {exc}")
            self._abort_recording_start()
            self.publish_recording_state()
            return False, str(exc)

        self._recording_started_monotonic = time.monotonic()
        self._recording_elapsed_sec = 0.0
        self.is_recording = True
        self.task_name = task_name
        
        self.recent_record_dir = record_dir

        self.publish_recording_state()
        self.get_logger().info("Rosbag recording started.")
        return True, "Success"

    def _abort_recording_start(self):
        """Reap the failed child, retaining any staging data for diagnosis."""
        process = self.rosbag_process
        if process is None:
            return
        for stop_signal, timeout in ((signal.SIGINT, 2), (signal.SIGTERM, 1), (signal.SIGKILL, 1)):
            if process.poll() is not None:
                break
            try:
                process.send_signal(stop_signal)
                process.wait(timeout=timeout)
            except ProcessLookupError:
                break
            except subprocess.TimeoutExpired:
                continue
        if process.poll() is None:
            self.get_logger().error("录制器未退出，保留进程引用并阻止再次启动")
        else:
            self.rosbag_process = None

    def _recording_payload(self, task_name: str) -> Tuple[str, str]:
        """Return metadata type and payload filename for the configured format."""
        basename = os.path.basename(task_name)
        if self.record_format == "hdf5":
            return "hdf5", basename + ".h5"

        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--storage", type=str)
        args, _unknown = parser.parse_known_args(self.rosbag_params)
        if args.storage != "mcap":
            raise ValueError("Only 'mcap' rosbag storage is supported.")
        return "mcap", basename + "_0.mcap"

    def _recording_command(self, record_dir: str, payload_filename: str) -> list[str]:
        """Build the backend command without process-launch side effects."""
        del payload_filename
        output_dir = record_dir
        if self.record_format == "hdf5":
            output_dir = os.path.join(record_dir, ".rosbag_staging")
        command = ["ros2", "bag", "record", "-o", output_dir]
        if self.use_sim_time:
            if not self._sim_time_verified:
                self._sim_time_verified = self._check_clock_available()
            if self._sim_time_verified:
                command.append("--use-sim-time")
            else:
                self.get_logger().warn(
                    "use_sim_time requested but /clock not available — using wall clock"
                )
        command.extend(self.rosbag_params)
        if self.qos_overrides_file:
            command.extend(["--qos-profile-overrides-path", self.qos_overrides_file])
        command.extend(self.record_topics)
        return command

    def _finalize_hdf5_recording(
        self, record_dir: str, task_name: str
    ) -> Tuple[bool, str]:
        """Convert or revalidate one crash-safe compact HDF5 bundle."""
        _payload_type, payload_filename = self._recording_payload(task_name)
        staging_dir = os.path.join(record_dir, ".rosbag_staging")
        output_file = os.path.join(record_dir, payload_filename)
        command = [
            sys.executable,
            "-m",
            "galaxis_core.hdf5_recorder",
            "--input",
            staging_dir,
            "--output",
            output_file,
            "--profile",
            "compact-video",
            "--signal-profile",
            "umi-pose-gripper-v1",
            "--video-crf",
            "14",
            "--video-fps",
            "30",
            "--video-preset",
            "medium",
        ]
        if os.path.isfile(output_file):
            command.append("--verify-existing")
        with self._hdf5_process_lock:
            if self._hdf5_shutdown.is_set():
                return False, "HDF5 conversion skipped during shutdown; MCAP preserved."
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            self._hdf5_process = process
        try:
            stdout, _stderr = process.communicate()
        finally:
            with self._hdf5_process_lock:
                if self._hdf5_process is process:
                    self._hdf5_process = None
        if process.returncode != 0 or not os.path.isfile(output_file):
            detail = (stdout or "").strip()
            self.get_logger().error(
                "HDF5 conversion/validation failed; preserving staging MCAP at "
                f"{staging_dir}: {detail}"
            )
            return False, (
                "HDF5 conversion failed; the complete staging MCAP was preserved at "
                f"{staging_dir}."
            )
        self.get_logger().info(
            f"HDF5 conversion validated: {(stdout or '').strip()}"
        )
        return True, "Success"

    def _complete_hdf5_recording(
        self, record_dir: str, task_name: str, meta_dict: Optional[dict]
    ) -> None:
        """Finalize one stopped round without blocking the ROS stop service."""
        try:
            converted, _message = self._finalize_hdf5_recording(
                record_dir, task_name
            )
        finally:
            # A cancelled/failed converter can leave a root-owned .partial.
            # Apply the same ownership rule as rosbag even on that path.
            if not self.normalize_recording_owner(record_dir):
                self.get_logger().warning(
                    "Could not return interrupted HDF5 files to the operator."
                )
        if not converted:
            return
        meta_path = os.path.join(record_dir, "meta.json")
        if meta_dict is not None and not os.path.isfile(meta_path):
            self.save_meta_file(meta_dict, record_dir=record_dir)
        if not os.path.isfile(meta_path):
            self.get_logger().error(
                "HDF5 payload was recovered and validated, but meta.json is "
                f"unavailable; preserving staging MCAP at {record_dir}"
            )
            return
        staging_dir = os.path.join(record_dir, ".rosbag_staging")
        if os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir)
        if not self.normalize_recording_owner(record_dir):
            self.get_logger().warning(
                "HDF5 recording saved, but ownership normalization failed."
            )
        completed_msg = String()
        completed_msg.data = record_dir
        self.recording_completed_pub.publish(completed_msg)

    def _recover_hdf5_finalizations(self) -> None:
        """Resume stopped HDF5 rounds left by Ctrl+C or a container restart."""
        if self.record_format != "hdf5" or not os.path.isdir(self.record_base_dir):
            return
        candidates: list[str] = []
        for root, _dirs, files in os.walk(self.record_base_dir):
            if os.path.basename(root) != ".rosbag_staging":
                continue
            if "metadata.yaml" not in files:
                continue
            candidates.append(os.path.dirname(root))
        queued = 0
        for record_dir in candidates:
            meta_path = os.path.join(record_dir, "meta.json")
            meta_dict = None
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, encoding="utf-8") as handle:
                        meta_dict = json.load(handle)
                except (OSError, json.JSONDecodeError) as exc:
                    self.get_logger().error(
                        f"Cannot read recovery metadata {meta_path}: {exc}"
                    )
            # Only the basename controls the final payload filename.
            task_name = os.path.basename(record_dir)
            output_file = os.path.join(record_dir, task_name + ".h5")
            if os.path.isfile(output_file) and meta_dict is None:
                # Legacy rounds created before metadata persistence may have a
                # fully converted payload but no recoverable airoa meta.json.
                # Revalidating on every startup only blocks new work; retain
                # staging as the authoritative fallback and leave both files.
                self.get_logger().warning(
                    "Recovered HDF5 exists but meta.json is unavailable; "
                    f"preserving staging without requeueing {record_dir}"
                )
                continue
            self._queue_hdf5_finalization(record_dir, task_name, meta_dict)
            queued += 1
        if queued:
            self.get_logger().warning(
                f"Resuming {queued} interrupted HDF5 finalization job(s)."
            )

    def _queue_hdf5_finalization(
        self, record_dir: str, task_name: str, meta_dict: Optional[dict]
    ) -> None:
        future = self._hdf5_executor.submit(
            self._complete_hdf5_recording,
            record_dir,
            task_name,
            meta_dict,
        )
        with self._hdf5_jobs_lock:
            self._hdf5_jobs.add(future)

        def finished(job: Future) -> None:
            with self._hdf5_jobs_lock:
                self._hdf5_jobs.discard(job)
            if job.cancelled():
                return
            try:
                job.result()
            except Exception as exc:  # noqa: BLE001
                self.get_logger().error(
                    f"Unexpected HDF5 finalization failure: {exc}"
                )

        future.add_done_callback(finished)

    def stop_recording(self) -> Tuple[bool, str]:
        if not self._recording_start_lock.acquire(blocking=False):
            return False, "录制器正在切换状态，请稍候"
        try:
            return self._stop_recording()
        finally:
            if not self.is_recording and self.rosbag_process is None:
                self._release_capture_priority()
            self._recording_start_lock.release()

    def _stop_recording(self) -> Tuple[bool, str]:
        if not self.is_recording or self.rosbag_process is None:
            self.get_logger().warn("Rosbag is not recording.")
            return False, "Rosbag is not recording."

        # check if metadata is valid
        get_verified_metadata_req = Trigger.Request()
        get_verified_metadata_future = self.get_verified_metadata_client.call_async(get_verified_metadata_req)
        self.wait_for_future_done(get_verified_metadata_future, timeout=5.0)

        # Now stop the selected recording backend and wait for a clean flush.
        self.rosbag_process.send_signal(signal.SIGINT)
        recorder_returncode = self.rosbag_process.wait()
        self.rosbag_process = None

        if isinstance(recorder_returncode, int) and recorder_returncode != 0:
            self.is_recording = False
            self.get_logger().error(
                f"Recorder exited with status {recorder_returncode}."
            )
            self._preserve_recording_failure(f"Recorder exited with status {recorder_returncode}")
            self.publish_recording_state()
            return False, f"Recorder exited with status {recorder_returncode}."

        # The MCAP writer is fully stopped and flushed. Publish pause now so
        # the controller can save this round and start the next one without
        # waiting for HDF5 compression and full-file verification.
        self.is_recording = False
        self.publish_recording_state()

        if get_verified_metadata_future.result() is None or not get_verified_metadata_future.result().success:
            result = get_verified_metadata_future.result()
            detail = result.message if result is not None else "metadata service timed out"
            message = f"Metadata verification failed: {detail}. Data preserved at {self.recent_record_dir}"
            self._preserve_recording_failure(message)
            return False, message

        meta_dict = json.loads(get_verified_metadata_future.result().message)
        if self.record_format == "hdf5":
            record_dir = self.recent_record_dir
            task_name = self.task_name
            if not record_dir or not task_name:
                return False, "Missing stopped recording path for HDF5 conversion."
            # Persist metadata before returning from the stop service. The
            # conversion queue is intentionally asynchronous and can be
            # interrupted by Ctrl+C/container shutdown, but exact round
            # metadata must never exist only in process memory.
            self.save_meta_file(meta_dict, record_dir=record_dir)
            if not self.normalize_recording_owner(record_dir):
                self.get_logger().warning(
                    "Stopped MCAP is safe, but operator ownership normalization failed."
                )
            saved_msg = String()
            saved_msg.data = record_dir
            self.recording_saved_pub.publish(saved_msg)
            self._queue_hdf5_finalization(record_dir, task_name, meta_dict)
            self.get_logger().info(
                f"MCAP stopped; queued background HDF5 finalization for {record_dir}"
            )
            return True, "Recording stopped; HDF5 finalization queued."

        if getattr(self, "video_mp4_enabled", False):
            try:
                self._queue_mp4_recording(Path(self.recent_record_dir), meta_dict)
            except Exception as exc:
                message = f"MP4 任务保存失败，原始数据已保留: {exc}"
                self._preserve_recording_failure(message)
                return False, message
        else:
            self.save_meta_file(meta_dict)
        if not self.normalize_recording_owner(self.recent_record_dir):
            self.get_logger().warning(
                "Recording saved, but could not apply operator ownership and mode 777; "
                "leaving its contents untouched."
            )

        saved_msg = String()
        saved_msg.data = self.recent_record_dir
        self.recording_saved_pub.publish(saved_msg)

        # Notify uploader of completed recording
        completed_msg = String()
        completed_msg.data = self.recent_record_dir
        if not getattr(self, "video_mp4_enabled", False):
            self.recording_completed_pub.publish(completed_msg)

        self.get_logger().info("Rosbag recording stopped; local data saved.")
        return True, "Success"

    def _queue_mp4_recording(self, record_dir: Path, meta: dict) -> None:
        from galaxis_core.mp4_worker import atomic_json
        # Durable metadata is captured per episode, before accepting the next
        # start. Uploaders must ignore this episode until pending is removed.
        meta = dict(meta)
        if getattr(self, "collection_author", None):
            meta["author"] = self.collection_author
        if getattr(self, 'collection_destination', None):
            meta['upload_destination'] = dict(self.collection_destination)
        atomic_json(record_dir / ".mp4-pending.json", dict(meta, _video_crf=14))
        atomic_json(record_dir / "meta.json", meta)

    def toggle_recording(self, message: Optional[str]) -> Tuple[bool, str]:
        if not self.is_recording:
            return self.start_recording(message)
        return self.stop_recording()

    def _preserve_recording_failure(self, reason: str) -> None:
        """Retain failed saves for diagnosis without publishing a success marker."""
        self.get_logger().error(reason)
        if not self.recent_record_dir or not os.path.isdir(self.recent_record_dir):
            return
        try:
            path = os.path.join(self.recent_record_dir, "recording-failure.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"reason": reason, "record_format": self.record_format}, handle)
            self.normalize_recording_owner(self.recent_record_dir)
        except OSError as exc:
            self.get_logger().error(f"Could not write recording failure details: {exc}")

    def delete_last_recording(self) -> bool:
        if not self.recent_record_dir:
            self.get_logger().warn("No recent recording to remove.")
            return False

        target = self.recent_record_dir
        try:
            # make empty dir by removing and creating same dir
            shutil.rmtree(target)
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            self.get_logger().error(f"Failed to remove recording: {exc}")
            return False

        self.get_logger().info(f"Recent recording {target} has been removed.")
        self.recent_record_dir = None
        return True

    def start_recording_srv(self, request: STRequest, response: STResponse):
        success, message = self.start_recording(request.message)
        response.success = success
        response.message = message
        return response

    def stop_recording_srv(self, request: Trigger.Request, response: Trigger.Response):
        success, message = self.stop_recording()
        response.success = success
        response.message = message
        return response

    def cancel_recording(self) -> Tuple[bool, str]:
        if not self._recording_start_lock.acquire(blocking=False):
            return False, "录制器正在切换状态，请稍候"
        try:
            return self._cancel_recording()
        finally:
            if not self.is_recording and self.rosbag_process is None:
                self._release_capture_priority()
            self._recording_start_lock.release()

    def _cancel_recording(self) -> Tuple[bool, str]:
        """Stop recording without uploading, notify cancellation, and delete."""
        if not self.is_recording or self.rosbag_process is None:
            self.get_logger().warn("Rosbag is not recording.")
            return False, "Rosbag is not recording."

        self.rosbag_process.send_signal(signal.SIGINT)
        self.rosbag_process.wait()
        self.rosbag_process = None
        self.is_recording = False

        # Notify storage node of cancellation (no upload)
        if self.recent_record_dir:
            cancelled_msg = String()
            cancelled_msg.data = self.recent_record_dir
            self.recording_cancelled_pub.publish(cancelled_msg)

        self.delete_last_recording()
        self._release_capture_priority()
        self.publish_recording_state()
        self.get_logger().info("Recording cancelled and deleted.")
        return True, "Recording cancelled"

    def cancel_recording_srv(self, request: Trigger.Request, response: Trigger.Response):
        success, message = self.cancel_recording()
        response.success = success
        response.message = message
        return response

    def toggle_recording_srv(self, request: STRequest, response: STResponse):
        success, message = self.toggle_recording(request.message)
        response.success = success
        response.message = message
        return response

    def delete_recording_srv(self, request: STRequest, response: STResponse):
        if self.is_recording:
            success, message = self.stop_recording()
            if not success:
                response.success = False
                response.message = message
                return response
        response.success = self.delete_last_recording()
        response.message = "Recent recording deleted." if response.success else "Nothing to delete."
        return response

    def save_meta_file(self, meta_data: dict, record_dir: Optional[str] = None):
        target_dir = record_dir or os.path.join(self.record_base_dir, self.task_name)
        meta_data_file = os.path.join(target_dir, "meta.json")
        author = getattr(self, "collection_author", None)
        if author:
            meta_data = dict(meta_data, author=author)
        if getattr(self, 'collection_destination', None):
            meta_data = dict(meta_data, upload_destination=dict(self.collection_destination))
        # meta.json is the completed-rosbag marker consumed by the uploader.
        with open(meta_data_file + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(meta_data, handle, ensure_ascii=False, indent=2)
        os.replace(meta_data_file + ".tmp", meta_data_file)

    def normalize_recording_owner(self, record_dir: Optional[str]) -> bool:
        """Assign a completed recording to its operator and make it mode 777.

        Do this only after every writer, including the metadata writer, has
        finished. Symlinks are deliberately not followed or modified.
        """
        if not record_dir:
            return False
        try:
            normalize_tree_access(
                record_dir, self.record_owner_uid, self.record_owner_gid
            )
            return True
        except OSError as exc:
            self.get_logger().error(
                f"Failed to apply operator ownership and mode 777 to {record_dir}: {exc}"
            )
            return False

    def shutdown(self):
        if self.is_recording:
            self.get_logger().info("Stopping recording before shutdown.")
            self.stop_recording()
        self._hdf5_shutdown.set()
        with self._hdf5_jobs_lock:
            pending_jobs = len(self._hdf5_jobs)
        if pending_jobs:
            self.get_logger().info(
                f"Cancelling {pending_jobs} HDF5 finalization job(s); staging MCAP is preserved."
            )
        with self._hdf5_process_lock:
            process = self._hdf5_process
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    process.terminate()
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    process.kill()
                process.wait(timeout=3.0)
        self._hdf5_executor.shutdown(wait=True, cancel_futures=True)
        self.destroy_node()


def main():
    from galaxis_core.sentry_setup import init_sentry
    init_sentry()
    rclpy.init()
    record_manager = RecordManager()

    executor = MultiThreadedExecutor()

    try:
        rclpy.spin(record_manager, executor)
    except KeyboardInterrupt:
        print("Keyboard interrupt received, shutting down record manager.")
    finally:
        executor.shutdown()
        record_manager.shutdown()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
