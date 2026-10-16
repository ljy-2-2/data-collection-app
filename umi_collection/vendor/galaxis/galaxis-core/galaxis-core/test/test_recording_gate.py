"""Unit tests for recording_gate_node.

Tests the condition checkers and config loading as pure logic — no live
ROS2 required.  Uses the ``mock_rclpy`` fixture from conftest.py.
"""

import time
import textwrap
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def gate_module(mock_rclpy):
    """Import recording_gate_node with mocked ROS2."""
    # Also mock diagnostic_msgs (needed by the node)
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

    # Flush any cached import
    for key in list(sys.modules):
        if key.startswith("galaxis_core.recording_gate"):
            del sys.modules[key]

    import galaxis_core.recording_gate_node as mod

    return mod


@pytest.fixture()
def diag_status_cls(gate_module):
    """Return the DiagnosticStatus class visible to the gate module."""
    import sys

    return sys.modules["diagnostic_msgs.msg"].DiagnosticStatus


@pytest.fixture()
def diag_array_cls(gate_module):
    """Return the DiagnosticArray class visible to the gate module."""
    import sys

    return sys.modules["diagnostic_msgs.msg"].DiagnosticArray


@pytest.fixture()
def bool_cls(gate_module):
    """Return the Bool class visible to the gate module."""
    import sys

    return sys.modules["std_msgs.msg"].Bool


# ---------------------------------------------------------------------------
# DiagnosticsErrorRateChecker
# ---------------------------------------------------------------------------


def test_camera_warning_is_visible_without_blocking_start_or_restarting_settle(gate_module):
    checker = gate_module.TopicConditionChecker('camera_warning', dict(
        topic='/center_camera/image_raw/compressed', diagnostic_only=True,
        escalation=0, timeout_sec=.5))
    checker.setup(MagicMock())
    # Even a failing warning leaves the already healthy gate open.
    result = checker.evaluate()
    assert not result.passed and result.escalation == 0 and result.settle_exempt
    from galaxis_core.recording_gate import evaluate_gate
    outcome = evaluate_gate([checker], 0, True, False, 0, 30, 1, 100)
    assert outcome[0] == 0 and outcome[1] is True
    assert gate_module.RecordingGateNode._escalation_to_diag_level(0, False) == 1


class TestDiagnosticsErrorRateChecker:
    def test_passes_under_threshold(self, gate_module, diag_array_cls, diag_status_cls):
        cfg = {"topic": "/diag", "max_errors": 3, "window_sec": 30.0, "name_filter": ""}
        checker = gate_module.DiagnosticsErrorRateChecker("diag", cfg)
        node = MagicMock()
        checker.setup(node)

        # Send a message with 1 error
        msg = diag_array_cls()
        s = diag_status_cls()
        s.level = diag_status_cls.ERROR
        s.name = "some_component"
        msg.status = [s]
        checker._cb(msg)

        result = checker.evaluate()
        assert result.passed is True
        assert "1 errors" in result.reason

    def test_fails_over_threshold(self, gate_module, diag_array_cls, diag_status_cls):
        cfg = {"topic": "/diag", "max_errors": 2, "window_sec": 30.0, "name_filter": ""}
        checker = gate_module.DiagnosticsErrorRateChecker("diag", cfg)
        node = MagicMock()
        checker.setup(node)

        # Send 3 error statuses
        for _ in range(3):
            msg = diag_array_cls()
            s = diag_status_cls()
            s.level = diag_status_cls.ERROR
            s.name = "comp"
            msg.status = [s]
            checker._cb(msg)

        result = checker.evaluate()
        assert result.passed is False
        assert "3 errors" in result.reason

    def test_sliding_window_expiry(self, gate_module, diag_array_cls, diag_status_cls):
        cfg = {"topic": "/diag", "max_errors": 2, "window_sec": 1.0, "name_filter": ""}
        checker = gate_module.DiagnosticsErrorRateChecker("diag", cfg)
        node = MagicMock()
        checker.setup(node)

        # Insert errors in the past (outside the window)
        now = time.monotonic()
        checker._errors.append(now - 2.0)
        checker._errors.append(now - 2.0)
        checker._errors.append(now - 2.0)
        checker._last_stamp = now

        result = checker.evaluate()
        assert result.passed is True  # all expired

    def test_name_filter(self, gate_module, diag_array_cls, diag_status_cls):
        cfg = {
            "topic": "/diag",
            "max_errors": 1,
            "window_sec": 30.0,
            "name_filter": "motor",
        }
        checker = gate_module.DiagnosticsErrorRateChecker("diag", cfg)
        node = MagicMock()
        checker.setup(node)

        # Error with non-matching name — should be ignored
        msg = diag_array_cls()
        s = diag_status_cls()
        s.level = diag_status_cls.ERROR
        s.name = "camera_driver"
        msg.status = [s]
        checker._cb(msg)

        result = checker.evaluate()
        assert result.passed is True

        # Error with matching name — should be counted
        s2 = diag_status_cls()
        s2.level = diag_status_cls.ERROR
        s2.name = "motor_controller"
        msg2 = diag_array_cls()
        msg2.status = [s2]
        checker._cb(msg2)

        result = checker.evaluate()
        assert result.passed is False

    def test_fails_on_no_message(self, gate_module):
        cfg = {"topic": "/diag", "max_errors": 3, "window_sec": 30.0, "name_filter": ""}
        checker = gate_module.DiagnosticsErrorRateChecker("diag", cfg)
        node = MagicMock()
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is False
        assert "no message" in result.reason


# ---------------------------------------------------------------------------
# TopicConditionChecker
# ---------------------------------------------------------------------------


