"""Scenario tests for RecordingGateNode.

Each test exercises a realistic multi-step workflow through the gate
evaluation pipeline, unlike the unit tests which test individual checkers
or single evaluation calls in isolation.

All ROS 2 dependencies are mocked via ``conftest.mock_rclpy``.
"""

import time
import textwrap
from collections import deque
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def gate_module(mock_rclpy):
    """Import recording_gate_node with mocked ROS 2."""
    import sys
    import types

    diag_msgs = types.ModuleType("diagnostic_msgs")
    diag_msgs_msg = types.ModuleType("diagnostic_msgs.msg")

    class _DiagnosticArray:
        def __init__(self):
            self.header = MagicMock()
            self.status = []

    class _DiagnosticStatus:
        OK = 0
        WARN = 1
        ERROR = 2
        STALE = 3

        def __init__(self):
            self.level = 0
            self.name = ""
            self.message = ""
            self.hardware_id = ""
            self.values = []

    class _KeyValue:
        def __init__(self, key="", value=""):
            self.key = key
            self.value = value

    diag_msgs_msg.DiagnosticArray = _DiagnosticArray
    diag_msgs_msg.DiagnosticStatus = _DiagnosticStatus
    diag_msgs_msg.KeyValue = _KeyValue

    sys.modules["diagnostic_msgs"] = diag_msgs
    sys.modules["diagnostic_msgs.msg"] = diag_msgs_msg

    for key in list(sys.modules):
        if key.startswith("galaxis_core.recording_gate"):
            del sys.modules[key]

    import galaxis_core.recording_gate_node as mod

    return mod


@pytest.fixture()
def bool_cls(gate_module):
    """Return the Bool class visible to the gate module."""
    import sys

    return sys.modules["std_msgs.msg"].Bool


@pytest.fixture()
def float64_cls(gate_module):
    """Return the Float64 class visible to the gate module."""
    import sys

    return sys.modules["std_msgs.msg"].Float64


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_gate(gate_module, checkers, *, settle_timeout=0.0, re_settle=0.0):
    """Build a RecordingGateNode-like object with manual checkers."""
    from test.conftest import make_test_gate

    return make_test_gate(
        gate_module, checkers, settle_sec=settle_timeout, recovery_sec=re_settle
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


class TestScenarioStartupAllFailThenGradualRecovery:
    """3 mock checkers all failing. Checkers pass one by one.
    Only when all pass does gate reach 0."""

    def test_scenario_startup_all_fail_then_gradual_recovery(self, gate_module):
        c1 = MagicMock()
        c2 = MagicMock()
        c3 = MagicMock()
        for c in (c1, c2, c3):
            c.evaluate.return_value = gate_module.ConditionResult(
                "c", False, "bad", escalation=2
            )

        gate = _make_gate(gate_module, [c1, c2, c3], settle_timeout=0.0)

        # Step 1: All fail → level 2
        gate._evaluate()
        # settle_timeout=0 forces settled despite failures
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 2

        # Step 2: c1 recovers, c2 and c3 still fail → still level 2
        c1.evaluate.return_value = gate_module.ConditionResult(
            "c1", True, "ok", escalation=0
        )
        gate._evaluate()
        assert gate._gate_level == 2

        # Step 3: c2 also recovers, c3 still fails → still level 2
        c2.evaluate.return_value = gate_module.ConditionResult(
            "c2", True, "ok", escalation=0
        )
        gate._evaluate()
        assert gate._gate_level == 2

        # Step 4: All recover → level 0
        c3.evaluate.return_value = gate_module.ConditionResult(
            "c3", True, "ok", escalation=0
        )
        # Push settle start back so re-settle elapses
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 0


class TestScenarioStartupSettleDelay:
    """1 passing checker, settle_timeout=10. First evaluate → not settled.
    Push _settle_start_time back 11s. Second evaluate → settled, level 0."""

    def test_scenario_startup_settle_delay(self, gate_module):
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )

        gate = _make_gate(gate_module, [checker], settle_timeout=10.0)

        # Step 1: First evaluate — not settled yet (elapsed ≈ 0 < 10s)
        gate._evaluate()
        assert gate._groups[0].settled is False
        assert gate._gate_level >= 1  # minimum BLOCK_START during settle

        # Step 2: Push settle start back 11s
        gate._groups[0].settle_start_time = time.monotonic() - 11.0

        # Step 3: Second evaluate — settled, level 0
        gate._evaluate()
        assert gate._groups[0].settled is True
        assert gate._gate_level == 0


