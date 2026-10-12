"""Integration tests for RecordingGateNode with a live ROS 2 stack.

Requires a built ROS 2 workspace (run inside the project Docker image).
Tests are marked ``@pytest.mark.integration`` and auto-skip if rclpy
is not available.

Run with:
    make test-gate          # builds image + runs tests in Docker
    make test-gate-down     # cleanup
"""

import os
import shutil
import tempfile
import textwrap
import threading
import time

import pytest

try:
    import rclpy
    import rclpy.executors
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from std_msgs.msg import Bool, Float64, String, UInt8
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
    from geometry_msgs.msg import PoseStamped
    from sensor_msgs.msg import JointState
    from tf2_ros import StaticTransformBroadcaster, TransformBroadcaster
    from geometry_msgs.msg import TransformStamped

    HAS_ROS2 = True
except ImportError:
    HAS_ROS2 = False

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not HAS_ROS2, reason="rclpy not available"),
]


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


class GateTestHarness:
    """Manages a RecordingGateNode + test helper node in a background executor."""

    def __init__(self, config_yaml: str):
        self._tmpdir = tempfile.mkdtemp()
        self._config_path = os.path.join(self._tmpdir, "gate_config.yaml")
        with open(self._config_path, "w") as f:
            f.write(config_yaml)

        rclpy.init(
            args=[
                "--ros-args",
                "-p",
                f"recording_gate_config:={self._config_path}",
            ]
        )

        from galaxis_core.recording_gate_node import RecordingGateNode

        self._gate_node = RecordingGateNode()

        # Test helper node
        self._helper = Node("gate_test_helper")
        self._publishers = {}

        # Subscribe to gate_level (latched)
        self._gate_levels = []
        self._gate_level_lock = threading.Lock()
        latched_qos = QoSProfile(depth=10)
        latched_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        latched_qos.reliability = ReliabilityPolicy.RELIABLE
        self._helper.create_subscription(
            UInt8,
            "/recording_gate/gate_level",
            self._on_gate_level,
            latched_qos,
        )

        # Subscribe to diagnostics
        self._diagnostics = []
        self._diag_lock = threading.Lock()
        self._helper.create_subscription(
            DiagnosticArray,
            "/diagnostics",
            self._on_diagnostics,
            10,
        )

        # Spin in background
        self._executor = rclpy.executors.MultiThreadedExecutor()
        self._executor.add_node(self._gate_node)
        self._executor.add_node(self._helper)
        self._spin_thread = threading.Thread(
            target=self._executor.spin,
            daemon=True,
        )
        self._spin_thread.start()

    def _on_gate_level(self, msg):
        with self._gate_level_lock:
            self._gate_levels.append(msg.data)

    def _on_diagnostics(self, msg):
        with self._diag_lock:
            self._diagnostics.append(msg)

    def publish(self, topic, msg_type, msg, qos=10):
        key = f"{topic}:{id(qos)}" if not isinstance(qos, int) else topic
        if key not in self._publishers:
            self._publishers[key] = self._helper.create_publisher(
                msg_type,
                topic,
                qos,
            )
        self._publishers[key].publish(msg)

    def publish_latched(self, topic, msg_type, msg, match_timeout=5.0):
        """Publish one latched message, waiting for subscriber match first."""
        latched_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        key = f"{topic}:latched"
        if key not in self._publishers:
            self._publishers[key] = self._helper.create_publisher(
                msg_type,
                topic,
                latched_qos,
            )
        pub = self._publishers[key]
        deadline = time.monotonic() + match_timeout
        matched = False
        while time.monotonic() < deadline:
            if pub.get_subscription_count() > 0:
                matched = True
                break
            time.sleep(0.05)
        if not matched:
            self._helper.get_logger().warning(
                f"publish_latched: no subscriber matched for {topic} "
                f"within {match_timeout}s"
            )
        pub.publish(msg)

    def publish_bool(self, topic, value):
        msg = Bool()
        msg.data = value
        self.publish(topic, Bool, msg)

    def publish_float64(self, topic, value):
        msg = Float64()
        msg.data = value
        self.publish(topic, Float64, msg)

    def publish_pose_stamped(self, topic, x=0.0, y=0.0, z=0.0):
        msg = PoseStamped()
        msg.header.stamp = self._helper.get_clock().now().to_msg()
        msg.header.frame_id = "map"
        msg.pose.position.x = x
        msg.pose.position.y = y
        msg.pose.position.z = z
        msg.pose.orientation.w = 1.0
        self.publish(topic, PoseStamped, msg)

    def publish_joint_state(self, topic, effort=None, position=None):
        msg = JointState()
        msg.header.stamp = self._helper.get_clock().now().to_msg()
        msg.name = ["joint_0"]
        msg.effort = effort or [0.0]
        msg.position = position or [0.0]
        msg.velocity = [0.0]
        self.publish(topic, JointState, msg)

    def publish_diagnostic(self, topic, statuses):
        msg = DiagnosticArray()
        msg.header.stamp = self._helper.get_clock().now().to_msg()
        msg.status = statuses
        self.publish(topic, DiagnosticArray, msg)

    def publish_static_tf(self, parent, child):
        if not hasattr(self, "_static_broadcaster"):
            self._static_broadcaster = StaticTransformBroadcaster(self._helper)
        t = TransformStamped()
        t.header.stamp = self._helper.get_clock().now().to_msg()
        t.header.frame_id = parent
        t.child_frame_id = child
        t.transform.rotation.w = 1.0
        self._static_broadcaster.sendTransform(t)

    def publish_dynamic_tf(self, parent, child):
        if not hasattr(self, "_dynamic_broadcaster"):
            self._dynamic_broadcaster = TransformBroadcaster(self._helper)
        t = TransformStamped()
        t.header.stamp = self._helper.get_clock().now().to_msg()
        t.header.frame_id = parent
        t.child_frame_id = child
        t.transform.rotation.w = 1.0
        self._dynamic_broadcaster.sendTransform(t)

    def wait_for_gate_level(self, expected, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._gate_level_lock:
                if self._gate_levels and self._gate_levels[-1] == expected:
                    return True
            time.sleep(0.05)
        return False

    def call_invalidate(self, timeout=5.0):
        """Call the ~/invalidate service and wait for the response."""
        from std_srvs.srv import Trigger

        client = self._helper.create_client(Trigger, "/recording_gate/invalidate")
        assert client.wait_for_service(timeout_sec=timeout)
        future = client.call_async(Trigger.Request())
        deadline = time.monotonic() + timeout
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.05)
        return future.result()

    def wait_for_gate_level_gte(self, min_level, timeout=5.0):
        """Poll until the latest gate_level >= *min_level*, or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._gate_level_lock:
                if self._gate_levels and self._gate_levels[-1] >= min_level:
                    return True
            time.sleep(0.05)
        return False

    def get_latest_gate_level(self):
        with self._gate_level_lock:
            return self._gate_levels[-1] if self._gate_levels else None

    def get_diagnostics(self):
        with self._diag_lock:
            return list(self._diagnostics)

    def shutdown(self):
        self._executor.shutdown()
        self._gate_node.destroy_node()
        self._helper.destroy_node()
        rclpy.try_shutdown()
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Integration scenarios
# ---------------------------------------------------------------------------


class TestGateStartsAtHardStop:
    """Gate with one topic_condition condition. No messages published.
    Verify gate publishes 2 (HARD_STOP) via latched QoS."""

    def test_gate_starts_at_hard_stop(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 1.0
            recovery_sec: 0.5
            groups:
              safety:
                default_type: topic_condition
                conditions:
                  estop:
                    topic: /test/estop
                    condition: "not msg.data"
                    timeout_sec: 1.0
        """)
        harness = GateTestHarness(config)
        try:
            assert harness.wait_for_gate_level(2, timeout=3.0)
        finally:
            harness.shutdown()


