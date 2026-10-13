#!/usr/bin/env python3
"""Periodic robot status reporter node.

Subscribes to internal ROS topics and pushes aggregated telemetry to the
backend via ``PUT /robot/status`` on a configurable interval.
"""

import time
from datetime import datetime, timezone

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy

from std_msgs.msg import Bool, Int64, UInt8
from diagnostic_msgs.msg import DiagnosticArray

from galaxis_core.backend_client import create_backend


class RobotStatusNode(Node):

    def __init__(self):
        super().__init__("robot_status")

        # -- parameters --------------------------------------------------------
        self.declare_parameter("base_url", "http://localhost:8000/api")
        self.declare_parameter("api_key", "")
        self.declare_parameter("offline_mode", False)
        self.declare_parameter("task_file", "")
        self.declare_parameter("robot_type", "unknown")
        self.declare_parameter("status_interval_sec", 30.0)
        self.declare_parameter("gate_throttle_sec", 2.0)
        self.declare_parameter("battery_topic", "")

        gp = self.get_parameter
        base_url = gp("base_url").value
        api_key = gp("api_key").value
        offline_mode = bool(gp("offline_mode").value)
        task_file = str(gp("task_file").value)
        self._robot_type: str = gp("robot_type").value
        interval: float = gp("status_interval_sec").value
        self._gate_throttle: float = gp("gate_throttle_sec").value
        battery_topic: str = gp("battery_topic").value

        self._backend = create_backend(
            offline_mode=offline_mode, task_file=task_file,
            base_url=base_url, api_key=api_key,
        )
        self._start_time = time.monotonic()

        # -- state from subscriptions ------------------------------------------
        self._battery_pct: int = -1
        self._battery_charging: bool = False
        self._disk_free: int = 0
        self._disk_used: int = 0
        self._is_recording: bool = False
        self._gate_level: int = 0
        self._gate_conditions: dict = {}
        self._gate_snapshot: tuple = ()
        self._last_gate_report: float = 0.0

        # -- subscriptions -----------------------------------------------------
        if battery_topic:
            from sensor_msgs.msg import BatteryState

            self.create_subscription(
                BatteryState, battery_topic, self._battery_cb, 10,
            )

        self.create_subscription(
            Int64, "/record_manager/free", self._disk_free_cb, 10,
        )
        self.create_subscription(
            Int64, "/record_manager/used", self._disk_used_cb, 10,
        )
        self.create_subscription(
            Bool, "/record_manager/recording", self._recording_cb, 10,
        )
        gate_qos = QoSProfile(depth=1)
        gate_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        gate_qos.reliability = ReliabilityPolicy.RELIABLE
        self.create_subscription(
            UInt8, "/recording_gate/gate_level", self._gate_level_cb, gate_qos,
        )
        self.create_subscription(
            DiagnosticArray, "/diagnostics", self._diagnostics_cb, 10,
        )

        # -- timer -------------------------------------------------------------
        self.create_timer(interval, self._report_status)

        self.get_logger().info(
            f"Robot status reporter started (interval={interval}s, "
            f"robot_type={self._robot_type})"
        )

    # -- subscription callbacks ------------------------------------------------

    def _battery_cb(self, msg):
        self._battery_pct = int(msg.percentage * 100)
        # BatteryState.POWER_SUPPLY_STATUS_CHARGING == 1
        self._battery_charging = msg.power_supply_status == 1

    def _disk_free_cb(self, msg):
        self._disk_free = msg.data

    def _disk_used_cb(self, msg):
        self._disk_used = msg.data

    def _recording_cb(self, msg):
        self._is_recording = msg.data

    def _gate_level_cb(self, msg):
        self._gate_level = msg.data

    def _diagnostics_cb(self, msg):
        """Extract recording_gate entries and build structured gate_conditions."""
        groups: dict[str, dict] = {}
        for status in msg.status:
            if status.hardware_id != "recording_gate":
                continue
            parts = status.name.split("/")
            # recording_gate/<group>/<condition> → condition entry
            # recording_gate/<group> → group summary
            if len(parts) == 3:
                _, group_name, cond_name = parts
                group = groups.setdefault(
                    group_name, {"level": 0, "settled": True, "conditions": []}
                )
                values = {kv.key: kv.value for kv in status.values}
                try:
                    escalation = int(values.get("escalation", 0))
                except (ValueError, TypeError):
                    escalation = 0
                # DiagnosticStatus.level is a byte (bytes on the wire); some
                # publishers set it as a plain int — accept either.
                level = status.level
                level = level[0] if isinstance(level, (bytes, bytearray)) else level
                group["conditions"].append({
                    "name": cond_name,
                    "passed": level == 0,
                    "reason": status.message,
                    "escalation": escalation,
                })
            elif len(parts) == 2:
                _, group_name = parts
                group = groups.setdefault(
                    group_name, {"level": 0, "settled": True, "conditions": []}
                )
                values = {kv.key: kv.value for kv in status.values}
                try:
                    group["level"] = int(values.get("level", 0))
                except (ValueError, TypeError):
                    group["level"] = 0
                group["settled"] = values.get("settled", "True") == "True"

        if groups:
            self._gate_conditions = {
                "gate_level": self._gate_level,
                "groups": groups,
            }
            # Detect state change: level + per-condition pass/fail
            snapshot = (
                self._gate_level,
                frozenset(
                    (g, c["name"], c["passed"])
                    for g, grp in groups.items()
                    for c in grp["conditions"]
                ),
            )
            if snapshot != self._gate_snapshot:
                now = time.monotonic()
                if now - self._last_gate_report >= self._gate_throttle:
                    self._gate_snapshot = snapshot
                    self._report_status()

    # -- timer callback --------------------------------------------------------

    def _build_payload(self) -> dict:
        return {
            "robot_type": self._robot_type,
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "status": {
                "battery": {
                    "pct": self._battery_pct,
                    "charging": self._battery_charging,
                },
                # TODO: connection quality is hard-coded; implement real measurement
                "connection": {"quality_pct": 100},
                "uptime_sec": time.monotonic() - self._start_time,
                "metrics": [
                    {
                        "name": "disk_free_bytes",
                        "type": "scalar",
                        "unit": "bytes",
                        "value": self._disk_free,
                    },
                    {
                        "name": "disk_used_bytes",
                        "type": "scalar",
                        "unit": "bytes",
                        "value": self._disk_used,
                    },
                    {
                        "name": "is_recording",
                        "type": "scalar",
                        "unit": "bool",
                        "value": int(self._is_recording),
                    },
                    {
                        "name": "gate_level",
                        "type": "scalar",
                        "unit": "level",
                        "value": self._gate_level,
                    },
                ],
                "gate_conditions": self._gate_conditions,
            },
        }

    def _report_status(self):
        self._last_gate_report = time.monotonic()
        payload = self._build_payload()
        try:
            result = self._backend.update_robot_status(payload)
            if result is None:
                self.get_logger().warning("Status report failed (see backend_client logs)")
            else:
                self.get_logger().debug("Status report sent")
        except Exception as exc:
            self.get_logger().error(f"Status report exception: {exc}")


def main():
    from galaxis_core.sentry_setup import init_sentry

    init_sentry()
    rclpy.init()
    node = RobotStatusNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