class TestScenarioConditionFailureMidRecording:
    """Gate open (level 0). Checker fails → re-settle triggered.
    Checker recovers but re-settle not elapsed → still blocked.
    Push time → evaluate → settled, level 0."""

    def test_scenario_condition_failure_mid_recording(self, gate_module):
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )

        gate = _make_gate(gate_module, [checker], settle_timeout=0.0, re_settle=5.0)

        # Step 1: Initial settle → gate open
        gate._evaluate()
        assert gate._gate_level == 0
        assert gate._groups[0].settled is True

        # Step 2: Condition fails → re-settle triggered
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", False, "sensor offline", escalation=1
        )
        gate._evaluate()
        assert gate._gate_level >= 1
        assert gate._groups[0].settled is False
        assert gate._groups[0].settle_is_initial is False

        # Step 3: Condition recovers but re-settle (5s) not elapsed
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )
        gate._evaluate()
        assert gate._groups[0].settled is False
        assert gate._gate_level >= 1  # still blocked during re-settle

        # Step 4: Push time past re-settle timeout
        gate._groups[0].settle_start_time = time.monotonic() - 6.0
        gate._evaluate()
        assert gate._groups[0].settled is True
        assert gate._gate_level == 0


class TestScenarioMultipleEscalationLevelsInteraction:
    """3 checkers (esc=0, esc=1, esc=2). Fail/recover them in sequence
    and verify gate level follows the max escalation."""

    def test_scenario_multiple_escalation_levels_interaction(self, gate_module):
        c0 = MagicMock()  # escalation=0
        c1 = MagicMock()  # escalation=1
        c2 = MagicMock()  # escalation=2

        # All start passing
        for c in (c0, c1, c2):
            c.evaluate.return_value = gate_module.ConditionResult(
                "c", True, "ok", escalation=0
            )

        gate = _make_gate(gate_module, [c0, c1, c2], settle_timeout=0.0, re_settle=0.0)

        # Step 1: All pass → level 0
        gate._evaluate()
        assert gate._gate_level == 0

        # Step 2: esc=0 checker fails → still level 0 (escalation=0 doesn't block)
        c0.evaluate.return_value = gate_module.ConditionResult(
            "c0", False, "bad", escalation=0
        )
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 0

        # Step 3: esc=1 checker fails → level 1
        c1.evaluate.return_value = gate_module.ConditionResult(
            "c1", False, "bad", escalation=1
        )
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 1

        # Step 4: esc=2 checker fails → level 2
        c2.evaluate.return_value = gate_module.ConditionResult(
            "c2", False, "bad", escalation=2
        )
        gate._evaluate()
        assert gate._gate_level == 2

        # Step 5: esc=2 recovers → level 1 (esc=1 still failing)
        c2.evaluate.return_value = gate_module.ConditionResult(
            "c2", True, "ok", escalation=0
        )
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 1

        # Step 6: All recover → level 0
        c0.evaluate.return_value = gate_module.ConditionResult(
            "c0", True, "ok", escalation=0
        )
        c1.evaluate.return_value = gate_module.ConditionResult(
            "c1", True, "ok", escalation=0
        )
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 0


class TestScenarioRealCheckersStartup:
    """Use actual TopicConditionChecker instances (not mocks).
    Both fail initially. Send messages to make them pass, gate opens."""

    def test_scenario_real_checkers_startup(self, gate_module, bool_cls):
        # Create real checkers
        health_checker = gate_module.TopicConditionChecker(
            "estop",
            {"topic": "/estop", "condition": "not msg.data", "timeout_sec": 5.0},
        )
        topic_checker = gate_module.TopicConditionChecker(
            "heartbeat", {"topic": "/joint_states", "timeout_sec": 5.0}
        )

        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/estop", ["std_msgs/msg/Bool"]),
            ("/joint_states", ["sensor_msgs/msg/JointState"]),
        ]
        node.get_shared_subscription = MagicMock()
        health_checker.setup(node)
        topic_checker.setup(node)

        gate = _make_gate(
            gate_module, [health_checker, topic_checker], settle_timeout=0.0
        )

        # Step 1: Both fail initially → gate blocked
        gate._evaluate()
        assert gate._gate_level >= 1

        # Step 2: Send Bool message → health passes, topic_checker still fails
        msg = bool_cls()
        msg.data = False
        health_checker._cb(msg)

        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level >= 1  # topic_checker still failing

        # Step 3: Send message → both pass, gate opens
        topic_checker._cb(MagicMock())

        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 0