class TestTopicConditionChecker:
    @staticmethod
    def _make_node(topic="/test", type_str="std_msgs/msg/Bool"):
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [(topic, [type_str])]
        return node

    def test_passes_within_timeout(self, gate_module):
        cfg = {"topic": "/test", "timeout_sec": 5.0}
        checker = gate_module.TopicConditionChecker("th", cfg)
        node = self._make_node()
        checker.setup(node)

        checker._cb(MagicMock())

        result = checker.evaluate()
        assert result.passed is True

    def test_fails_no_message(self, gate_module):
        cfg = {"topic": "/test", "timeout_sec": 5.0}
        checker = gate_module.TopicConditionChecker("th", cfg)
        node = self._make_node()
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is False
        assert "no message" in result.reason

    def test_fails_freshness_timeout(self, gate_module):
        cfg = {"topic": "/test", "timeout_sec": 1.0}
        checker = gate_module.TopicConditionChecker("th", cfg)
        node = self._make_node()
        checker.setup(node)

        checker._cb(MagicMock())
        checker._last_stamp = time.monotonic() - 2.0

        result = checker.evaluate()
        assert result.passed is False
        assert "timeout" in result.reason

    def test_fails_topic_absent(self, gate_module):
        cfg = {"topic": "/test", "timeout_sec": 5.0, "absence_timeout_sec": 0.0}
        checker = gate_module.TopicConditionChecker("th", cfg)
        node = MagicMock()
        node.get_topic_names_and_types.return_value = []
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is False
        assert "not advertised" in result.reason

    def test_absence_escalation_override(self, gate_module):
        cfg = {
            "topic": "/test",
            "timeout_sec": 5.0,
            "escalation": 2,
            "absence_escalation": 1,
        }
        checker = gate_module.TopicConditionChecker("th", cfg)
        node = MagicMock()
        node.get_topic_names_and_types.return_value = []
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is False
        assert result.escalation == 1

    def test_condition_expression_passes(self, gate_module):
        cfg = {
            "topic": "/test",
            "timeout_sec": 5.0,
            "condition": "msg.data < 10.0",
        }
        checker = gate_module.TopicConditionChecker("th", cfg)
        node = self._make_node()
        checker.setup(node)

        msg = MagicMock()
        msg.data = 5.0
        checker._cb(msg)

        result = checker.evaluate()
        assert result.passed is True

    def test_condition_expression_fails(self, gate_module):
        cfg = {
            "topic": "/test",
            "timeout_sec": 5.0,
            "condition": "msg.data < 10.0",
        }
        checker = gate_module.TopicConditionChecker("th", cfg)
        node = self._make_node()
        checker.setup(node)

        msg = MagicMock()
        msg.data = 50.0
        checker._cb(msg)

        result = checker.evaluate()
        assert result.passed is False
        assert "condition failed" in result.reason

    def test_rate_below_minimum(self, gate_module):
        cfg = {
            "topic": "/test",
            "timeout_sec": 5.0,
            "min_rate_hz": 10.0,
            "rate_window_sec": 5.0,
        }
        checker = gate_module.TopicConditionChecker("th", cfg)
        node = self._make_node()
        checker.setup(node)

        # Only 1 message in 5s window → 0.2 Hz
        checker._cb(MagicMock())

        result = checker.evaluate()
        assert result.passed is False
        assert "rate" in result.reason

    def test_rate_escalation_override(self, gate_module):
        cfg = {
            "topic": "/test",
            "timeout_sec": 5.0,
            "escalation": 2,
            "min_rate_hz": 10.0,
            "rate_window_sec": 5.0,
            "rate_escalation": 1,
        }
        checker = gate_module.TopicConditionChecker("th", cfg)
        node = self._make_node()
        checker.setup(node)

        checker._cb(MagicMock())

        result = checker.evaluate()
        assert result.passed is False
        assert result.escalation == 1

    def test_no_optional_fields_behaves_like_heartbeat(self, gate_module):
        cfg = {"topic": "/test", "timeout_sec": 5.0}
        checker = gate_module.TopicConditionChecker("th", cfg)
        node = self._make_node()
        checker.setup(node)

        checker._cb(MagicMock())

        result = checker.evaluate()
        assert result.passed is True

    def test_invalid_condition_raises(self, gate_module):
        cfg = {
            "topic": "/test",
            "timeout_sec": 5.0,
            "condition": "len(msg.data)",
        }
        with pytest.raises(ValueError, match="Unsafe node"):
            checker = gate_module.TopicConditionChecker("th", cfg)
            node = self._make_node()
            checker.setup(node)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadGateConfig:
    def test_load_default_only(self, gate_module, tmp_path):
        default = tmp_path / "default.yaml"
        default.write_text(
            textwrap.dedent("""\
            eval_rate: 2.0
            settle_timeout_sec: 30.0
            conditions:
              estop:
                type: topic_condition
                enabled: false
                topic: /runstop_button
                condition: "not msg.data"
        """)
        )

        cfg = gate_module.load_gate_config(str(default))
        assert cfg["eval_rate"] == 2.0
        assert cfg["conditions"]["estop"]["enabled"] is False

    def test_merge_override(self, gate_module, tmp_path):
        default = tmp_path / "default.yaml"
        default.write_text(
            textwrap.dedent("""\
            eval_rate: 2.0
            settle_timeout_sec: 30.0
            conditions:
              estop:
                type: topic_condition
                enabled: false
                topic: /runstop_button
                condition: "not msg.data"
                timeout_sec: 5.0
        """)
        )

        override = tmp_path / "override.yaml"
        override.write_text(
            textwrap.dedent("""\
            settle_timeout_sec: 20.0
            conditions:
              estop:
                enabled: true
              wireless_stop:
                type: topic_condition
                enabled: true
                topic: /wireless_stop
                condition: "not msg.data"
                timeout_sec: 5.0
        """)
        )

        cfg = gate_module.load_gate_config(str(default), str(override))
        assert cfg["settle_timeout_sec"] == 20.0
        assert cfg["eval_rate"] == 2.0  # preserved from default
        assert cfg["conditions"]["estop"]["enabled"] is True
        assert cfg["conditions"]["estop"]["topic"] == "/runstop_button"  # preserved
        assert cfg["conditions"]["wireless_stop"]["enabled"] is True

    def test_missing_override_is_ok(self, gate_module, tmp_path):
        default = tmp_path / "default.yaml"
        default.write_text("eval_rate: 1.0\n")

        cfg = gate_module.load_gate_config(
            str(default), str(tmp_path / "nonexistent.yaml")
        )
        assert cfg["eval_rate"] == 1.0

    def test_disabled_conditions_skipped_in_setup(self, gate_module, tmp_path):
        default = tmp_path / "default.yaml"
        default.write_text(
            textwrap.dedent("""\
            eval_rate: 2.0
            settle_timeout_sec: 30.0
            conditions:
              estop:
                type: topic_condition
                enabled: false
                topic: /runstop_button
                condition: "not msg.data"
                timeout_sec: 5.0
        """)
        )

        cfg = gate_module.load_gate_config(str(default))
        conditions = cfg.get("conditions", {})
        enabled = {k: v for k, v in conditions.items() if v.get("enabled")}
        assert len(enabled) == 0


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_nested_merge(self, gate_module):
        base = {"a": {"b": 1, "c": 2}, "d": 3}
        override = {"a": {"b": 10, "e": 5}, "f": 6}
        result = gate_module._deep_merge(base, override)
        assert result == {"a": {"b": 10, "c": 2, "e": 5}, "d": 3, "f": 6}
        # Original not mutated
        assert base["a"]["b"] == 1

    def test_override_replaces_non_dict(self, gate_module):
        base = {"a": 1}
        override = {"a": {"nested": True}}
        result = gate_module._deep_merge(base, override)
        assert result == {"a": {"nested": True}}


