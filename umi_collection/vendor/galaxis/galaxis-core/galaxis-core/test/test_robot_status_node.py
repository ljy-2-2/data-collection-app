"""Unit tests for robot_status_node."""

from datetime import datetime
from unittest.mock import MagicMock, patch


class TestRobotStatusNode:
    """Tests for RobotStatusNode."""

    def _make_node(self, mock_rclpy, battery_topic=""):
        """Import and instantiate the node with mocked ROS2."""
        from galaxis_core.robot_status_node import RobotStatusNode

        with patch.object(RobotStatusNode, "__init__", lambda self: None):
            node = RobotStatusNode()

        # Manually run init logic with controlled params
        from test.conftest import FakeNode

        fake = FakeNode("robot_status")
        node._name = fake._name
        node._params = {}
        node._logger = MagicMock()

        # Bind FakeNode methods
        node.declare_parameter = fake.declare_parameter
        node.get_parameter = fake.get_parameter
        node.get_logger = fake.get_logger
        node.create_subscription = fake.create_subscription
        node.create_timer = fake.create_timer
        node.create_publisher = fake.create_publisher

        # Set params
        node.declare_parameter("base_url", "http://localhost:8000/api")
        node.declare_parameter("api_key", "test-key")
        node.declare_parameter("robot_type", "test_robot")
        node.declare_parameter("status_interval_sec", 30.0)
        node.declare_parameter("battery_topic", battery_topic)

        import time

        node._backend = MagicMock()
        node._start_time = time.monotonic()
        node._battery_pct = -1
        node._battery_charging = False
        node._disk_free = 0
        node._disk_used = 0
        node._is_recording = False
        node._gate_level = 0
        node._gate_conditions = {}
        node._gate_snapshot = ()
        node._last_gate_report = 0.0
        node._gate_throttle = 2.0
        node._robot_type = "test_robot"

        return node

    def test_payload_matches_api_schema(self, mock_rclpy):
        node = self._make_node(mock_rclpy)
        payload = node._build_payload()

        assert "robot_type" in payload
        assert payload["robot_type"] == "test_robot"
        assert "reported_at" in payload
        datetime.fromisoformat(payload["reported_at"])  # validates ISO8601 format
        assert "status" in payload

        status = payload["status"]
        assert "battery" in status
        assert "pct" in status["battery"]
        assert "charging" in status["battery"]
        assert "connection" in status
        assert "quality_pct" in status["connection"]
        assert "uptime_sec" in status
        assert status["uptime_sec"] >= 0
        assert "metrics" in status
        assert len(status["metrics"]) == 4

        metric_names = {m["name"] for m in status["metrics"]}
        assert metric_names == {
            "disk_free_bytes",
            "disk_used_bytes",
            "is_recording",
            "gate_level",
        }

        for m in status["metrics"]:
            assert "type" in m
            assert "unit" in m
            assert "value" in m

    def test_defaults_before_any_subscription_data(self, mock_rclpy):
        node = self._make_node(mock_rclpy)
        payload = node._build_payload()

        assert payload["status"]["battery"]["pct"] == -1
        assert payload["status"]["battery"]["charging"] is False
        assert payload["status"]["connection"]["quality_pct"] == 100

        metrics = {m["name"]: m["value"] for m in payload["status"]["metrics"]}
        assert metrics["disk_free_bytes"] == 0
        assert metrics["disk_used_bytes"] == 0
        assert metrics["is_recording"] == 0
        assert metrics["gate_level"] == 0

    def test_report_status_calls_backend(self, mock_rclpy):
        node = self._make_node(mock_rclpy)
        node._backend.update_robot_status.return_value = {}

        node._report_status()

        node._backend.update_robot_status.assert_called_once()
        payload = node._backend.update_robot_status.call_args[0][0]
        assert payload["robot_type"] == "test_robot"

    def test_no_crash_on_backend_failure(self, mock_rclpy):
        node = self._make_node(mock_rclpy)
        node._backend.update_robot_status.return_value = None

        # Should not raise
        node._report_status()
        node.get_logger().warning.assert_called()

    def test_no_crash_on_backend_exception(self, mock_rclpy):
        node = self._make_node(mock_rclpy)
        node._backend.update_robot_status.side_effect = Exception("connection refused")

        # Should not raise
        node._report_status()
        node.get_logger().error.assert_called()

    def test_battery_callback(self, mock_rclpy):
        node = self._make_node(mock_rclpy)

        msg = MagicMock()
        msg.percentage = 0.85  # BatteryState.percentage is a 0.0-1.0 ratio
        msg.power_supply_status = 1  # CHARGING

        node._battery_cb(msg)

        assert node._battery_pct == 85
        assert node._battery_charging is True

    def test_disk_callbacks(self, mock_rclpy):
        node = self._make_node(mock_rclpy)

        free_msg = MagicMock()
        free_msg.data = 1_000_000
        node._disk_free_cb(free_msg)
        assert node._disk_free == 1_000_000

        used_msg = MagicMock()
        used_msg.data = 500_000
        node._disk_used_cb(used_msg)
        assert node._disk_used == 500_000

    def test_recording_callback(self, mock_rclpy):
        node = self._make_node(mock_rclpy)

        msg = MagicMock()
        msg.data = True
        node._recording_cb(msg)
        assert node._is_recording is True

    def test_gate_level_callback(self, mock_rclpy):
        node = self._make_node(mock_rclpy)

        msg = MagicMock()
        msg.data = 2
        node._gate_level_cb(msg)
        assert node._gate_level == 2

    def test_payload_reflects_subscription_data(self, mock_rclpy):
        node = self._make_node(mock_rclpy)

        # Simulate receiving data
        node._battery_pct = 72
        node._battery_charging = True
        node._disk_free = 5_000_000
        node._disk_used = 2_000_000
        node._is_recording = True
        node._gate_level = 1

        payload = node._build_payload()

        assert payload["status"]["battery"]["pct"] == 72
        assert payload["status"]["battery"]["charging"] is True

        metrics = {m["name"]: m["value"] for m in payload["status"]["metrics"]}
        assert metrics["disk_free_bytes"] == 5_000_000
        assert metrics["disk_used_bytes"] == 2_000_000
        assert metrics["is_recording"] == 1
        assert metrics["gate_level"] == 1

    def test_payload_includes_gate_conditions(self, mock_rclpy):
        node = self._make_node(mock_rclpy)
        node._gate_conditions = {
            "gate_level": 2,
            "groups": {"safety": {"level": 0, "settled": True, "conditions": []}},
        }

        payload = node._build_payload()
        assert "gate_conditions" in payload["status"]
        assert payload["status"]["gate_conditions"]["gate_level"] == 2
        assert "safety" in payload["status"]["gate_conditions"]["groups"]

    def test_diagnostics_cb_builds_gate_conditions(self, mock_rclpy):
        node = self._make_node(mock_rclpy)
        node._gate_level = 2

        # Build a fake DiagnosticArray message
        cond_status = MagicMock()
        cond_status.hardware_id = "recording_gate"
        cond_status.name = "recording_gate/safety/estop"
        cond_status.level = 0
        cond_status.message = "ok"
        kv1 = MagicMock()
        kv1.key = "escalation"
        kv1.value = "2"
        kv2 = MagicMock()
        kv2.key = "group"
        kv2.value = "safety"
        cond_status.values = [kv1, kv2]

        group_status = MagicMock()
        group_status.hardware_id = "recording_gate"
        group_status.name = "recording_gate/safety"
        group_status.level = 0
        group_status.message = "ok"
        gkv1 = MagicMock()
        gkv1.key = "settled"
        gkv1.value = "True"
        gkv2 = MagicMock()
        gkv2.key = "level"
        gkv2.value = "0"
        group_status.values = [gkv1, gkv2]

        diag_msg = MagicMock()
        diag_msg.status = [cond_status, group_status]

        node._diagnostics_cb(diag_msg)

        assert "groups" in node._gate_conditions
        safety = node._gate_conditions["groups"]["safety"]
        assert safety["level"] == 0
        assert safety["settled"] is True
        assert len(safety["conditions"]) == 1
        assert safety["conditions"][0]["name"] == "estop"
        assert safety["conditions"][0]["passed"] is True

    def test_diagnostics_cb_ignores_non_gate_entries(self, mock_rclpy):
        node = self._make_node(mock_rclpy)

        other_status = MagicMock()
        other_status.hardware_id = "other_node"
        other_status.name = "other/thing"

        diag_msg = MagicMock()
        diag_msg.status = [other_status]

        node._diagnostics_cb(diag_msg)

        assert node._gate_conditions == {}

    def test_empty_gate_conditions_in_payload(self, mock_rclpy):
        node = self._make_node(mock_rclpy)

        payload = node._build_payload()
        assert payload["status"]["gate_conditions"] == {}