class TestScenarioDurationLimitTriggersStop:
    """TopicConditionChecker (condition="msg.data < 300.0", esc=2) + passing
    TopicConditionChecker.  Send Float64 values to trigger and then clear."""

    def test_scenario_duration_limit_triggers_stop(
        self, gate_module, bool_cls, float64_cls
    ):
        health_checker = gate_module.TopicConditionChecker(
            "estop",
            {
                "topic": "/estop",
                "condition": "not msg.data",
                "timeout_sec": 5.0,
                "escalation": 1,
            },
        )
        duration_checker = gate_module.TopicConditionChecker(
            "duration",
            {
                "topic": "/elapsed",
                "condition": "msg.data < 300.0",
                "timeout_sec": -1.0,
                "escalation": 2,
            },
        )

        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/estop", ["std_msgs/msg/Bool"]),
            ("/elapsed", ["std_msgs/msg/Float64"]),
        ]
        node.get_shared_subscription = MagicMock()
        health_checker.setup(node)
        duration_checker.setup(node)

        gate = _make_gate(
            gate_module,
            [health_checker, duration_checker],
            settle_timeout=0.0,
            re_settle=0.0,
        )

        # Step 1: Health passes, duration has no message (timeout_sec=-1 → pass) → level 0
        msg_bool = bool_cls()
        msg_bool.data = False
        health_checker._cb(msg_bool)

        gate._evaluate()
        assert gate._gate_level == 0

        # Step 2: Send Float64=100 → condition "msg.data < 300.0" passes, still level 0
        msg_f = float64_cls()
        msg_f.data = 100.0
        duration_checker._cb(msg_f)

        gate._evaluate()
        assert gate._gate_level == 0

        # Step 3: Send Float64=300 → condition fails, level 2 (HARD_STOP)
        msg_f2 = float64_cls()
        msg_f2.data = 300.0
        duration_checker._cb(msg_f2)

        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 2

        # Step 4: Send Float64=0 → condition passes, recovers, level 0
        msg_f3 = float64_cls()
        msg_f3.data = 0.0
        duration_checker._cb(msg_f3)

        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 0


class TestScenarioSettleTimeoutExpiresWithFailures:
    """1 failing checker (esc=1), settle_timeout=0.1. Evaluate → not settled.
    Push time past timeout. Evaluate → settled=True despite failure, level=1."""

    def test_scenario_settle_timeout_expires_with_failures(self, gate_module):
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", False, "sensor down", escalation=1
        )

        gate = _make_gate(gate_module, [checker], settle_timeout=0.1)

        # Step 1: First evaluate — not settled, gate elevated
        gate._evaluate()
        assert gate._groups[0].settled is False
        assert gate._gate_level >= 1

        # Step 2: Push settle start past timeout
        gate._groups[0].settle_start_time = time.monotonic() - 1.0

        # Step 3: Evaluate again — timeout expired with failure still active
        gate._evaluate()
        assert gate._groups[0].settled is True
        assert gate._gate_level == 1

        # Step 4: Verify warning was logged
        gate.get_logger().warning.assert_called()
        warning_args = str(gate.get_logger().warning.call_args)
        assert "sensor down" in warning_args or "Settle timeout" in warning_args


class TestScenarioConfigDrivenGateConstruction:
    """Write YAML config to tmp_path with 2 enabled conditions.
    Load via load_gate_config. Verify correct checker types created."""

    def test_scenario_config_driven_gate_construction(self, gate_module, tmp_path):
        config_path = tmp_path / "gate_config.yaml"
        config_path.write_text(
            textwrap.dedent("""\
            eval_rate: 2.0
            settle_timeout_sec: 10.0
            re_settle_timeout_sec: 3.0
            conditions:
              estop:
                type: topic_condition
                enabled: true
                topic: /runstop_button
                condition: "not msg.data"
                timeout_sec: 5.0
                escalation: 2
              heartbeat:
                type: topic_condition
                enabled: true
                topic: /joint_states
                timeout_sec: 3.0
                escalation: 1
              disabled_diag:
                type: diagnostics_error_rate
                enabled: false
                topic: /diagnostics_agg
                max_errors: 3
        """)
        )

        # Step 1: Load config
        cfg = gate_module.load_gate_config(str(config_path))
        assert cfg["eval_rate"] == 2.0
        assert cfg["settle_timeout_sec"] == 10.0
        assert cfg["re_settle_timeout_sec"] == 3.0

        # Step 2: Verify enabled/disabled conditions
        conditions = cfg["conditions"]
        enabled = {k: v for k, v in conditions.items() if v.get("enabled")}
        disabled = {k: v for k, v in conditions.items() if not v.get("enabled")}
        assert len(enabled) == 2
        assert "estop" in enabled
        assert "heartbeat" in enabled
        assert "disabled_diag" in disabled

        # Step 3: Instantiate the correct checker types
        checkers = []
        for name, cond_cfg in enabled.items():
            ctype = cond_cfg["type"]
            cls = gate_module.CONDITION_TYPES[ctype]
            checker = cls(name, cond_cfg)
            checkers.append(checker)

        assert len(checkers) == 2
        checker_types = {type(c).__name__ for c in checkers}
        assert "TopicConditionChecker" in checker_types

        # Step 4: Verify escalation levels from config
        by_name = {c.name: c for c in checkers}
        assert by_name["estop"].escalation == 2
        assert by_name["heartbeat"].escalation == 1


# ---------------------------------------------------------------------------
# Multi-group helper
# ---------------------------------------------------------------------------