# ---------------------------------------------------------------------------
# Startup behavior (fail-closed)
# ---------------------------------------------------------------------------


class TestStartupBehavior:
    """Verify that checkers start in the failed state."""

    def test_topic_condition_starts_failed(self, gate_module):
        cfg = {"topic": "/test", "condition": "not msg.data", "timeout_sec": 5.0}
        checker = gate_module.TopicConditionChecker("test", cfg)
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/test", ["std_msgs/msg/Bool"]),
        ]
        node.get_shared_subscription = MagicMock()
        checker.setup(node)
        assert checker.evaluate().passed is False

    def test_topic_condition_no_topic_starts_failed(self, gate_module):
        cfg = {"topic": "/test", "timeout_sec": 5.0}
        checker = gate_module.TopicConditionChecker("test", cfg)
        node = MagicMock()
        node.get_topic_names_and_types.return_value = []
        node.get_shared_subscription = MagicMock()
        checker.setup(node)
        assert checker.evaluate().passed is False

    def test_diagnostics_starts_failed(self, gate_module):
        cfg = {"topic": "/diag", "max_errors": 3, "window_sec": 30.0, "name_filter": ""}
        checker = gate_module.DiagnosticsErrorRateChecker("test", cfg)
        node = MagicMock()
        checker.setup(node)
        assert checker.evaluate().passed is False

    def test_transitions_to_true_once_all_pass(self, gate_module, bool_cls):
        """Simulate two checkers: both must pass for gate_level to be 0."""
        cfg1 = {"topic": "/a", "condition": "not msg.data", "timeout_sec": 5.0}
        cfg2 = {"topic": "/b", "timeout_sec": 5.0}

        c1 = gate_module.TopicConditionChecker("a", cfg1)
        c2 = gate_module.TopicConditionChecker("b", cfg2)
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/a", ["std_msgs/msg/Bool"]),
            ("/b", ["std_msgs/msg/Bool"]),
        ]
        node.get_shared_subscription = MagicMock()
        c1.setup(node)
        c2.setup(node)

        # Both fail initially
        assert not c1.evaluate().passed
        assert not c2.evaluate().passed

        # Only c1 passes
        msg = bool_cls()
        msg.data = False
        c1._cb(msg)
        assert c1.evaluate().passed
        assert not c2.evaluate().passed

        # Now c2 also passes
        c2._cb(MagicMock())
        assert c1.evaluate().passed
        assert c2.evaluate().passed

        # Aggregation: all pass
        results = [c1.evaluate(), c2.evaluate()]
        assert all(r.passed for r in results)


# ---------------------------------------------------------------------------
# Re-settle after condition failure
# ---------------------------------------------------------------------------


class TestReSettle:
    """Verify that conditions must hold for recovery_sec after recovery."""

    def _make_gate(self, gate_module, checkers, *, settle_sec=0.0, recovery_sec=0.5):
        from conftest import make_test_gate

        return make_test_gate(
            gate_module, checkers, settle_sec=settle_sec, recovery_sec=recovery_sec
        )

    def _group(self, gate):
        return gate._groups[0]

    def test_re_settle_resets_on_failure(self, gate_module):
        """After conditions pass then fail, settled resets to False."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )

        gate = self._make_gate(gate_module, [checker], settle_sec=0.0)

        gate._evaluate()
        assert self._group(gate).settled is True
        assert gate._gate_level == 0

        checker.evaluate.return_value = gate_module.ConditionResult("c", False, "bad")
        gate._evaluate()
        assert self._group(gate).settled is False
        assert gate._gate_level > 0
        assert self._group(gate).settle_is_initial is False

    def test_holds_blocked_during_re_settle(self, gate_module, monkeypatch):
        """_gate_level stays > 0 when all_pass but re-settle hasn't elapsed."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )

        gate = self._make_gate(
            gate_module, [checker], settle_sec=0.0, recovery_sec=10.0
        )

        gate._evaluate()
        assert gate._gate_level == 0

        checker.evaluate.return_value = gate_module.ConditionResult("c", False, "bad")
        gate._evaluate()
        assert gate._gate_level > 0

        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )
        gate._evaluate()
        assert self._group(gate).settled is False
        assert gate._gate_level > 0

    def test_allows_after_recovery_timeout(self, gate_module):
        """_gate_level becomes 0 once conditions hold for recovery_sec."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )

        gate = self._make_gate(gate_module, [checker], settle_sec=0.0, recovery_sec=0.0)

        gate._evaluate()
        assert gate._gate_level == 0

        checker.evaluate.return_value = gate_module.ConditionResult("c", False, "bad")
        gate._evaluate()
        assert gate._gate_level > 0

        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )
        self._group(gate).settle_start_time = time.monotonic() - 1.0
        gate._evaluate()
        assert self._group(gate).settled is True
        assert gate._gate_level == 0

    def test_startup_settle_uses_startup_timeout(self, gate_module):
        """Startup uses settle_sec, not recovery_sec."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )

        gate = self._make_gate(
            gate_module, [checker], settle_sec=10.0, recovery_sec=0.0
        )

        gate._evaluate()
        assert self._group(gate).settle_is_initial is True
        assert self._group(gate).settled is False
        assert gate._gate_level > 0


# ---------------------------------------------------------------------------
# Settle-exempt (single_shot) conditions
# ---------------------------------------------------------------------------