class TestGateOpensAfterSettle:
    """Gate with one topic_condition condition (settle_sec=2). Publish passing Bool.
    Wait for settle. Verify gate transitions from 2 to 0."""

    def test_gate_opens_after_settle(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 2.0
            recovery_sec: 0.5
            groups:
              safety:
                default_type: topic_condition
                conditions:
                  estop:
                    topic: /test/estop
                    condition: "not msg.data"
                    timeout_sec: 2.0
        """)
        harness = GateTestHarness(config)
        try:
            assert harness.wait_for_gate_level(2, timeout=2.0)

            for _ in range(30):
                harness.publish_bool("/test/estop", False)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=3.0)
        finally:
            harness.shutdown()


class TestGateBlocksOnConditionFailure:
    """Gate open (level 0). Stop publishing (freshness timeout).
    Verify gate transitions to level >= 1. Resume publishing.
    Verify gate recovers to 0 after recovery_sec."""

    def test_gate_blocks_on_condition_failure(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 1.0
            groups:
              safety:
                default_type: topic_condition
                conditions:
                  estop:
                    topic: /test/estop
                    condition: "not msg.data"
                    timeout_sec: 0.5
        """)
        harness = GateTestHarness(config)
        try:
            for _ in range(20):
                harness.publish_bool("/test/estop", False)
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Stop publishing — wait for freshness timeout (0.5s) + margin
            time.sleep(1.5)
            assert harness.wait_for_gate_level_gte(1, timeout=3.0)

            # Resume publishing
            for _ in range(30):
                harness.publish_bool("/test/estop", False)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestDiagnosticsPublishedPerCondition:
    """Gate with two conditions in different groups. Verify /diagnostics
    messages contain entries named recording_gate/{group}/{condition}."""

    def test_diagnostics_published_per_condition(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              safety:
                default_type: topic_condition
                conditions:
                  estop:
                    topic: /test/estop
                    condition: "not msg.data"
                    timeout_sec: 2.0
              limits:
                default_type: topic_condition
                conditions:
                  duration:
                    topic: /test/elapsed
                    condition: "msg.data < 300.0"
                    timeout_sec: -1.0
        """)
        harness = GateTestHarness(config)
        try:
            time.sleep(1.0)

            diags = harness.get_diagnostics()
            assert len(diags) > 0

            all_names = set()
            for diag_msg in diags:
                for status in diag_msg.status:
                    all_names.add(status.name)

            assert "recording_gate/safety/estop" in all_names
            assert "recording_gate/limits/duration" in all_names
            assert "recording_gate/safety" in all_names
            assert "recording_gate/limits" in all_names

            for diag_msg in diags:
                for status in diag_msg.status:
                    if status.name.startswith("recording_gate/"):
                        assert status.hardware_id == "recording_gate"
        finally:
            harness.shutdown()


class TestLatchedGateLevelForLateSubscriber:
    """Gate settles to level 0. A new subscriber connects after settling.
    Verify the late subscriber immediately receives the current gate level
    (tests TRANSIENT_LOCAL durability)."""

    def test_latched_gate_level_for_late_subscriber(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              safety:
                default_type: topic_condition
                conditions:
                  estop:
                    topic: /test/estop
                    condition: "not msg.data"
                    timeout_sec: 2.0
        """)
        harness = GateTestHarness(config)
        try:
            for _ in range(20):
                harness.publish_bool("/test/estop", False)
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Create a LATE subscriber
            received = []
            late_qos = QoSProfile(depth=10)
            late_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
            late_qos.reliability = ReliabilityPolicy.RELIABLE
            late_node = Node("late_subscriber")
            late_node.create_subscription(
                UInt8,
                "/recording_gate/gate_level",
                lambda msg: received.append(msg.data),
                late_qos,
            )

            executor = rclpy.executors.SingleThreadedExecutor()
            executor.add_node(late_node)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not received:
                executor.spin_once(timeout_sec=0.1)

            assert len(received) > 0
            assert received[-1] == 0

            executor.shutdown()
            late_node.destroy_node()
        finally:
            harness.shutdown()


