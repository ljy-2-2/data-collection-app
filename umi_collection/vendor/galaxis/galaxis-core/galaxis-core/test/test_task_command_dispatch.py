"""Unit tests for TaskCommandDispatchNode.

ROS2 and sensor_msgs dependencies are mocked via ``conftest.mock_rclpy``.
"""

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def dispatch_node(mock_rclpy):
    """Create a TaskCommandDispatchNode."""
    from galaxis_core.task_command_dispatch_node import TaskCommandDispatchNode

    node = TaskCommandDispatchNode()
    return node


def set_clock_time(node, seconds):
    """Patch get_clock() to return a specific time."""
    clock = MagicMock()
    now_mock = MagicMock()
    now_mock.seconds_nanoseconds.return_value = (seconds, 0)
    now_mock.nanoseconds = int(seconds * 1e9)
    clock.now.return_value = now_mock
    node.get_clock = MagicMock(return_value=clock)


def _make_joy_msg(buttons):
    """Create a fake Joy message with given buttons."""
    from sensor_msgs.msg import Joy

    msg = Joy()
    msg.buttons = buttons
    return msg


# ===================================================================
# TestSafeButton
# ===================================================================


class TestSafeButton:
    """Tests for ``_safe_button`` static method."""

    def test_valid_pressed(self, dispatch_node):
        from galaxis_core.task_command_dispatch_node import (
            TaskCommandDispatchNode,
        )

        assert TaskCommandDispatchNode._safe_button([0, 1, 0], 1) == 1

    def test_valid_not_pressed(self, dispatch_node):
        from galaxis_core.task_command_dispatch_node import (
            TaskCommandDispatchNode,
        )

        assert TaskCommandDispatchNode._safe_button([0, 1, 0], 0) == 0

    def test_out_of_range(self, dispatch_node):
        from galaxis_core.task_command_dispatch_node import (
            TaskCommandDispatchNode,
        )

        assert TaskCommandDispatchNode._safe_button([0, 1], 5) == 0

    def test_normalizes_truthy(self, dispatch_node):
        from galaxis_core.task_command_dispatch_node import (
            TaskCommandDispatchNode,
        )

        assert TaskCommandDispatchNode._safe_button([42], 0) == 1


# ===================================================================
# TestRisingEdge
# ===================================================================


class TestRisingEdge:
    """Tests for ``_rising_edge``."""

    def test_0_to_1_fires(self, dispatch_node):
        set_clock_time(dispatch_node, 10.0)
        dispatch_node.prev_buttons = [0, 0, 0, 0]
        result = dispatch_node._rising_edge(2, [0, 0, 1, 0])
        assert result is True

    def test_1_to_1_no_fire(self, dispatch_node):
        set_clock_time(dispatch_node, 10.0)
        dispatch_node.prev_buttons = [0, 0, 1, 0]
        result = dispatch_node._rising_edge(2, [0, 0, 1, 0])
        assert result is False

    def test_1_to_0_no_fire(self, dispatch_node):
        set_clock_time(dispatch_node, 10.0)
        dispatch_node.prev_buttons = [0, 0, 1, 0]
        result = dispatch_node._rising_edge(2, [0, 0, 0, 0])
        assert result is False

    def test_debounce_blocks_rapid(self, dispatch_node):
        # First press at t=10
        set_clock_time(dispatch_node, 10.0)
        dispatch_node.prev_buttons = [0, 0, 0, 0]
        assert dispatch_node._rising_edge(2, [0, 0, 1, 0]) is True

        # Second press at t=10.1 (within debounce_sec=0.25)
        set_clock_time(dispatch_node, 10.1)
        dispatch_node.prev_buttons = [0, 0, 0, 0]
        assert dispatch_node._rising_edge(2, [0, 0, 1, 0]) is False

    def test_first_press_no_prev(self, dispatch_node):
        set_clock_time(dispatch_node, 10.0)
        dispatch_node.prev_buttons = None
        result = dispatch_node._rising_edge(2, [0, 0, 1, 0])
        assert result is True


# ===================================================================
# TestTaskProcessCallback
# ===================================================================


class TestTaskProcessCallback:
    """Tests for ``task_process_callback``."""

    def test_no_joy_noop(self, dispatch_node):
        dispatch_node.joy_msg = None
        dispatch_node.task_process_callback()
        # No assertions needed — just verify it doesn't crash
        dispatch_node.task_accept_client.call_async.assert_not_called()

    def test_accept_button_fires(self, dispatch_node):
        set_clock_time(dispatch_node, 10.0)
        dispatch_node.prev_buttons = [0, 0, 0, 0]

        # Create joy msg with accept button (index 2) pressed
        msg = _make_joy_msg([0, 0, 1, 0])
        dispatch_node.joy_msg = msg

        dispatch_node.task_process_callback()

        dispatch_node.task_accept_client.call_async.assert_called_once()

    def test_next_round_button_fires(self, dispatch_node):
        set_clock_time(dispatch_node, 10.0)
        dispatch_node.prev_buttons = [0, 0, 0, 0]
        dispatch_node.joy_next_round_button = 0
        dispatch_node.joy_cancel_episode_button = -1

        dispatch_node.joy_msg = _make_joy_msg([1, 0, 0, 0])
        dispatch_node.task_process_callback()

        dispatch_node.task_next_round_client.call_async.assert_called_once()
        dispatch_node.task_cancel_episode_client.call_async.assert_not_called()

    def test_inflight_prevents_duplicate(self, dispatch_node):
        set_clock_time(dispatch_node, 10.0)
        dispatch_node.prev_buttons = [0, 0, 0, 0]
        dispatch_node.inflight["accept"] = True

        msg = _make_joy_msg([0, 0, 1, 0])
        dispatch_node.joy_msg = msg

        dispatch_node.task_process_callback()

        dispatch_node.task_accept_client.call_async.assert_not_called()

    def test_prev_buttons_updated(self, dispatch_node):
        set_clock_time(dispatch_node, 10.0)
        dispatch_node.prev_buttons = [0, 0, 0, 0]

        msg = _make_joy_msg([1, 0, 1, 0])
        dispatch_node.joy_msg = msg

        dispatch_node.task_process_callback()

        assert dispatch_node.prev_buttons == [1, 0, 1, 0]

    def test_multiple_buttons_independent(self, dispatch_node):
        set_clock_time(dispatch_node, 10.0)
        dispatch_node.prev_buttons = [0, 0, 0, 0]

        # Accept (2) and reject (3) both pressed
        msg = _make_joy_msg([0, 0, 1, 1])
        dispatch_node.joy_msg = msg

        dispatch_node.task_process_callback()

        dispatch_node.task_accept_client.call_async.assert_called_once()
        dispatch_node.task_reject_client.call_async.assert_called_once()


# ===================================================================
# TestFinishCallback
# ===================================================================


class TestFinishCallback:
    """Tests for ``_finish``."""

    def test_clears_inflight(self, dispatch_node):
        dispatch_node.inflight["accept"] = True
        future = MagicMock()
        result = MagicMock()
        result.success = True
        result.message = "ok"
        future.result.return_value = result

        dispatch_node._finish("accept", future, "Task accepted", "Failed")

        assert dispatch_node.inflight["accept"] is False