class TestSettleExempt:
    """Verify that settle_exempt conditions bypass the settle timer."""

    def _make_gate(self, gate_module, checkers, *, settle_sec=10.0, recovery_sec=10.0):
        from conftest import make_test_gate

        return make_test_gate(
            gate_module, checkers, settle_sec=settle_sec, recovery_sec=recovery_sec
        )

    @staticmethod
    def _group(gate):
        return gate._groups[0]

    def test_all_exempt_group_settles_immediately(self, gate_module):
        """A group with only settle-exempt conditions settles on first eval."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0, settle_exempt=True
        )

        gate = self._make_gate(gate_module, [checker], settle_sec=10.0)
        gate._evaluate()

        assert self._group(gate).settled is True
        assert gate._gate_level == 0

    def test_all_exempt_group_failing_shows_level(self, gate_module):
        """All-exempt group: failing condition → gate blocked, no settle wait."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", False, "waiting", escalation=2, settle_exempt=True
        )

        gate = self._make_gate(gate_module, [checker], settle_sec=10.0)
        gate._evaluate()

        assert self._group(gate).settled is True
        assert gate._gate_level == 2

    def test_mixed_group_settle_waits_for_periodic_only(self, gate_module):
        """Mixed group: settle waits for periodic condition, not single_shot."""
        periodic = MagicMock()
        periodic.evaluate.return_value = gate_module.ConditionResult(
            "periodic", True, "ok", escalation=0
        )
        single_shot = MagicMock()
        single_shot.evaluate.return_value = gate_module.ConditionResult(
            "single_shot", False, "waiting", escalation=2, settle_exempt=True
        )

        gate = self._make_gate(gate_module, [periodic, single_shot], settle_sec=10.0)
        gate._evaluate()

        # Settle hasn't completed (periodic needs 10s), but single_shot
        # is exempt so it doesn't block settle. However settle_sec=10
        # and periodic is passing → settle waits for elapsed >= 10.
        assert self._group(gate).settled is False
        # Gate blocked: single_shot failing + during settle
        assert gate._gate_level >= 1

    def test_exempt_failure_does_not_trigger_resettle(self, gate_module):
        """A settle-exempt condition failing doesn't reset the settle timer."""
        periodic = MagicMock()
        periodic.evaluate.return_value = gate_module.ConditionResult(
            "periodic", True, "ok", escalation=0
        )
        exempt = MagicMock()
        exempt.evaluate.return_value = gate_module.ConditionResult(
            "exempt", True, "ok", escalation=0, settle_exempt=True
        )

        gate = self._make_gate(
            gate_module, [periodic, exempt], settle_sec=0.0, recovery_sec=10.0
        )
        gate._evaluate()
        assert gate._gate_level == 0

        # Exempt condition fails → should NOT trigger re-settle
        exempt.evaluate.return_value = gate_module.ConditionResult(
            "exempt", False, "bad", escalation=1, settle_exempt=True
        )
        gate._evaluate()

        # settled remains True (no re-settle), gate shows exempt's failure
        assert self._group(gate).settled is True
        assert gate._gate_level == 1


# ---------------------------------------------------------------------------
# Escalation levels
# ---------------------------------------------------------------------------