class TestTopicConditionCheckerRateAndExpression:
    """Gate with topic_condition condition using min_rate_hz and condition
    expression. Publish messages at various rates and values."""

    def test_topic_condition_rate_and_expression(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              sensors:
                default_type: topic_condition
                conditions:
                  temperature:
                    topic: /test/temp
                    timeout_sec: 1.0
                    condition: "msg.data < 100.0"
                    min_rate_hz: 5.0
                    rate_window_sec: 2.0
                    escalation: 2
        """)
        harness = GateTestHarness(config)
        try:
            assert harness.wait_for_gate_level(2, timeout=2.0)

            # Publish good values at high rate
            for _ in range(30):
                harness.publish_float64("/test/temp", 50.0)
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Publish bad value (fails content expression)
            for _ in range(10):
                harness.publish_float64("/test/temp", 150.0)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(2, timeout=3.0)

            # Publish good values again to recover
            for _ in range(30):
                harness.publish_float64("/test/temp", 50.0)
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestTfAvailabilityIntegration:
    """TF frame checking with multiple frames and staleness via max_age."""

    def test_tf_availability(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 1.0
            recovery_sec: 0.5
            groups:
              tf:
                conditions:
                  frames:
                    type: tf_availability
                    frames:
                      - source: test_odom
                        target: test_base_link
                        max_age_sec: -1.0
                      - source: test_base_link
                        target: test_camera
                        max_age_sec: -1.0
        """)
        harness = GateTestHarness(config)
        try:
            # No TF → HARD_STOP
            assert harness.wait_for_gate_level(2, timeout=3.0)

            # Publish static TFs repeatedly until DDS discovery matches
            # publisher to TransformListener subscriber. Each sendTransform
            # call re-publishes on /tf_static (TRANSIENT_LOCAL).
            for _ in range(30):
                harness.publish_static_tf("test_odom", "test_base_link")
                harness.publish_static_tf("test_base_link", "test_camera")
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()

    def test_tf_missing_one_frame(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              tf:
                conditions:
                  frames:
                    type: tf_availability
                    frames:
                      - source: test_odom
                        target: test_base_link
                        max_age_sec: -1.0
                      - source: test_base_link
                        target: test_missing
                        max_age_sec: -1.0
        """)
        harness = GateTestHarness(config)
        try:
            # Only publish one of two required frames
            for _ in range(20):
                harness.publish_static_tf("test_odom", "test_base_link")
                time.sleep(0.1)

            # Gate stays blocked (missing test_base_link→test_missing)
            assert harness.wait_for_gate_level_gte(1, timeout=3.0)
        finally:
            harness.shutdown()

    def test_tf_staleness(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              tf:
                conditions:
                  frames:
                    type: tf_availability
                    frames:
                      - source: test_odom
                        target: test_base_link
                        max_age_sec: 1.0
        """)
        harness = GateTestHarness(config)
        try:
            # Publish dynamic TF continuously → gate opens
            for _ in range(20):
                harness.publish_dynamic_tf("test_odom", "test_base_link")
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Stop publishing → transform goes stale (>1s)
            assert harness.wait_for_gate_level_gte(1, timeout=4.0)

            # Resume → gate recovers
            for _ in range(20):
                harness.publish_dynamic_tf("test_odom", "test_base_link")
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestTopicHealthWithPoseStamped:
    """topic_condition with geometry_msgs/PoseStamped and nested attribute
    expression: msg.pose.position.x bounds checking."""

    def test_pose_stamped_expression(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              nav:
                default_type: topic_condition
                conditions:
                  robot_pose:
                    topic: /test/robot_pose
                    timeout_sec: 2.0
                    condition: "msg.pose.position.x > -2.0 and msg.pose.position.x < 2.0"
        """)
        harness = GateTestHarness(config)
        try:
            # Publish in-bounds pose → gate opens
            for _ in range(15):
                harness.publish_pose_stamped("/test/robot_pose", x=0.5)
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Publish out-of-bounds → gate blocks
            for _ in range(10):
                harness.publish_pose_stamped("/test/robot_pose", x=5.0)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(2, timeout=3.0)

            # Publish back in-bounds → gate recovers
            for _ in range(20):
                harness.publish_pose_stamped("/test/robot_pose", x=-1.0)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestTopicHealthWithJointState:
    """topic_condition with sensor_msgs/JointState, array subscript expression,
    and min_rate_hz checking."""

    def test_joint_state_expression_and_rate(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              arm:
                default_type: topic_condition
                conditions:
                  joint_effort:
                    topic: /test/joint_states
                    timeout_sec: 2.0
                    condition: "msg.effort[0] < 10.0"
                    min_rate_hz: 5.0
                    rate_window_sec: 2.0
        """)
        harness = GateTestHarness(config)
        try:
            # Publish good effort at high rate → gate opens
            for _ in range(30):
                harness.publish_joint_state("/test/joint_states", effort=[5.0])
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Publish bad effort → gate blocks (expression fails)
            for _ in range(10):
                harness.publish_joint_state("/test/joint_states", effort=[15.0])
                time.sleep(0.1)

            assert harness.wait_for_gate_level(2, timeout=3.0)

            # Publish good effort at high rate → gate recovers
            for _ in range(30):
                harness.publish_joint_state("/test/joint_states", effort=[3.0])
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestDiagnosticsErrorRateIntegration:
    """diagnostics_error_rate with real DiagnosticArray messages.
    Errors accumulate past threshold, then expire from sliding window."""

    def test_diagnostics_error_rate(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              health:
                conditions:
                  error_rate:
                    type: diagnostics_error_rate
                    topic: /test/diagnostics
                    max_errors: 3
                    window_sec: 3.0
        """)
        harness = GateTestHarness(config)
        try:
            # Publish clean diagnostics → gate opens
            for _ in range(10):
                ok_status = DiagnosticStatus()
                ok_status.level = DiagnosticStatus.OK
                ok_status.name = "system"
                ok_status.message = "ok"
                harness.publish_diagnostic("/test/diagnostics", [ok_status])
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Publish 3 ERROR diagnostics → gate blocks
            for _ in range(3):
                err_status = DiagnosticStatus()
                err_status.level = DiagnosticStatus.ERROR
                err_status.name = "motor"
                err_status.message = "fault"
                harness.publish_diagnostic("/test/diagnostics", [err_status])
                time.sleep(0.1)

            assert harness.wait_for_gate_level(2, timeout=3.0)

            # Wait for sliding window to expire (3s + margin)
            time.sleep(3.5)

            # Publish clean diagnostics to keep checker alive
            for _ in range(10):
                ok_status = DiagnosticStatus()
                ok_status.level = DiagnosticStatus.OK
                ok_status.name = "system"
                ok_status.message = "ok"
                harness.publish_diagnostic("/test/diagnostics", [ok_status])
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestMultiGroupSettlingIntegration:
    """Two groups with different settle_sec settle independently.
    Gate opens only when both have settled."""

    def test_multi_group_settling(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            recovery_sec: 0.5
            groups:
              fast:
                settle_sec: 1.0
                default_type: topic_condition
                conditions:
                  estop:
                    topic: /test/fast_estop
                    condition: "not msg.data"
                    timeout_sec: 2.0
              slow:
                settle_sec: 3.0
                default_type: topic_condition
                conditions:
                  sensor:
                    topic: /test/slow_sensor
                    condition: "not msg.data"
                    timeout_sec: 2.0
        """)
        harness = GateTestHarness(config)
        try:
            # Publish both topics continuously
            for _ in range(15):
                harness.publish_bool("/test/fast_estop", False)
                harness.publish_bool("/test/slow_sensor", False)
                time.sleep(0.1)

            # After ~1.5s: fast settled but slow hasn't → gate still blocked
            assert harness.wait_for_gate_level_gte(1, timeout=1.0)

            # Keep publishing until slow settles (~3s total)
            for _ in range(25):
                harness.publish_bool("/test/fast_estop", False)
                harness.publish_bool("/test/slow_sensor", False)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Stop publishing fast topic → gate blocks (timeout_sec=2.0)
            time.sleep(2.5)
            for _ in range(10):
                harness.publish_bool("/test/slow_sensor", False)
                time.sleep(0.1)

            assert harness.wait_for_gate_level_gte(1, timeout=3.0)

            # Resume both → gate recovers
            for _ in range(20):
                harness.publish_bool("/test/fast_estop", False)
                harness.publish_bool("/test/slow_sensor", False)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestDebounceIntegration:
    """debounce_sec prevents premature gate opening when condition flaps."""

    def test_debounce_filtering(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              sensors:
                default_type: topic_condition
                conditions:
                  sensor:
                    topic: /test/sensor
                    condition: "not msg.data"
                    timeout_sec: 2.0
                    debounce_sec: 2.0
        """)
        harness = GateTestHarness(config)
        try:
            # Publish passing value — debounce starts, gate still blocked
            for _ in range(10):
                harness.publish_bool("/test/sensor", False)
                time.sleep(0.05)

            assert harness.wait_for_gate_level_gte(
                1, timeout=2.0
            )  # debounce not elapsed

            # Flap: briefly publish failing value → resets debounce
            for _ in range(5):
                harness.publish_bool("/test/sensor", True)
                time.sleep(0.05)

            # Resume passing — new debounce timer starts
            for _ in range(50):
                harness.publish_bool("/test/sensor", False)
                time.sleep(0.05)

            # After ~2.5s of stable passing, debounce should clear
            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestEscalationLevelsIntegration:
    """Verify gate correctly outputs level 0, 1, and 2 depending on
    condition escalation values."""

    def test_escalation_levels(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              safety:
                default_type: topic_condition
                conditions:
                  warn_sensor:
                    topic: /test/warn
                    condition: "not msg.data"
                    timeout_sec: 0.5
                    escalation: 1
                  critical_sensor:
                    topic: /test/critical
                    condition: "not msg.data"
                    timeout_sec: 0.5
                    escalation: 2
        """)
        harness = GateTestHarness(config)
        try:
            # Both passing → level 0
            for _ in range(15):
                harness.publish_bool("/test/warn", False)
                harness.publish_bool("/test/critical", False)
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Stop warn_sensor (timeout) → level 1 (BLOCK_START)
            for _ in range(15):
                harness.publish_bool("/test/critical", False)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(1, timeout=3.0)

            # Stop critical_sensor too → level 2 (HARD_STOP, max wins)
            time.sleep(2.0)
            assert harness.wait_for_gate_level(2, timeout=5.0)

            # Recover critical only, warn still failing → level 1
            for _ in range(15):
                harness.publish_bool("/test/critical", False)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(1, timeout=5.0)

            # Recover both → level 0
            for _ in range(20):
                harness.publish_bool("/test/warn", False)
                harness.publish_bool("/test/critical", False)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestTopicRateCheckingIntegration:
    """Verify min_rate_hz with controlled publish rates, and that
    rate_escalation differs from main escalation on freshness timeout."""

    def test_rate_and_freshness_escalation(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              camera:
                default_type: topic_condition
                conditions:
                  feed:
                    topic: /test/camera_feed
                    timeout_sec: 2.0
                    min_rate_hz: 10.0
                    rate_window_sec: 2.0
                    rate_escalation: 1
                    escalation: 2
        """)
        harness = GateTestHarness(config)
        try:
            # Publish at ~20 Hz → gate opens
            for _ in range(40):
                harness.publish_float64("/test/camera_feed", 1.0)
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Reduce to ~2 Hz → rate fails, level 1 (rate_escalation)
            for _ in range(8):
                harness.publish_float64("/test/camera_feed", 1.0)
                time.sleep(0.5)

            assert harness.wait_for_gate_level(1, timeout=5.0)

            # Increase back to ~20 Hz → recovers
            for _ in range(40):
                harness.publish_float64("/test/camera_feed", 1.0)
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=5.0)

            # Stop completely → freshness timeout, level 2 (main escalation)
            time.sleep(5.0)
            assert harness.wait_for_gate_level(2, timeout=5.0)
        finally:
            harness.shutdown()


class TestBoolConditionTrueExpression:
    """Verify topic_condition with 'msg.data == True' — the inverse of
    'not msg.data'. Passes when Bool.data is True, fails when False."""

    def test_bool_expected_true(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              sensors:
                default_type: topic_condition
                conditions:
                  enabled_flag:
                    topic: /test/enabled
                    condition: "msg.data == True"
                    timeout_sec: 2.0
        """)
        harness = GateTestHarness(config)
        try:
            # Publish True → gate opens
            for _ in range(15):
                harness.publish_bool("/test/enabled", True)
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Publish False → gate blocks
            for _ in range(10):
                harness.publish_bool("/test/enabled", False)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(2, timeout=3.0)

            # Publish True again → gate recovers
            for _ in range(20):
                harness.publish_bool("/test/enabled", True)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestDurationLimitIntegration:
    """Verify topic_condition as a duration limiter. No-message = PASS
    (inactive-safe with timeout_sec=-1). Value >= limit triggers HARD_STOP."""

    def test_duration_limit(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              limits:
                default_type: topic_condition
                conditions:
                  subtask_limit:
                    topic: /test/subtask_elapsed
                    condition: "msg.data < 60.0"
                    timeout_sec: -1.0
        """)
        harness = GateTestHarness(config)
        try:
            # No messages → PASS (inactive-safe), gate opens
            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Publish under threshold → still open
            for _ in range(10):
                harness.publish_float64("/test/subtask_elapsed", 30.0)
                time.sleep(0.05)

            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Publish at threshold → HARD_STOP
            for _ in range(15):
                harness.publish_float64("/test/subtask_elapsed", 60.0)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(2, timeout=5.0)

            # Publish under threshold again → recovers
            for _ in range(20):
                harness.publish_float64("/test/subtask_elapsed", 0.0)
                time.sleep(0.1)

            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestSingleShotCondition:
    """Verify single_shot: true — requires one message, then always passes
    regardless of freshness timeout."""

    def test_single_shot(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              latched:
                default_type: topic_condition
                conditions:
                  description:
                    topic: /test/robot_description
                    single_shot: true
                    timeout_sec: 1.0
        """)
        harness = GateTestHarness(config)
        try:
            # No message → gate blocked
            assert harness.wait_for_gate_level(2, timeout=3.0)

            # Publish one latched message — waits for subscriber match first.
            # single_shot + latch: transient-local on both sides.
            msg = String()
            msg.data = "robot_v1"
            harness.publish_latched("/test/robot_description", String, msg)

            # single_shot is settle-exempt → gate opens without waiting
            # for the settle period to complete.
            assert harness.wait_for_gate_level(0, timeout=5.0)

            # Stop publishing, wait well past timeout_sec (1.0s)
            time.sleep(3.0)

            # Gate should STILL be open (single-shot satisfied)
            level = harness.get_latest_gate_level()
            assert level == 0
        finally:
            harness.shutdown()