def _make_multi_group_gate(gate_module, group_specs):
    """Build a gate with multiple GroupState objects.

    group_specs: list of dicts with keys:
        name, checkers, settle_sec (default 0.0), recovery_sec (default 0.0)
    """
    node = MagicMock()
    node.get_logger.return_value = MagicMock()

    gate = gate_module.RecordingGateNode.__new__(gate_module.RecordingGateNode)
    gate._groups = []
    gate._checkers = []
    gate._gate_level = gate_module.EscalationLevel.HARD_STOP
    gate._pub = MagicMock()
    gate._diag_pub = MagicMock()
    gate.get_logger = node.get_logger
    gate.get_clock = MagicMock()

    now = time.monotonic()
    for spec in group_specs:
        group = gate_module.GroupState(
            name=spec["name"],
            checkers=spec["checkers"],
            settle_sec=spec.get("settle_sec", 0.0),
            recovery_sec=spec.get("recovery_sec", 0.0),
            settle_start_time=now,
        )
        gate._groups.append(group)
        gate._checkers.extend(spec["checkers"])

    return gate


# ---------------------------------------------------------------------------
# V2 Scenarios
# ---------------------------------------------------------------------------


class TestScenarioMultiGroupIndependentSettling:
    """Two groups (safety, health) with different settle_sec.
    Safety settles immediately, health takes 10s. Gate stays blocked
    until both groups have settled."""

    def test_scenario_multi_group_independent_settling(self, gate_module):
        c_safety = MagicMock()
        c_safety.evaluate.return_value = gate_module.ConditionResult(
            "estop", True, "ok", escalation=0
        )
        c_health = MagicMock()
        c_health.evaluate.return_value = gate_module.ConditionResult(
            "joints", True, "ok", escalation=0
        )

        gate = _make_multi_group_gate(
            gate_module,
            [
                {"name": "safety", "checkers": [c_safety], "settle_sec": 0.0},
                {"name": "health", "checkers": [c_health], "settle_sec": 10.0},
            ],
        )

        # Step 1: Evaluate — safety settles (settle=0), health does not
        gate._evaluate()
        assert gate._groups[0].settled is True
        assert gate._groups[1].settled is False
        assert gate._gate_level >= 1  # health unsettled forces BLOCK_START

        # Step 2: Push health settle back 11s
        gate._groups[1].settle_start_time = time.monotonic() - 11.0
        gate._evaluate()
        assert gate._groups[1].settled is True
        assert gate._gate_level == 0


class TestScenarioMultiGroupMaxEscalation:
    """Three groups with different escalation levels.
    Gate level equals the max across all groups."""

    def test_scenario_multi_group_max_escalation(self, gate_module):
        c_info = MagicMock()
        c_info.evaluate.return_value = gate_module.ConditionResult(
            "info", False, "bad", escalation=0
        )
        c_warn = MagicMock()
        c_warn.evaluate.return_value = gate_module.ConditionResult(
            "warn", False, "bad", escalation=1
        )
        c_clean = MagicMock()
        c_clean.evaluate.return_value = gate_module.ConditionResult(
            "clean", True, "ok", escalation=0
        )

        gate = _make_multi_group_gate(
            gate_module,
            [
                {"name": "info", "checkers": [c_info]},
                {"name": "warn", "checkers": [c_warn]},
                {"name": "clean", "checkers": [c_clean]},
            ],
        )

        # Step 1: info esc=0 + warn esc=1 → gate=1
        gate._evaluate()
        assert gate._gate_level == 1

        # Step 2: Recover warn → gate=0 (info esc=0 doesn't block)
        c_warn.evaluate.return_value = gate_module.ConditionResult(
            "warn", True, "ok", escalation=0
        )
        gate._groups[1].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 0

        # Step 3: Fail clean with esc=2 → gate=2
        c_clean.evaluate.return_value = gate_module.ConditionResult(
            "clean", False, "bad", escalation=2
        )
        gate._groups[2].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 2