class TestEscalationLevels:
    """Verify per-condition escalation behaviour."""

    def _make_gate(self, gate_module, checkers, *, settle_sec=0.0, recovery_sec=0.0):
        from conftest import make_test_gate

        return make_test_gate(
            gate_module, checkers, settle_sec=settle_sec, recovery_sec=recovery_sec
        )

    def test_default_escalation_is_hard_stop(self, gate_module):
        """Config without 'escalation' key defaults to 2 (HARD_STOP)."""
        cfg = {"topic": "/test", "condition": "not msg.data", "timeout_sec": 5.0}
        checker = gate_module.TopicConditionChecker("test", cfg)
        assert checker.escalation == gate_module.EscalationLevel.HARD_STOP

    def test_custom_escalation_from_config(self, gate_module):
        """Config with escalation=1 reads correctly."""
        cfg = {
            "topic": "/test",
            "condition": "not msg.data",
            "timeout_sec": 5.0,
            "escalation": 1,
        }
        checker = gate_module.TopicConditionChecker("test", cfg)
        assert checker.escalation == gate_module.EscalationLevel.BLOCK_START

    def test_escalation_propagated_in_result(self, gate_module):
        """Failing result carries the checker's escalation level."""
        cfg = {
            "topic": "/test",
            "condition": "not msg.data",
            "timeout_sec": 5.0,
            "escalation": 1,
        }
        checker = gate_module.TopicConditionChecker("test", cfg)
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/test", ["std_msgs/msg/Bool"]),
        ]
        node.get_shared_subscription = MagicMock()
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is False
        assert result.escalation == 1

    def test_passing_result_has_escalation_zero(self, gate_module, bool_cls):
        """Passing result always has escalation=0."""
        cfg = {
            "topic": "/test",
            "condition": "not msg.data",
            "timeout_sec": 5.0,
            "escalation": 2,
        }
        checker = gate_module.TopicConditionChecker("test", cfg)
        node = MagicMock()
        node.get_topic_names_and_types.return_value = [
            ("/test", ["std_msgs/msg/Bool"]),
        ]
        node.get_shared_subscription = MagicMock()
        checker.setup(node)

        msg = bool_cls()
        msg.data = False
        checker._cb(msg)

        result = checker.evaluate()
        assert result.passed is True
        assert result.escalation == 0

    def test_all_pass_gives_level_zero(self, gate_module):
        """When all conditions pass, gate level is 0."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )

        gate = self._make_gate(gate_module, [checker])
        gate._evaluate()
        assert gate._gate_level == 0

    def test_single_level1_failure_gives_level1(self, gate_module):
        """A single level-1 failure results in gate_level=1."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", False, "bad", escalation=1
        )

        gate = self._make_gate(gate_module, [checker])
        gate._evaluate()
        assert gate._gate_level == 1

    def test_mixed_levels_max_wins(self, gate_module):
        """When multiple conditions fail, max escalation wins."""
        c1 = MagicMock()
        c1.evaluate.return_value = gate_module.ConditionResult(
            "a", False, "bad", escalation=1
        )
        c2 = MagicMock()
        c2.evaluate.return_value = gate_module.ConditionResult(
            "b", False, "bad", escalation=2
        )

        gate = self._make_gate(gate_module, [c1, c2])
        gate._evaluate()
        assert gate._gate_level == 2

    def test_re_settle_triggered_from_zero_to_failure(self, gate_module):
        """Transition from level 0 to any failure triggers re-settle."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )

        gate = self._make_gate(gate_module, [checker], recovery_sec=10.0)
        gate._evaluate()
        assert gate._gate_level == 0
        assert gate._groups[0].settled is True

        # Now condition fails with level 1
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", False, "bad", escalation=1
        )
        gate._evaluate()
        assert gate._groups[0].settled is False
        assert gate._groups[0].settle_is_initial is False

    def test_settle_period_elevates_to_block_start_minimum(self, gate_module):
        """During settle period, gate level is at minimum BLOCK_START (1)."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )

        gate = self._make_gate(gate_module, [checker], recovery_sec=10.0)
        gate._evaluate()
        assert gate._gate_level == 0

        # Fail → triggers re-settle
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", False, "bad", escalation=1
        )
        gate._evaluate()

        # Recover — but during re-settle (10s), even with all passing,
        # level should be at minimum 1 (BLOCK_START)
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )
        gate._evaluate()
        assert gate._groups[0].settled is False
        assert gate._gate_level >= 1

    def test_escalation_zero_failure_does_not_block(self, gate_module):
        """A failing condition with escalation=0 does not block recording."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", False, "bad", escalation=0
        )

        gate = self._make_gate(gate_module, [checker])
        gate._evaluate()
        assert gate._gate_level == 0

    def test_setup_warns_on_escalation_zero(self, gate_module):
        """_setup_checkers logs a warning for escalation=0 conditions."""
        cfg = {
            "topic": "/test",
            "condition": "not msg.data",
            "timeout_sec": 5.0,
            "escalation": 0,
        }
        checker = gate_module.TopicConditionChecker("noop", cfg)
        assert checker.escalation == gate_module.EscalationLevel.OK

    def test_settle_timeout_with_failing_conditions(self, gate_module):
        """Settle timeout expires while conditions still fail."""
        checker = MagicMock()
        checker.evaluate.return_value = gate_module.ConditionResult(
            "c", False, "bad", escalation=1
        )

        gate = self._make_gate(gate_module, [checker], settle_sec=0.1)

        # First evaluate — not settled, gate elevated
        gate._evaluate()
        assert gate._groups[0].settled is False
        assert gate._gate_level >= 1

        # Push settle start into the past beyond timeout
        gate._groups[0].settle_start_time = time.monotonic() - 1.0

        # Second evaluate — timeout expired with failure still active
        gate._evaluate()
        assert gate._groups[0].settled is True
        assert gate._gate_level == 1
        gate.get_logger().warning.assert_called()


# ---------------------------------------------------------------------------
# TfAvailabilityChecker
# ---------------------------------------------------------------------------


class TestTfAvailabilityChecker:
    """Tests for the TfAvailabilityChecker condition type."""

    @staticmethod
    def _make_tf_mocks(monkeypatch):
        """Inject a fake tf2_ros module and return (buffer_mock, module)."""
        import sys
        import types

        tf2_ros_mod = types.ModuleType("tf2_ros")
        buffer_mock = MagicMock()
        tf2_ros_mod.Buffer = MagicMock(return_value=buffer_mock)
        tf2_ros_mod.TransformListener = MagicMock()
        monkeypatch.setitem(sys.modules, "tf2_ros", tf2_ros_mod)

        # Ensure rclpy.time and rclpy.duration exist as both sys.modules
        # entries AND attributes on the rclpy module (needed for `import rclpy.time`)
        rclpy_mod = sys.modules["rclpy"]

        rclpy_time = types.ModuleType("rclpy.time")
        rclpy_time.Time = MagicMock(return_value=MagicMock())
        rclpy_time.Time.from_msg = MagicMock(return_value=MagicMock(nanoseconds=0))
        monkeypatch.setitem(sys.modules, "rclpy.time", rclpy_time)
        rclpy_mod.time = rclpy_time

        rclpy_duration = types.ModuleType("rclpy.duration")
        rclpy_duration.Duration = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "rclpy.duration", rclpy_duration)
        rclpy_mod.duration = rclpy_duration

        return buffer_mock

    def _make_node(self, buffer_mock):
        """Return a MagicMock node whose get_shared_tf_buffer returns buffer_mock."""
        node = MagicMock()
        node.get_shared_tf_buffer.return_value = buffer_mock
        return node

    def test_passes_when_transform_available(self, gate_module, monkeypatch):
        """Passes when can_transform returns True."""
        buffer_mock = self._make_tf_mocks(monkeypatch)
        buffer_mock.can_transform.return_value = True

        cfg = {
            "escalation": 2,
            "frames": [{"source": "odom", "target": "base_link", "max_age_sec": -1.0}],
            "timeout_sec": 5.0,
        }
        checker = gate_module.TfAvailabilityChecker("tf_test", cfg)
        node = self._make_node(buffer_mock)
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is True

    def test_fails_when_can_transform_false(self, gate_module, monkeypatch):
        """Fails when can_transform returns False (non-blocking check)."""
        buffer_mock = self._make_tf_mocks(monkeypatch)
        buffer_mock.can_transform.return_value = False

        cfg = {
            "escalation": 2,
            "frames": [{"source": "odom", "target": "base_link"}],
            "timeout_sec": 5.0,
        }
        checker = gate_module.TfAvailabilityChecker("tf_test", cfg)
        node = self._make_node(buffer_mock)
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is False
        assert "not available" in result.reason

    def test_fails_on_lookup_exception(self, gate_module, monkeypatch):
        """Fails when lookup_transform raises (with max_age staleness check)."""
        buffer_mock = self._make_tf_mocks(monkeypatch)
        buffer_mock.can_transform.return_value = True
        buffer_mock.lookup_transform.side_effect = Exception("no transform")

        cfg = {
            "escalation": 2,
            "frames": [{"source": "odom", "target": "base_link", "max_age_sec": 1.0}],
            "timeout_sec": 5.0,
        }
        checker = gate_module.TfAvailabilityChecker("tf_test", cfg)
        node = self._make_node(buffer_mock)
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is False
        assert "lookup failed" in result.reason

    def test_fails_when_transform_too_old(self, gate_module, monkeypatch):
        """Fails when transform age exceeds max_age_sec."""
        buffer_mock = self._make_tf_mocks(monkeypatch)
        buffer_mock.can_transform.return_value = True
        transform = MagicMock()
        transform.header.stamp = MagicMock()
        buffer_mock.lookup_transform.return_value = transform

        # Make the age check return a large value
        import sys

        rclpy_time = sys.modules["rclpy.time"]
        now_mock = MagicMock()
        old_stamp = MagicMock()
        age_ns = int(5.0 * 1e9)  # 5 seconds
        diff_mock = MagicMock()
        diff_mock.nanoseconds = age_ns
        now_mock.__sub__ = MagicMock(return_value=diff_mock)
        rclpy_time.Time.from_msg = MagicMock(return_value=old_stamp)

        cfg = {
            "escalation": 2,
            "frames": [{"source": "odom", "target": "base_link", "max_age_sec": 1.0}],
            "timeout_sec": 5.0,
        }
        checker = gate_module.TfAvailabilityChecker("tf_test", cfg)
        node = self._make_node(buffer_mock)
        node.get_clock.return_value.now.return_value = now_mock
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is False
        assert "too old" in result.reason

    def test_fails_when_tf2_ros_not_available(self, gate_module, monkeypatch):
        """Fails gracefully when tf2_ros is not installed."""
        import sys

        # Ensure tf2_ros is NOT importable
        monkeypatch.delitem(sys.modules, "tf2_ros", raising=False)
        monkeypatch.setattr(
            "builtins.__import__",
            _make_import_blocker("tf2_ros", monkeypatch),
        )

        cfg = {
            "escalation": 2,
            "frames": [{"source": "odom", "target": "base_link"}],
            "timeout_sec": 5.0,
        }
        checker = gate_module.TfAvailabilityChecker("tf_test", cfg)
        node = MagicMock()
        node.get_shared_tf_buffer.return_value = None
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is False
        assert "tf2_ros" in result.reason

    def test_multiple_frames_all_pass(self, gate_module, monkeypatch):
        """Passes when all frame pairs are available."""
        buffer_mock = self._make_tf_mocks(monkeypatch)
        buffer_mock.can_transform.return_value = True

        cfg = {
            "escalation": 2,
            "frames": [
                {"source": "odom", "target": "base_link", "max_age_sec": -1.0},
                {"source": "base_link", "target": "hand_left", "max_age_sec": -1.0},
            ],
            "timeout_sec": 5.0,
        }
        checker = gate_module.TfAvailabilityChecker("tf_test", cfg)
        node = self._make_node(buffer_mock)
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is True
        assert buffer_mock.can_transform.call_count == 2

    def test_second_frame_fails(self, gate_module, monkeypatch):
        """Fails if second frame can_transform returns False."""
        buffer_mock = self._make_tf_mocks(monkeypatch)
        buffer_mock.can_transform.side_effect = [True, False]

        cfg = {
            "escalation": 1,
            "frames": [
                {"source": "odom", "target": "base_link"},
                {"source": "base_link", "target": "hand_left"},
            ],
            "timeout_sec": 5.0,
        }
        checker = gate_module.TfAvailabilityChecker("tf_test", cfg)
        node = self._make_node(buffer_mock)
        checker.setup(node)

        result = checker.evaluate()
        assert result.passed is False
        assert result.escalation == 1

    def test_registered_in_condition_types(self, gate_module):
        """TfAvailabilityChecker is in the CONDITION_TYPES registry."""
        assert "tf_availability" in gate_module.CONDITION_TYPES
        assert (
            gate_module.CONDITION_TYPES["tf_availability"]
            is gate_module.TfAvailabilityChecker
        )


def _make_import_blocker(blocked_module, monkeypatch):
    """Return an __import__ replacement that blocks a specific module."""
    real_import = (
        __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
    )

    def _blocked_import(name, *args, **kwargs):
        if name == blocked_module:
            raise ImportError(f"Mocked: {name} not available")
        return real_import(name, *args, **kwargs)

    return _blocked_import


# ---------------------------------------------------------------------------
# Debounce wrapper
# ---------------------------------------------------------------------------


class TestDebounce:
    def test_zero_debounce_passes_immediately(self, gate_module):
        inner = MagicMock()
        inner.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )
        inner.name = "c"
        inner.escalation = 2
        inner.config = {}

        wrapper = gate_module.DebouncedChecker(inner, debounce_sec=0.0)
        result = wrapper.evaluate()
        assert result.passed is True

    def test_debounce_holds_until_stable(self, gate_module):
        inner = MagicMock()
        inner.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )
        inner.name = "c"
        inner.escalation = 2
        inner.config = {}

        wrapper = gate_module.DebouncedChecker(inner, debounce_sec=2.0)

        # First eval: starts debounce timer, reports failing
        result = wrapper.evaluate()
        assert result.passed is False
        assert "debouncing" in result.reason

    def test_debounce_passes_after_duration(self, gate_module):
        inner = MagicMock()
        inner.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )
        inner.name = "c"
        inner.escalation = 2
        inner.config = {}

        wrapper = gate_module.DebouncedChecker(inner, debounce_sec=0.5)

        # Start timer
        wrapper.evaluate()
        # Push timer into the past
        wrapper._passing_since = time.monotonic() - 1.0

        result = wrapper.evaluate()
        assert result.passed is True

    def test_debounce_resets_on_flap(self, gate_module):
        inner = MagicMock()
        inner.name = "c"
        inner.escalation = 2
        inner.config = {}

        wrapper = gate_module.DebouncedChecker(inner, debounce_sec=2.0)

        # Pass → start timer
        inner.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )
        wrapper.evaluate()
        assert wrapper._passing_since is not None

        # Fail → reset timer
        inner.evaluate.return_value = gate_module.ConditionResult(
            "c", False, "bad", escalation=2
        )
        result = wrapper.evaluate()
        assert result.passed is False
        assert wrapper._passing_since is None

        # Pass again → new timer starts
        inner.evaluate.return_value = gate_module.ConditionResult(
            "c", True, "ok", escalation=0
        )
        result = wrapper.evaluate()
        assert result.passed is False  # still debouncing
        assert wrapper._passing_since is not None


# ---------------------------------------------------------------------------
# Expression compiler (pure Python, no ROS mocking needed)
# ---------------------------------------------------------------------------


class TestCompileCondition:
    @pytest.fixture(autouse=True)
    def _import(self):
        from galaxis_core.recording_gate import compile_condition

        self.compile_condition = compile_condition

    def test_simple_comparison(self):
        code = self.compile_condition("msg.data < 5.0")
        assert code is not None

    def test_nested_attribute(self):
        code = self.compile_condition(
            "msg.pose.position.x > -1.0 and msg.pose.position.x < 5.0"
        )
        assert code is not None

    def test_boolean_not(self):
        code = self.compile_condition("not msg.data")
        assert code is not None

    def test_subscript(self):
        code = self.compile_condition("msg.effort[0] < 10.0")
        assert code is not None

    def test_arithmetic(self):
        code = self.compile_condition("msg.velocity * 2.0 > 10.0")
        assert code is not None

    def test_rejects_function_call(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("len(msg.data)")

    def test_rejects_import(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("__import__('os')")

    def test_rejects_dunder(self):
        with pytest.raises(ValueError, match="Private attribute"):
            self.compile_condition("msg.__class__")

    def test_rejects_unknown_variable(self):
        with pytest.raises(ValueError, match="Unknown variable"):
            self.compile_condition("x + 1")

    def test_rejects_syntax_error(self):
        with pytest.raises(ValueError, match="Invalid expression"):
            self.compile_condition("msg.data <")

    def test_compiled_code_evaluates_true(self):
        code = self.compile_condition("msg.value < 32.0 and msg.value > 0.5")
        msg = type("Msg", (), {"value": 10.0})()
        assert eval(code, {"__builtins__": {}}, {"msg": msg}) is True

    def test_compiled_code_evaluates_false(self):
        code = self.compile_condition("msg.value < 32.0 and msg.value > 0.5")
        msg = type("Msg", (), {"value": 50.0})()
        assert eval(code, {"__builtins__": {}}, {"msg": msg}) is False

    def test_is_none(self):
        code = self.compile_condition("msg.data is None")
        msg = type("Msg", (), {"data": None})()
        assert eval(code, {"__builtins__": {}}, {"msg": msg}) is True

    def test_is_not_none(self):
        code = self.compile_condition("msg.data is not None")
        msg = type("Msg", (), {"data": 5})()
        assert eval(code, {"__builtins__": {}}, {"msg": msg}) is True

    def test_membership_in_list(self):
        code = self.compile_condition("msg.status in [1, 2, 3]")
        msg = type("Msg", (), {"status": 2})()
        assert eval(code, {"__builtins__": {}}, {"msg": msg}) is True
        msg.status = 99
        assert eval(code, {"__builtins__": {}}, {"msg": msg}) is False

    def test_membership_not_in(self):
        code = self.compile_condition("msg.level not in (0, 3)")
        msg = type("Msg", (), {"level": 1})()
        assert eval(code, {"__builtins__": {}}, {"msg": msg}) is True

    # --- Security: additional attack vectors ---

    def test_rejects_lambda(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("(lambda: msg.data)()")

    def test_rejects_list_comprehension(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("[x for x in msg.data]")

    def test_rejects_walrus(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("(x := msg.data)")

    def test_rejects_fstring(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("f'{msg.data}'")

    def test_rejects_starred(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("(*msg.data,)")

    def test_rejects_subclass_escape(self):
        with pytest.raises(ValueError, match="Unsafe node|Private attribute"):
            self.compile_condition("msg.__class__.__subclasses__()")

    def test_rejects_getattr(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("getattr(msg, 'data')")

    def test_rejects_exec(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("exec('import os')")

    def test_rejects_type_call(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("type(msg)")

    def test_rejects_generator(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("sum(x for x in msg.data)")

    def test_rejects_dict_literal(self):
        with pytest.raises(ValueError, match="Unsafe node"):
            self.compile_condition("{msg.data: 1}")

    def test_builtins_blocked_at_eval(self):
        code = self.compile_condition("msg.data > 0")
        msg = type("Msg", (), {"data": 1})()
        env = {"__builtins__": {}}
        eval(code, env, {"msg": msg})
        assert env["__builtins__"] == {}

    # --- Performance benchmarks ---

    def test_eval_performance(self):
        """Compiled condition eval: 10k iterations should take < 100ms."""
        code = self.compile_condition(
            "msg.pose.position.x > -2.0 and msg.pose.position.x < 2.0 "
            "and msg.pose.position.y > -1.0"
        )
        pos = type("Pos", (), {"x": 0.5, "y": 0.3})()
        pose = type("Pose", (), {"position": pos})()
        msg = type("Msg", (), {"pose": pose})()
        env = {"__builtins__": {}}

        for _ in range(100):  # warm up
            eval(code, env, {"msg": msg})

        start = time.monotonic()
        for _ in range(10_000):
            eval(code, env, {"msg": msg})
        elapsed = time.monotonic() - start

        assert elapsed < 0.1, (
            f"10k evals took {elapsed:.3f}s ({elapsed / 10_000 * 1e6:.1f}us each)"
        )

    def test_compile_performance(self):
        """compile_condition: 1k compilations should take < 500ms."""
        expr = "msg.effort[0] < 10.0 and msg.effort[1] < 10.0 and msg.velocity > 0"

        start = time.monotonic()
        for _ in range(1_000):
            self.compile_condition(expr)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, (
            f"1k compilations took {elapsed:.3f}s ({elapsed / 1_000 * 1e6:.1f}us each)"
        )


# ---------------------------------------------------------------------------
# extract_msg_refs
# ---------------------------------------------------------------------------


class TestExtractMsgRefs:
    """Verify AST-based extraction of msg.* references from expressions."""

    def setup_method(self):
        from galaxis_core.recording_gate import extract_msg_refs

        self.extract = extract_msg_refs

    def test_simple_attribute(self):
        assert self.extract("msg.data") == ["msg.data"]

    def test_nested_attribute(self):
        assert self.extract("msg.pose.position.x") == ["msg.pose.position.x"]

    def test_subscript(self):
        assert self.extract("msg.effort[0]") == ["msg.effort[0]"]

    def test_compound_and(self):
        refs = self.extract("msg.pose.position.x > 0 and msg.pose.orientation.w < -0.5")
        assert "msg.pose.position.x" in refs
        assert "msg.pose.orientation.w" in refs
        assert len(refs) == 2

    def test_boolean_not(self):
        assert self.extract("not msg.data") == ["msg.data"]

    def test_no_refs(self):
        assert self.extract("True") == []

    def test_deduplicates(self):
        refs = self.extract("msg.data > 0 and msg.data < 10")
        assert refs == ["msg.data"]

    def test_leaf_only(self):
        """Only leaf references are returned, not intermediate prefixes."""
        refs = self.extract("msg.pose.position.x > 0")
        assert "msg.pose" not in refs
        assert "msg.pose.position" not in refs
        assert "msg.pose.position.x" in refs


# ---------------------------------------------------------------------------
# _format_reason (via TopicConditionChecker)
# ---------------------------------------------------------------------------


class TestFormatReason:
    """Verify auto-enriched and template-based condition failure reasons."""

    def _make_checker(self, gate_module, condition, reason=""):
        cfg = {
            "topic": "/test",
            "condition": condition,
            "timeout_sec": 5.0,
        }
        if reason:
            cfg["reason"] = reason
        checker = MagicMock()
        checker.name = "test_cond"
        checker.config = cfg
        checker.escalation = 2

        # Build a real TopicConditionChecker to test _format_reason
        from galaxis_core.recording_gate_node import TopicConditionChecker

        real = TopicConditionChecker("test_cond", cfg)
        # Run setup parts that don't need ROS
        from galaxis_core.recording_gate import (
            compile_condition,
            extract_msg_refs,
        )

        real._condition_code = compile_condition(condition)
        real._condition_expr = condition
        refs = extract_msg_refs(condition)
        real._condition_ref_codes = [
            (ref, compile(ref, "<reason>", "eval")) for ref in refs
        ]
        real._reason_template = reason
        return real

    def test_auto_enrich_simple_bool(self):
        checker = self._make_checker(None, "not msg.data")
        msg = MagicMock()
        msg.data = True
        result = checker._format_reason(msg)
        assert "msg.data=True" in result
        assert "not msg.data" in result

    def test_auto_enrich_nested_attrs(self):
        checker = self._make_checker(None, "msg.pose.position.x > 10")
        msg = MagicMock()
        msg.pose.position.x = 3.2
        result = checker._format_reason(msg)
        assert "msg.pose.position.x=3.2" in result

    def test_auto_enrich_multiple_refs(self):
        checker = self._make_checker(
            None, "msg.pose.position.x > 0 and msg.pose.orientation.w < -0.5"
        )
        msg = MagicMock()
        msg.pose.position.x = 5.0
        msg.pose.orientation.w = 0.3
        result = checker._format_reason(msg)
        assert "msg.pose.position.x=5.0" in result
        assert "msg.pose.orientation.w=0.3" in result

    def test_auto_enrich_eval_error(self):
        checker = self._make_checker(None, "msg.foo.bar > 0")
        msg = MagicMock()
        del msg.foo  # force AttributeError
        result = checker._format_reason(msg)
        assert "msg.foo.bar=?" in result

    def test_no_condition_returns_default(self):
        """Checker with no condition expression returns simple default."""
        from galaxis_core.recording_gate_node import TopicConditionChecker

        cfg = {"topic": "/test", "timeout_sec": 5.0}
        real = TopicConditionChecker("test_cond", cfg)
        real._condition_code = None
        real._condition_expr = ""
        real._condition_ref_codes = []
        real._reason_template = ""
        result = real._format_reason(MagicMock())
        assert result == "condition failed: "

    def test_template_with_msg(self):
        checker = self._make_checker(
            None, "not msg.data", reason="E-stop pressed (raw={msg.data})"
        )
        msg = MagicMock()
        msg.data = True
        result = checker._format_reason(msg)
        assert result == "E-stop pressed (raw=True)"

    def test_template_with_condition(self):
        checker = self._make_checker(
            None, "msg.data < 50", reason="Limit exceeded: {condition}"
        )
        result = checker._format_reason(MagicMock())
        assert result == "Limit exceeded: msg.data < 50"

    def test_template_with_detail(self):
        checker = self._make_checker(
            None, "msg.data < 50", reason="Limit exceeded ({detail})"
        )
        msg = MagicMock()
        msg.data = 72
        result = checker._format_reason(msg)
        assert result == "Limit exceeded (msg.data=72)"

    def test_template_with_name(self):
        checker = self._make_checker(None, "msg.data", reason="[{name}] failed")
        result = checker._format_reason(MagicMock())
        assert result == "[test_cond] failed"

    def test_template_combined(self):
        checker = self._make_checker(
            None,
            "msg.effort[0] < 50.0",
            reason="Joint limit exceeded: {condition} ({detail})",
        )
        msg = MagicMock()
        msg.effort = [72.3]
        result = checker._format_reason(msg)
        assert "msg.effort[0] < 50.0" in result
        assert "msg.effort[0]=72.3" in result

    def test_template_format_spec(self):
        checker = self._make_checker(
            None, "msg.data < 50", reason="Value: {msg.data:.1f}"
        )
        msg = MagicMock()
        msg.data = 72.345
        result = checker._format_reason(msg)
        assert result == "Value: 72.3"

    def test_malformed_template_falls_back(self):
        checker = self._make_checker(
            None, "msg.data < 50", reason="Bad template: {nonexistent}"
        )
        msg = MagicMock()
        msg.data = 72
        result = checker._format_reason(msg)
        # Falls back to auto-enriched
        assert "condition failed:" in result
        assert "msg.data=72" in result

    def test_empty_template_uses_auto(self):
        checker = self._make_checker(None, "msg.data < 50", reason="")
        msg = MagicMock()
        msg.data = 72
        result = checker._format_reason(msg)
        assert "condition failed: msg.data < 50 (msg.data=72)" in result


def test_pico_timing_blocks_start_only_and_recovers(gate_module, monkeypatch):
    from types import SimpleNamespace
    checker=gate_module.TopicConditionChecker('left_clock',dict(topic='/pico/controller/left/state',pico_timing=True,escalation=1))
    checker.setup(MagicMock());checker._subscribed=True
    for i in range(200):
        now=i/90;monkeypatch.setattr(gate_module.time,'monotonic',lambda:now)
        checker._cb(SimpleNamespace(device_time_ns=10**18+round(now*2e9),pc_monotonic_ns=10**12+round(now*1e9),seq=i,pico_id='pico'))
    result=checker.evaluate()
    assert not result.passed and result.escalation==1 and '积压' in result.reason
    checker.invalidate();checker._subscribed=True
    for i in range(200):
        now=3+i/90;monkeypatch.setattr(gate_module.time,'monotonic',lambda:now)
        checker._cb(SimpleNamespace(device_time_ns=10**18+round(now*1e9),pc_monotonic_ns=10**12+round(now*1e9),seq=i,pico_id='pico'))
    assert checker.evaluate().passed