class TestInvalidateServiceLatched:
    """Invalidate service tears down subscriptions and re-subscribes.
    Latched single_shot topic: gate opens → invalidate → gate blocks →
    re-publish latched → gate opens again."""

    def test_invalidate_resubscribes_latched(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              latched:
                default_type: topic_condition
                conditions:
                  description:
                    topic: /test/robot_description
                    single_shot: true
                    timeout_sec: 1.0
        """)
        harness = GateTestHarness(config)
        try:
            # Publish latched message → gate opens
            msg = String()
            msg.data = "robot_v1"
            harness.publish_latched("/test/robot_description", String, msg)
            assert harness.wait_for_gate_level(0, timeout=5.0)

            # Invalidate → subscriptions destroyed, checker reset
            result = harness.call_invalidate()
            assert result.success

            # Gate blocks (no message, checker reset)
            assert harness.wait_for_gate_level_gte(1, timeout=5.0)

            # Re-publish latched message → gate opens again
            msg.data = "robot_v2"
            harness.publish_latched("/test/robot_description", String, msg)
            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()


class TestInvalidateServicePeriodic:
    """Invalidate service on a periodic topic: gate opens → invalidate →
    gate blocks (freshness lost) → resume publishing → gate recovers."""

    def test_invalidate_resubscribes_periodic(self):
        config = textwrap.dedent("""\
            eval_rate: 10.0
            settle_sec: 0.5
            recovery_sec: 0.5
            groups:
              safety:
                default_type: topic_condition
                conditions:
                  estop:
                    topic: /test/estop
                    condition: "not msg.data"
                    timeout_sec: 0.5
        """)
        harness = GateTestHarness(config)
        try:
            # Publish periodically → gate opens
            for _ in range(20):
                harness.publish_bool("/test/estop", False)
                time.sleep(0.05)
            assert harness.wait_for_gate_level(0, timeout=3.0)

            # Invalidate → subscriptions destroyed
            result = harness.call_invalidate()
            assert result.success

            # Gate blocks (checker reset, no fresh messages)
            assert harness.wait_for_gate_level_gte(1, timeout=5.0)

            # Resume publishing → gate recovers
            for _ in range(30):
                harness.publish_bool("/test/estop", False)
                time.sleep(0.1)
            assert harness.wait_for_gate_level(0, timeout=5.0)
        finally:
            harness.shutdown()
