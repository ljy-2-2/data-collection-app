#!/usr/bin/env python3
"""Storage node — thin ROS 2 wrapper around StorageManager.

Handles multi-target upload, per-target GC, local retention, and
diagnostics publishing.  All business logic lives in the
``data_backend`` package.
"""

import os
import queue
import shutil
import threading

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from std_msgs.msg import String
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

from data_backend.config import load_storage_config
from data_backend.gc import compute_diagnostic_level
from data_backend.manager import StorageManager
from galaxis_core.ros_log_bridge import bridge_to_ros

RETRY_BACKOFF_SEC = 5.0
RETENTION_CHECK_INTERVAL_SEC = 300.0


class StorageNode(Node):
    def __init__(self):
        super().__init__("storage_node")
        bridge_to_ros("data_backend", self.get_logger())

        self.declare_parameter("upload_targets_file", "")
        self.declare_parameter("record_base_dir", "/root/datasets/rosbags")
        self.declare_parameter("upload_enabled", True)

        gp = self.get_parameter
        self._record_base_dir = str(gp("record_base_dir").value)
        self._upload_enabled = bool(gp("upload_enabled").value)

        self._upload_queue: queue.Queue = queue.Queue()
        self._mgr: StorageManager | None = None

        if not self._upload_enabled:
            self.get_logger().info("StorageNode started (upload DISABLED).")
            return

        targets_file = str(gp("upload_targets_file").value).strip()
        if not targets_file or not os.path.isfile(targets_file):
            self.get_logger().error(
                f"upload_targets_file not set or missing ({targets_file!r})"
            )
            return

        cfg = load_storage_config(targets_file)
        self._mgr = StorageManager(cfg, log=self.get_logger())

        if not self._mgr.targets:
            self.get_logger().error("No upload targets connected.")
            return

        # Upload subscription + worker
        self._setup_subscription()
        self._recover_pending()
        self._upload_thread = threading.Thread(target=self._upload_worker, daemon=True)
        self._upload_thread.start()

        # Retention timer
        self.create_timer(RETENTION_CHECK_INTERVAL_SEC, self._retention_callback)

        # Per-target GC timers
        self._diag_pub = self.create_publisher(DiagnosticArray, "/diagnostics", 10)
        for lt in self._mgr.targets:
            if lt.cfg.gc is not None:
                self.create_timer(
                    lt.cfg.gc.check_interval_sec,
                    lambda _lt=lt: self._gc_callback(_lt.cfg.name),
                )

        target_names = ", ".join(lt.cfg.name for lt in self._mgr.targets)
        gc_names = (
            ", ".join(lt.cfg.name for lt in self._mgr.targets if lt.cfg.gc is not None)
            or "none"
        )
        self.get_logger().info(
            f"StorageNode started — upload: [{target_names}], gc: [{gc_names}]"
        )

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def _setup_subscription(self):
        qos = QoSProfile(depth=10)
        qos.reliability = ReliabilityPolicy.RELIABLE
        qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.create_subscription(
            String,
            "/record_manager/recording_completed",
            self._on_recording_completed,
            qos,
        )
        self.create_subscription(
            String,
            "/record_manager/recording_cancelled",
            self._on_recording_cancelled,
            qos,
        )

    def _on_recording_completed(self, msg: String):
        self.get_logger().info(f"Recording completed: {msg.data}")
        self._upload_queue.put(msg.data)

    def _on_recording_cancelled(self, msg: String):
        self.get_logger().info(f"Recording cancelled (no upload): {msg.data}")

    def _recover_pending(self):
        for dir_path in self._mgr.recover_pending(self._record_base_dir):
            self._upload_queue.put(dir_path)

    def _upload_worker(self):
        import time

        while rclpy.ok():
            try:
                dir_path = self._upload_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            result = self._mgr.upload(dir_path)

            if result.can_delete_local and os.path.isdir(dir_path):
                self.get_logger().info(f"Deleting local {dir_path}")
                shutil.rmtree(dir_path)

            if self._mgr.needs_retry(result.rec_name) and os.path.isdir(dir_path):
                self.get_logger().warn(
                    f"Re-queuing {result.rec_name} in {RETRY_BACKOFF_SEC}s"
                )
                time.sleep(RETRY_BACKOFF_SEC)
                self._upload_queue.put(dir_path)

    # ------------------------------------------------------------------
    # GC
    # ------------------------------------------------------------------

    def _gc_callback(self, target_name: str):
        results = self._mgr.run_gc(target_name)
        for name, result in results.items():
            self._publish_gc_diagnostics(name, result)

    def _publish_gc_diagnostics(self, target_name: str, result):
        gc_cfg = None
        for lt in self._mgr.targets:
            if lt.cfg.name == target_name and lt.cfg.gc is not None:
                gc_cfg = lt.cfg.gc
                break
        if gc_cfg is None:
            return

        level = compute_diagnostic_level(gc_cfg, result)
        total_gb = result.total_bytes / 1_000_000_000

        if level == 0:
            message = f"Storage OK ({total_gb:.2f} GB)"
        elif level == 1:
            message = f"Storage high ({total_gb:.2f} GB)"
        else:
            message = result.error or f"Storage critical ({total_gb:.2f} GB)"

        status = DiagnosticStatus()
        status.level = bytes([level])
        status.name = f"storage_gc/{target_name}"
        status.message = message
        status.hardware_id = target_name
        status.values = [
            KeyValue(key="total_recordings", value=str(result.total_recordings)),
            KeyValue(key="total_gb", value=f"{total_gb:.2f}"),
            KeyValue(key="eligible_count", value=str(result.eligible_count)),
            KeyValue(key="deleted_count", value=str(result.deleted_count)),
            KeyValue(key="orphan_count", value=str(result.orphan_count)),
        ]

        msg = DiagnosticArray()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.status = [status]
        self._diag_pub.publish(msg)

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def _retention_callback(self):
        self._mgr.retention_cleanup(self._record_base_dir)
        self._mgr.purge_state()


def main():
    from galaxis_core.sentry_setup import init_sentry

    init_sentry()
    rclpy.init()
    node = StorageNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        print("Keyboard interrupt, shutting down storage node.")
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