class TestScenarioTopicConditionCheckerRateChecking:
    """TopicConditionChecker with min_rate_hz=10 and rate_escalation=1.
    Too few messages → fails with rate_escalation. Enough messages → passes.
    Old timestamps expire → fails again."""

    def test_scenario_rate_checking(self, gate_module):
        checker = gate_module.TopicConditionChecker(
            "camera",
            {
                "topic": "/camera",
                "timeout_sec": 5.0,
                "min_rate_hz": 10.0,
                "rate_window_sec": 2.0,
                "escalation": 2,
                "rate_escalation": 1,
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/camera", ["sensor_msgs/msg/Image"]),
        ]
        checker.setup(node)

        # Step 1: Send 1 message → rate too low
        checker._cb(MagicMock())
        result = checker.evaluate()
        assert result.passed is False
        assert result.escalation == 1  # rate_escalation, not main escalation=2
        assert "rate" in result.reason

        # Step 2: Send enough messages to exceed 10 Hz over 2s window
        for _ in range(24):
            checker._cb(MagicMock())
        result = checker.evaluate()
        assert result.passed is True

        # Step 3: Push all timestamps back beyond the window
        old = time.monotonic() - 3.0
        checker._msg_times = deque([old] * len(checker._msg_times))
        # Send 1 fresh message to keep freshness OK
        checker._cb(MagicMock())
        result = checker.evaluate()
        assert result.passed is False
        assert result.escalation == 1


class TestScenarioTopicConditionCheckerAbsenceGracePeriod:
    """TopicConditionChecker with absence_timeout_sec=5.0.
    Topic not advertised — within grace period, then past it.
    Topic appears + message → passes."""

    def test_scenario_absence_grace_period(self, gate_module):
        checker = gate_module.TopicConditionChecker(
            "sensor",
            {
                "topic": "/sensor",
                "timeout_sec": 5.0,
                "escalation": 2,
                "absence_timeout_sec": 5.0,
                "absence_escalation": 1,
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = []
        checker.setup(node)

        # Step 1: Topic absent — fails with absence_escalation
        result = checker.evaluate()
        assert result.passed is False
        assert result.escalation == 1
        assert "not yet advertised" in result.reason

        # Step 2: Push past absence timeout
        checker._first_absent = time.monotonic() - 6.0
        result = checker.evaluate()
        assert result.passed is False
        assert result.escalation == 1
        assert "not advertised" in result.reason

        # Step 3: Topic appears, send message → passes
        node.get_topic_names_and_types.return_value = [
            ("/sensor", ["std_msgs/msg/Float64"]),
        ]
        checker.evaluate()  # triggers _try_subscribe
        checker._cb(MagicMock())
        result = checker.evaluate()
        assert result.passed is True
        assert checker._first_absent is None


class TestScenarioTopicConditionCheckerContentExpression:
    """TopicConditionChecker with condition='msg.data < 100'.
    In-range passes, out-of-range fails. Freshness checked before content."""

    def test_scenario_content_expression(self, gate_module):
        checker = gate_module.TopicConditionChecker(
            "temp",
            {
                "topic": "/temp",
                "timeout_sec": 5.0,
                "condition": "msg.data < 100",
                "escalation": 2,
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/temp", ["std_msgs/msg/Float64"]),
        ]
        checker.setup(node)

        # Step 1: msg.data=50 → passes
        msg = MagicMock()
        msg.data = 50.0
        checker._cb(msg)
        result = checker.evaluate()
        assert result.passed is True

        # Step 2: msg.data=150 → fails (condition)
        msg2 = MagicMock()
        msg2.data = 150.0
        checker._cb(msg2)
        result = checker.evaluate()
        assert result.passed is False
        assert "condition failed" in result.reason
        assert result.escalation == 2

        # Step 3: msg.data=99.9 → passes again
        msg3 = MagicMock()
        msg3.data = 99.9
        checker._cb(msg3)
        result = checker.evaluate()
        assert result.passed is True

        # Step 4: Force freshness timeout → fails with timeout (before content)
        checker._last_stamp = time.monotonic() - 10.0
        result = checker.evaluate()
        assert result.passed is False
        assert "timeout" in result.reason.lower() or "age" in result.reason.lower()


class TestScenarioDebounceWithGateIntegration:
    """DebouncedChecker (debounce_sec=3) in a gate. Checker passes but
    debounce prevents opening. Flap resets timer. Stable passing + elapsed
    debounce → gate opens."""

    def test_scenario_debounce_gate_integration(self, gate_module):
        inner = MagicMock()
        inner.name = "sensor"
        inner.escalation = 2
        inner.config = {}
        inner.evaluate.return_value = gate_module.ConditionResult(
            "sensor", False, "down", escalation=2
        )

        debounced = gate_module.DebouncedChecker(inner, debounce_sec=3.0)
        gate = _make_gate(gate_module, [debounced], settle_timeout=0.0)

        # Step 1: Inner passes → debounce starts but not satisfied
        inner.evaluate.return_value = gate_module.ConditionResult(
            "sensor", True, "ok", escalation=0
        )
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert debounced._passing_since is not None
        assert gate._gate_level >= 1

        # Step 2: Inner fails (flap) → debounce resets
        inner.evaluate.return_value = gate_module.ConditionResult(
            "sensor", False, "flap", escalation=2
        )
        gate._evaluate()
        assert debounced._passing_since is None
        assert gate._gate_level >= 1

        # Step 3: Inner passes again → new debounce timer
        inner.evaluate.return_value = gate_module.ConditionResult(
            "sensor", True, "ok", escalation=0
        )
        gate._evaluate()
        assert debounced._passing_since is not None

        # Step 4: Push debounce timer past 3s → gate opens
        debounced._passing_since = time.monotonic() - 4.0
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 0


class TestScenarioDiagnosticsErrorRateSlidingWindow:
    """DiagnosticsErrorRateChecker: errors accumulate → threshold crossed.
    Old errors expire → gate re-opens. New burst re-blocks."""

    def test_scenario_diagnostics_sliding_window(self, gate_module):
        import sys

        DiagStatus = sys.modules["diagnostic_msgs.msg"].DiagnosticStatus
        DiagArray = sys.modules["diagnostic_msgs.msg"].DiagnosticArray

        checker = gate_module.DiagnosticsErrorRateChecker(
            "diag",
            {
                "topic": "/diagnostics_agg",
                "max_errors": 3,
                "window_sec": 2.0,
                "escalation": 2,
            },
        )
        node = MagicMock()
        checker.setup(node)

        gate = _make_gate(gate_module, [checker], settle_timeout=0.0)

        # Step 1: Send clean diagnostic → gate open
        clean_msg = DiagArray()
        status = DiagStatus()
        status.level = DiagStatus.OK
        status.name = "ok"
        clean_msg.status = [status]
        checker._cb(clean_msg)

        gate._evaluate()
        assert gate._gate_level == 0

        # Step 2: Send 3 ERROR diagnostics → gate blocked
        for _ in range(3):
            err_msg = DiagArray()
            err_status = DiagStatus()
            err_status.level = DiagStatus.ERROR
            err_status.name = "motor_fault"
            err_msg.status = [err_status]
            checker._cb(err_msg)

        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 2

        # Step 3: Push error timestamps past window → gate re-opens
        old = time.monotonic() - 3.0
        checker._errors = deque([old] * len(checker._errors))
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 0

        # Step 4: New burst of errors → gate re-blocks
        for _ in range(4):
            err_msg = DiagArray()
            err_status = DiagStatus()
            err_status.level = DiagStatus.ERROR
            err_status.name = "motor_fault"
            err_msg.status = [err_status]
            checker._cb(err_msg)

        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 2


class TestScenarioConfigDrivenGroupedConstruction:
    """Load a v2 grouped YAML config. Verify correct groups, checker types,
    default_type inheritance, disabled conditions/groups excluded, and
    escalation overrides."""

    def test_scenario_config_driven_grouped_construction(self, gate_module, tmp_path):
        config_path = tmp_path / "gate_v2.yaml"
        config_path.write_text(
            textwrap.dedent("""\
            eval_rate: 2.0
            settle_sec: 20.0
            recovery_sec: 5.0
            default_escalation: 2
            groups:
              safety:
                settle_sec: 5.0
                recovery_sec: 2.0
                default_type: topic_condition
                conditions:
                  estop:
                    topic: /runstop_button
                    condition: "not msg.data"
                    timeout_sec: 5.0
                  disabled_check:
                    enabled: false
                    topic: /unused
              diagnostics:
                default_type: diagnostics_error_rate
                conditions:
                  errors_warn:
                    topic: /diagnostics_agg
                    max_errors: 3
                    window_sec: 10.0
                    escalation: 1
              disabled_group:
                enabled: false
                conditions:
                  never_checked:
                    type: topic_condition
                    topic: /nope
        """)
        )

        # Step 1: Load config
        cfg = gate_module.load_gate_config(str(config_path))
        assert cfg["eval_rate"] == 2.0
        assert "groups" in cfg

        groups_cfg = cfg["groups"]

        # Step 2: Verify group-level settings
        assert groups_cfg["safety"]["settle_sec"] == 5.0
        assert groups_cfg["safety"]["recovery_sec"] == 2.0
        assert groups_cfg["disabled_group"]["enabled"] is False

        # Step 3: Iterate enabled groups and instantiate checkers
        global_esc = cfg.get("default_escalation", 2)
        built_groups = {}
        for gname, gcfg in groups_cfg.items():
            if not gcfg.get("enabled", True):
                continue
            default_type = gcfg.get("default_type", "")
            default_esc = gcfg.get("default_escalation", global_esc)
            checkers = []
            for cname, ccfg in gcfg.get("conditions", {}).items():
                if not ccfg.get("enabled", True):
                    continue
                ctype = ccfg.get("type", default_type)
                cls = gate_module.CONDITION_TYPES[ctype]
                effective = {**ccfg, "escalation": ccfg.get("escalation", default_esc)}
                checkers.append((cname, cls(cname, effective)))
            built_groups[gname] = checkers

        # Step 4: Verify results
        # disabled_group excluded
        assert "disabled_group" not in built_groups
        # safety: 1 enabled (estop), disabled_check excluded
        assert len(built_groups["safety"]) == 1
        assert built_groups["safety"][0][0] == "estop"
        assert type(built_groups["safety"][0][1]).__name__ == "TopicConditionChecker"
        assert built_groups["safety"][0][1].escalation == 2  # inherits global
        # diagnostics: 1 condition with esc override
        assert len(built_groups["diagnostics"]) == 1
        assert built_groups["diagnostics"][0][0] == "errors_warn"
        assert (
            type(built_groups["diagnostics"][0][1]).__name__
            == "DiagnosticsErrorRateChecker"
        )
        assert built_groups["diagnostics"][0][1].escalation == 1  # overridden


class TestScenarioReSettleWithDifferentGroupTimings:
    """Two groups with different recovery_sec (1s vs 10s). Gate opens,
    both fail, both recover. Fast group re-settles first; gate stays
    blocked until slow group also re-settles."""

    def test_scenario_resettle_different_group_timings(self, gate_module):
        c_fast = MagicMock()
        c_fast.evaluate.return_value = gate_module.ConditionResult(
            "fast", True, "ok", escalation=0
        )
        c_slow = MagicMock()
        c_slow.evaluate.return_value = gate_module.ConditionResult(
            "slow", True, "ok", escalation=0
        )

        gate = _make_multi_group_gate(
            gate_module,
            [
                {"name": "fast", "checkers": [c_fast], "recovery_sec": 1.0},
                {"name": "slow", "checkers": [c_slow], "recovery_sec": 10.0},
            ],
        )

        # Step 1: Both pass → gate opens
        gate._evaluate()
        assert gate._gate_level == 0

        # Step 2: Both fail → re-settle triggered
        c_fast.evaluate.return_value = gate_module.ConditionResult(
            "fast", False, "bad", escalation=1
        )
        c_slow.evaluate.return_value = gate_module.ConditionResult(
            "slow", False, "bad", escalation=1
        )
        gate._evaluate()
        assert gate._gate_level >= 1
        assert gate._groups[0].settled is False
        assert gate._groups[1].settled is False
        assert gate._groups[0].settle_is_initial is False
        assert gate._groups[1].settle_is_initial is False

        # Step 3: Both recover; push fast 2s back (past recovery=1s)
        c_fast.evaluate.return_value = gate_module.ConditionResult(
            "fast", True, "ok", escalation=0
        )
        c_slow.evaluate.return_value = gate_module.ConditionResult(
            "slow", True, "ok", escalation=0
        )
        gate._groups[0].settle_start_time = time.monotonic() - 2.0
        gate._evaluate()
        assert gate._groups[0].settled is True
        assert gate._groups[1].settled is False
        assert gate._gate_level >= 1  # slow still unsettled

        # Step 4: Push slow 11s back → both settled, gate opens
        gate._groups[1].settle_start_time = time.monotonic() - 11.0
        gate._evaluate()
        assert gate._groups[1].settled is True
        assert gate._gate_level == 0


class TestScenarioTopicConditionCheckerFullLifecycle:
    """TopicConditionChecker with all facets configured. Walk through the
    full evaluation priority: absent → no message → freshness → content
    → rate → all pass."""

    def test_scenario_full_lifecycle(self, gate_module):
        checker = gate_module.TopicConditionChecker(
            "data",
            {
                "topic": "/data",
                "timeout_sec": 3.0,
                "condition": "msg.data > 0",
                "min_rate_hz": 5.0,
                "rate_window_sec": 2.0,
                "escalation": 2,
                "absence_timeout_sec": 2.0,
                "absence_escalation": 1,
                "rate_escalation": 1,
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = []
        checker.setup(node)

        gate = _make_gate(gate_module, [checker], settle_timeout=0.0)

        # Step 1: Topic absent → absence failure
        gate._evaluate()
        assert gate._gate_level >= 1
        assert "not yet advertised" in gate._groups[0].results[0].reason

        # Step 2: Advertise topic → no message yet
        node.get_topic_names_and_types.return_value = [
            ("/data", ["std_msgs/msg/Float64"]),
        ]
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert "no message" in gate._groups[0].results[0].reason

        # Step 3: Send message with bad content (msg.data = -5)
        msg_bad = MagicMock()
        msg_bad.data = -5
        checker._cb(msg_bad)
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert "condition failed" in gate._groups[0].results[0].reason
        assert gate._groups[0].results[0].escalation == 2

        # Step 4: Send good content but rate too low (1 msg in 2s window)
        msg_good = MagicMock()
        msg_good.data = 10
        checker._cb(msg_good)
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert "rate" in gate._groups[0].results[0].reason
        assert gate._groups[0].results[0].escalation == 1

        # Step 5: Send enough messages to satisfy rate
        for _ in range(14):
            m = MagicMock()
            m.data = 10
            checker._cb(m)
        gate._groups[0].settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert gate._gate_level == 0


# ---------------------------------------------------------------------------
# Reason formatting scenarios
# ---------------------------------------------------------------------------


class TestScenarioReasonAutoEnrich:
    """TopicConditionChecker auto-enriches failure reasons with actual
    message values extracted from the condition AST."""

    def test_scenario_auto_enrich_simple_bool(self, gate_module):
        """condition='not msg.data', msg.data=True → reason includes msg.data=True."""
        checker = gate_module.TopicConditionChecker(
            "estop",
            {"topic": "/estop", "timeout_sec": 5.0, "condition": "not msg.data"},
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/estop", ["std_msgs/msg/Bool"]),
        ]
        checker.setup(node)

        msg = MagicMock()
        msg.data = True
        checker._cb(msg)
        result = checker.evaluate()

        assert result.passed is False
        assert "msg.data=True" in result.reason
        assert "not msg.data" in result.reason

    def test_scenario_auto_enrich_compound_condition(self, gate_module):
        """Two msg refs in one condition → both values shown in reason."""
        checker = gate_module.TopicConditionChecker(
            "pose",
            {
                "topic": "/pose",
                "timeout_sec": 5.0,
                "condition": "msg.position.x > 0 and msg.orientation.w > 0.5",
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/pose", ["geometry_msgs/msg/Pose"]),
        ]
        checker.setup(node)

        msg = MagicMock()
        msg.position.x = -1.0
        msg.orientation.w = 0.9
        checker._cb(msg)
        result = checker.evaluate()

        assert result.passed is False
        assert "msg.position.x=-1.0" in result.reason
        assert "msg.orientation.w=0.9" in result.reason

    def test_scenario_auto_enrich_subscript(self, gate_module):
        """condition with subscript → reason shows indexed value."""
        checker = gate_module.TopicConditionChecker(
            "effort",
            {
                "topic": "/joints",
                "timeout_sec": 5.0,
                "condition": "msg.effort[0] < 50.0",
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/joints", ["sensor_msgs/msg/JointState"]),
        ]
        checker.setup(node)

        msg = MagicMock()
        msg.effort = [72.3, 12.1]
        checker._cb(msg)
        result = checker.evaluate()

        assert result.passed is False
        assert "msg.effort[0]=72.3" in result.reason

    def test_scenario_passing_condition_shows_ok(self, gate_module):
        """When condition passes, reason is 'ok', not enriched."""
        checker = gate_module.TopicConditionChecker(
            "temp",
            {
                "topic": "/temp",
                "timeout_sec": 5.0,
                "condition": "msg.data < 100",
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/temp", ["std_msgs/msg/Float64"]),
        ]
        checker.setup(node)

        msg = MagicMock()
        msg.data = 50.0
        checker._cb(msg)
        result = checker.evaluate()

        assert result.passed is True
        assert result.reason == "ok"


class TestScenarioReasonTemplate:
    """TopicConditionChecker with custom reason template overrides
    the auto-enriched default."""

    def test_scenario_reason_template_end_to_end(self, gate_module):
        """Custom reason template with {condition}, {detail}, {msg.*}."""
        checker = gate_module.TopicConditionChecker(
            "estop",
            {
                "topic": "/estop",
                "timeout_sec": 5.0,
                "condition": "not msg.data",
                "reason": "E-stop is pressed ({detail})",
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/estop", ["std_msgs/msg/Bool"]),
        ]
        checker.setup(node)

        msg = MagicMock()
        msg.data = True
        checker._cb(msg)
        result = checker.evaluate()

        assert result.passed is False
        assert result.reason == "E-stop is pressed (msg.data=True)"

    def test_scenario_reason_template_with_format_spec(self, gate_module):
        """reason template with .format() spec for precision."""
        checker = gate_module.TopicConditionChecker(
            "temp",
            {
                "topic": "/temp",
                "timeout_sec": 5.0,
                "condition": "msg.data < 45.0",
                "reason": "Temperature too high: {msg.data:.1f}C",
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/temp", ["std_msgs/msg/Float64"]),
        ]
        checker.setup(node)

        msg = MagicMock()
        msg.data = 52.789
        checker._cb(msg)
        result = checker.evaluate()

        assert result.passed is False
        assert result.reason == "Temperature too high: 52.8C"

    def test_scenario_reason_template_bad_key_falls_back(self, gate_module):
        """Bad template key → falls back to auto-enriched reason."""
        checker = gate_module.TopicConditionChecker(
            "temp",
            {
                "topic": "/temp",
                "timeout_sec": 5.0,
                "condition": "msg.data < 45.0",
                "reason": "Bad: {nonexistent_var}",
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/temp", ["std_msgs/msg/Float64"]),
        ]
        checker.setup(node)

        msg = MagicMock()
        msg.data = 52.0
        checker._cb(msg)
        result = checker.evaluate()

        assert result.passed is False
        assert "condition failed:" in result.reason
        assert "msg.data=52.0" in result.reason

    def test_scenario_single_shot_with_reason_template(self, gate_module):
        """single_shot + reason template: enriched reason on expression failure."""
        checker = gate_module.TopicConditionChecker(
            "desc",
            {
                "topic": "/description",
                "single_shot": True,
                "timeout_sec": 1.0,
                "condition": "msg.data == 'expected_urdf'",
                "reason": "Wrong URDF: {msg.data}",
            },
        )
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/description", ["std_msgs/msg/String"]),
        ]
        checker.setup(node)

        msg = MagicMock()
        msg.data = "wrong_urdf"
        checker._cb(msg)
        result = checker.evaluate()

        assert result.passed is False
        assert result.reason == "Wrong URDF: wrong_urdf"
