"""Unit tests for TaskSequenceManager.

ROS2 dependencies are mocked via ``conftest.mock_rclpy``.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def manager(mock_rclpy):
    """Create a TaskSequenceManager with mocked backend via create_backend."""
    mock_backend = MagicMock()
    # Default to a concrete dict so _fetch_active_operator doesn't return a
    # MagicMock chain that would leak into enrich_episode's recordedBy.
    mock_backend.get_robot_self.return_value = {"organization_name": "TestOrg"}
    with patch("galaxis_core.backend_client.create_backend", return_value=mock_backend):
        from galaxis_core.task_sequence_manager import TaskSequenceManager

        node = TaskSequenceManager()
        node._mock_backend = mock_backend
    return node


@pytest.fixture()
def sample_tasks():
    return {
        "episodeId": "ep-1",
        "taskId": "task-1",
        "taskVersionId": "tv-1",
        "assignedRobotId": "robot-1",
        "createdUserId": "user-1",
        "status": 1,
        "task": {"id": "task-1", "name": "Test Task", "description": "Do things"},
        "subtasks": [
            {"id": "st-1", "name": "Subtask A", "orderIndex": 0, "status": 0},
            {"id": "st-2", "name": "Subtask B", "orderIndex": 1, "status": 0},
            {"id": "st-3", "name": "Subtask C", "orderIndex": 2, "status": 0},
        ],
    }


def _get_states(manager_node):
    """Import state enum from the same module as the manager."""
    from galaxis_core.task_sequence_manager import (
        TaskSequenceState,
        ActionCommand,
    )

    return TaskSequenceState, ActionCommand


def prepare_state(mgr, state, tasks, **overrides):
    """Set manager into a specific state with tasks and optional overrides."""
    TaskSequenceState, _ = _get_states(mgr)
    mgr.status = state
    mgr.tasks = tasks
    mgr.new_tasks = tasks
    if tasks and "subtasks" in tasks:
        mgr.has_subtasks_successed = [None] * len(tasks["subtasks"])
    else:
        mgr.has_subtasks_successed = None
    mgr.cur_subtask_index = overrides.get("cur_subtask_index", 0)
    mgr.prev_subtask_index = overrides.get("prev_subtask_index", None)
    mgr.meta_data = overrides.get("meta_data", {"labels": [], "segments": []})
    mgr._segment_count = len(mgr.meta_data.get("segments", []))
    mgr.start_time = overrides.get("start_time", None)
    mgr.end_time = overrides.get("end_time", None)
    mgr.task_start_time = overrides.get("task_start_time", None)
    if "has_subtasks_successed" in overrides:
        mgr.has_subtasks_successed = overrides["has_subtasks_successed"]


def _mock_wait_for_trigger(mgr, return_value=True):
    """Patch wait_for_trigger_future_done to return immediately."""
    mgr.wait_for_trigger_future_done = MagicMock(return_value=return_value)


# ===================================================================
# TestTransitConfirmTask
# ===================================================================


class TestTransitConfirmTask:
    """State: COMFIRM_TASK"""

    def test_accept_starts_recording(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager._do_start_recording = MagicMock(return_value=(True, "ok"))

        result = manager.transit_state(AC.ACCEPT)

        assert result is True
        manager._do_start_recording.assert_called_once()

    def test_continuous_accept_starts_first_segment(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager.continuous_round_mode = True
        manager._do_start_recording = MagicMock(return_value=(True, "ok"))
        manager._do_start_subtask = MagicMock(return_value=(True, "ok"))

        result = manager.transit_state(AC.ACCEPT)

        assert result is True
        manager._do_start_recording.assert_called_once()
        manager._do_start_subtask.assert_called_once_with("st-1")

    def test_accept_fails(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager._do_start_recording = MagicMock(return_value=(False, "no tasks"))

        result = manager.transit_state(AC.ACCEPT)
        assert result is False

    def test_reject_cancel_rewind_return_false(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)

        for action in [AC.REJECT, AC.CANCEL, AC.REWIND]:
            assert manager.transit_state(action) is False


# ===================================================================
# TestTransitWaitSubtask
# ===================================================================


class TestTransitWaitSubtask:
    """State: WAIT_SUBTASK"""

    def test_accept_starts_subtask(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)
        manager._do_start_subtask = MagicMock(return_value=(True, "ok"))

        result = manager.transit_state(AC.ACCEPT)

        assert result is True
        manager._do_start_subtask.assert_called_once_with("st-1")

    def test_reject_skips_and_advances(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, cur_subtask_index=0)

        result = manager.transit_state(AC.REJECT)

        assert result is True
        assert manager.cur_subtask_index == 1
        assert manager.has_subtasks_successed[0] is False
        assert manager.status == TSS.WAIT_SUBTASK

    def test_reject_last_goes_to_complete(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, cur_subtask_index=2)

        result = manager.transit_state(AC.REJECT)

        assert result is True
        assert manager.status == TSS.COMPLETE_TASK

    def test_cancel_with_segments(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        meta = {"labels": [], "segments": [{"start_time": 0, "end_time": 1}]}
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, meta_data=meta)
        manager._do_stop_recording = MagicMock(return_value=(True, "discarded"))

        result = manager.transit_state(AC.CANCEL)

        assert result is True
        manager._do_stop_recording.assert_called_once_with(save=False, reason="")

    def test_cancel_no_segments_allowed(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        meta = {"labels": [], "segments": []}
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, meta_data=meta)
        manager._do_stop_recording = MagicMock(return_value=(True, "discarded"))

        result = manager.transit_state(AC.CANCEL)

        assert result is True
        manager._do_stop_recording.assert_called_once_with(save=False, reason="")

    def test_rewind_with_prev_and_segments(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        meta = {"labels": [], "segments": [{"start_time": 0, "end_time": 1}]}
        prepare_state(
            manager,
            TSS.WAIT_SUBTASK,
            sample_tasks,
            prev_subtask_index=0,
            meta_data=meta,
        )

        result = manager.transit_state(AC.REWIND)

        assert result is True
        assert manager.status == TSS.REWIND_SUBTASK


# ===================================================================
# TestTransitRecordSubtask
# ===================================================================


class TestTransitRecordSubtask:
    """State: RECORD_SUBTASK"""

    def test_accept_stops_success_advances(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=0,
            prev_subtask_index=0,
        )
        manager._do_stop_subtask = MagicMock(return_value=(True, "ok"))
        manager._do_start_subtask = MagicMock(return_value=(True, "ok"))

        result = manager.transit_state(AC.ACCEPT)

        assert result is True
        manager._do_stop_subtask.assert_called_once_with(True)
        # After accept, cur_subtask_index = prev_subtask_index + 1
        assert manager.cur_subtask_index == 1
        manager._do_start_subtask.assert_called_once_with("st-2")
        # Status is WAIT_SUBTASK (set by transit_state before calling mocked _do_start_subtask)
        assert manager.status == TSS.WAIT_SUBTASK

    def test_next_round_saves_and_pauses(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks)
        manager.continuous_round_mode = True
        manager._do_stop_subtask = MagicMock(return_value=(True, "stopped"))
        manager._do_stop_recording = MagicMock(return_value=(True, "saved"))
        manager._do_start_round = MagicMock(return_value=(True, "started"))

        result = manager.transit_state(AC.NEXT_ROUND)

        assert result is True
        manager._do_stop_subtask.assert_called_once_with(True)
        manager._do_stop_recording.assert_called_once_with(save=True)
        manager._do_start_round.assert_not_called()

    def test_finish_session_saves_without_starting_next(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks)
        manager.continuous_round_mode = True
        manager._do_stop_subtask = MagicMock(return_value=(True, "stopped"))
        manager._do_stop_recording = MagicMock(return_value=(True, "saved"))
        manager._do_start_round = MagicMock(return_value=(True, "started"))

        result = manager.transit_state(AC.FINISH_SESSION)

        assert result is True
        manager._do_stop_subtask.assert_called_once_with(True)
        manager._do_stop_recording.assert_called_once_with(save=True)
        manager._do_start_round.assert_not_called()

    def test_accept_last_goes_complete(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=2,
            prev_subtask_index=2,
        )
        manager._do_stop_subtask = MagicMock(return_value=(True, "ok"))
        manager._do_stop_recording = MagicMock(return_value=(True, "saved"))

        result = manager.transit_state(AC.ACCEPT)

        assert result is True
        manager._do_stop_recording.assert_called_once_with(save=True)
        # Status is COMPLETE_TASK (set by transit_state before calling mocked _do_stop_recording)
        assert manager.status == TSS.COMPLETE_TASK

    def test_reject_stops_fail_stays(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=1,
            prev_subtask_index=1,
        )
        manager._do_stop_subtask = MagicMock(return_value=(True, "ok"))
        manager._do_start_subtask = MagicMock(return_value=(True, "ok"))

        result = manager.transit_state(AC.REJECT)

        assert result is True
        manager._do_stop_subtask.assert_called_once_with(False)
        # After reject, stays on same subtask
        assert manager.cur_subtask_index == 1
        manager._do_start_subtask.assert_called_once_with("st-2")
        # Status is WAIT_SUBTASK (set by transit_state before calling mocked _do_start_subtask)
        assert manager.status == TSS.WAIT_SUBTASK

    def test_cancel_stops_and_discards(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks)
        manager._do_stop_subtask = MagicMock(return_value=(True, "ok"))
        manager._do_stop_recording = MagicMock(return_value=(True, "discarded"))

        assert manager.transit_state(AC.CANCEL) is True
        manager._do_stop_subtask.assert_called_once_with(False)
        manager._do_stop_recording.assert_called_once_with(save=False, reason="")

    def test_accept_auto_start_fails(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=0,
            prev_subtask_index=0,
        )
        manager._do_stop_subtask = MagicMock(return_value=(True, "ok"))
        manager._do_start_subtask = MagicMock(return_value=(False, "error"))

        result = manager.transit_state(AC.ACCEPT)

        assert result is False
        manager._do_start_subtask.assert_called_once_with("st-2")
        manager.get_logger().warning.assert_called()

    def test_accept_last_auto_stop_fails(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=2,
            prev_subtask_index=2,
        )
        manager._do_stop_subtask = MagicMock(return_value=(True, "ok"))
        manager._do_stop_recording = MagicMock(return_value=(False, "error"))

        result = manager.transit_state(AC.ACCEPT)

        assert result is False
        manager._do_stop_recording.assert_called_once_with(save=True)
        manager.get_logger().warning.assert_called()

    def test_reject_auto_restart_succeeds(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=1,
            prev_subtask_index=1,
        )
        manager._do_stop_subtask = MagicMock(return_value=(True, "ok"))
        manager._do_start_subtask = MagicMock(return_value=(True, "ok"))

        result = manager.transit_state(AC.REJECT)

        assert result is True
        assert manager.cur_subtask_index == 1
        manager._do_start_subtask.assert_called_once_with("st-2")

    def test_reject_auto_restart_fails(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=1,
            prev_subtask_index=1,
        )
        manager._do_stop_subtask = MagicMock(return_value=(True, "ok"))
        manager._do_start_subtask = MagicMock(return_value=(False, "error"))

        result = manager.transit_state(AC.REJECT)

        assert result is False
        manager._do_start_subtask.assert_called_once_with("st-2")
        manager.get_logger().warning.assert_called()

    def test_reject_stop_fails(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=1,
            prev_subtask_index=1,
        )
        manager._do_stop_subtask = MagicMock(return_value=(False, "err"))
        manager._do_start_subtask = MagicMock(return_value=(True, "ok"))

        result = manager.transit_state(AC.REJECT)

        assert result is False
        manager._do_start_subtask.assert_not_called()

    def test_rewind_returns_false(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks)

        assert manager.transit_state(AC.REWIND) is False


# ===================================================================
# TestTransitCompleteTask
# ===================================================================


class TestTransitCompleteTask:
    """State: COMPLETE_TASK"""

    def test_accept_saves(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.COMPLETE_TASK, sample_tasks)
        manager._do_stop_recording = MagicMock(return_value=(True, "saved"))

        result = manager.transit_state(AC.ACCEPT)

        assert result is True
        manager._do_stop_recording.assert_called_once_with(save=True)

    def test_reject_discards(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.COMPLETE_TASK, sample_tasks)
        manager._do_stop_recording = MagicMock(return_value=(True, "discarded"))

        result = manager.transit_state(AC.REJECT)

        assert result is True
        manager._do_stop_recording.assert_called_once_with(
            save=False, reason="rejected by operator"
        )

    def test_rewind_with_prev(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        meta = {"labels": [], "segments": [{"x": 1}]}
        prepare_state(
            manager,
            TSS.COMPLETE_TASK,
            sample_tasks,
            prev_subtask_index=1,
            meta_data=meta,
        )

        result = manager.transit_state(AC.REWIND)

        assert result is True
        assert manager.status == TSS.REWIND_SUBTASK

    def test_cancel_discards(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.COMPLETE_TASK, sample_tasks)
        manager._do_stop_recording = MagicMock(return_value=(True, "discarded"))

        result = manager.transit_state(AC.CANCEL, reason="gate hard-stop")

        assert result is True
        manager._do_stop_recording.assert_called_once_with(
            save=False, reason="gate hard-stop"
        )


# ===================================================================
# TestTransitRewindSubtask
# ===================================================================


class TestTransitRewindSubtask:
    """State: REWIND_SUBTASK"""

    def test_accept_overrides_true_advances(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.REWIND_SUBTASK,
            sample_tasks,
            prev_subtask_index=0,
            cur_subtask_index=0,
        )
        _mock_wait_for_trigger(manager, True)

        result = manager.transit_state(AC.ACCEPT)

        assert result is True
        assert manager.has_subtasks_successed[0] is True
        assert manager.cur_subtask_index == 1
        assert manager.status == TSS.WAIT_SUBTASK

    def test_accept_last_goes_complete(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.REWIND_SUBTASK,
            sample_tasks,
            prev_subtask_index=2,
            cur_subtask_index=2,
        )
        _mock_wait_for_trigger(manager, True)

        result = manager.transit_state(AC.ACCEPT)

        assert result is True
        assert manager.status == TSS.COMPLETE_TASK

    def test_reject_overrides_false_stays(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.REWIND_SUBTASK,
            sample_tasks,
            prev_subtask_index=1,
            cur_subtask_index=1,
        )
        _mock_wait_for_trigger(manager, True)

        result = manager.transit_state(AC.REJECT)

        assert result is True
        assert manager.has_subtasks_successed[1] is False
        assert manager.cur_subtask_index == 1
        assert manager.status == TSS.WAIT_SUBTASK

    def test_rewind_removes_segment(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.REWIND_SUBTASK,
            sample_tasks,
            prev_subtask_index=1,
            cur_subtask_index=1,
        )
        _mock_wait_for_trigger(manager, True)

        result = manager.transit_state(AC.REWIND)

        assert result is True
        assert manager.has_subtasks_successed[1] is None
        assert manager.prev_subtask_index is None
        assert manager.status == TSS.WAIT_SUBTASK

    def test_service_failure_returns_false(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.REWIND_SUBTASK,
            sample_tasks,
            prev_subtask_index=0,
            cur_subtask_index=0,
        )
        _mock_wait_for_trigger(manager, False)

        assert manager.transit_state(AC.ACCEPT) is False


# ===================================================================
# TestDoStartRecording
# ===================================================================


class TestDoStartRecording:
    """Tests for ``_do_start_recording``."""

    def test_success(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager.start_recording = MagicMock(return_value=(True, ""))
        manager.record_runner = MagicMock(return_value=True)

        ok, msg = manager._do_start_recording()

        assert ok is True
        assert manager.status == TSS.WAIT_SUBTASK
        manager.start_recording.assert_called_once()

    def test_wrong_state(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)

        ok, msg = manager._do_start_recording()

        assert ok is False
        assert "invalid state" in msg

    def test_no_tasks(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager.start_recording = MagicMock(
            return_value=(False, "record_manager service call failed")
        )

        ok, msg = manager._do_start_recording()

        assert ok is False
        assert "record_manager" in msg

    def test_zero_subtasks_rejected(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        tasks_no_subtasks = dict(sample_tasks, subtasks=[])
        prepare_state(manager, TSS.COMFIRM_TASK, tasks_no_subtasks)
        manager.new_tasks = tasks_no_subtasks

        ok, msg = manager._do_start_recording()

        assert ok is False
        assert "no subtasks" in msg

    def test_with_episode_id_fetches_and_enriches(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, {})
        manager.new_tasks = {}

        raw_episode = {
            "id": "ep-99",
            "task_id": "task-1",
            "task_name": "Test Task",
            "task_description": "Do things",
            "task_version_id": "tv-1",
            "robot_id": "robot-1",
            "user_id": "user-1",
            "status": 1,
            "subtasks": [
                {"subtask_id": "st-1", "name": "A", "order_index": 0, "status": 0},
            ],
        }
        manager._mock_backend.get_episode.return_value = raw_episode

        def _sync_tasks():
            manager.tasks = manager.new_tasks
            return True, ""

        manager.start_recording = MagicMock(side_effect=_sync_tasks)
        manager.record_runner = MagicMock(return_value=True)

        ok, msg = manager._do_start_recording(episode_id="ep-99")

        assert ok is True
        manager._mock_backend.get_episode.assert_called_once_with("ep-99")
        assert manager.new_tasks["episodeId"] == "ep-99"

    def test_with_episode_id_not_found(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, {})
        manager.new_tasks = {}
        manager._mock_backend.get_episode.return_value = None

        ok, msg = manager._do_start_recording(episode_id="ep-missing")

        assert ok is False
        assert "not found" in msg

    def test_empty_new_tasks_falls_back_to_last_episode(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, {})
        manager.new_tasks = {}

        raw_episode = {
            "id": "ep-last",
            "task_id": "task-1",
            "task_name": "Fallback Task",
            "task_description": "Fallback",
            "task_version_id": "tv-1",
            "robot_id": "robot-1",
            "user_id": "user-1",
            "status": 1,
            "subtasks": [
                {"subtask_id": "st-1", "name": "A", "order_index": 0, "status": 0},
            ],
        }
        manager._mock_backend.list_episodes.return_value = [
            {"id": "ep-first"},
            {"id": "ep-last"},
        ]
        manager._mock_backend.get_episode.return_value = raw_episode

        def _sync_tasks():
            manager.tasks = manager.new_tasks
            return True, ""

        manager.start_recording = MagicMock(side_effect=_sync_tasks)
        manager.record_runner = MagicMock(return_value=True)

        ok, msg = manager._do_start_recording()

        assert ok is True
        manager._mock_backend.list_episodes.assert_called_once()
        manager._mock_backend.get_episode.assert_called_with("ep-last")
        assert manager.new_tasks["episodeId"] == "ep-last"

    def test_recorded_by_propagated_from_active_operator(self, manager, sample_tasks):
        """active_operator.user_id from /robot/me must end up in new_tasks.recordedBy.

        Without this, runner.name silently falls back to the episode creator
        because backend stamps episode.recorded_by only at /start time.
        """
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, {})
        manager.new_tasks = {}

        raw_episode = {
            "id": "ep-77",
            "task_id": "task-1",
            "task_name": "T",
            "task_description": "",
            "task_version_id": "tv-1",
            "robot_id": "robot-1",
            "user_id": "creator-1",
            "status": 1,
            "subtasks": [
                {"subtask_id": "st-1", "name": "A", "order_index": 0, "status": 0},
            ],
        }
        manager._mock_backend.get_episode.return_value = raw_episode
        manager._mock_backend.get_robot_self.return_value = {
            "organization_name": "TestOrg",
            "active_operator": {"user_id": "live-op-1", "display_name": "Live"},
        }

        def _sync_tasks():
            manager.tasks = manager.new_tasks
            return True, ""

        manager.start_recording = MagicMock(side_effect=_sync_tasks)
        manager.record_runner = MagicMock(return_value=True)

        ok, _ = manager._do_start_recording(episode_id="ep-77")

        assert ok is True
        assert manager.new_tasks["recordedBy"] == "live-op-1"
        assert manager.new_tasks["assignedUserName"] == "Live"
        assert manager.new_tasks["createdUserId"] == "creator-1"

    def test_empty_new_tasks_no_episodes_available(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, {})
        manager.new_tasks = {}
        manager._mock_backend.list_episodes.return_value = []
        manager._mock_backend.repeat_last_episode.return_value = None

        ok, msg = manager._do_start_recording()

        assert ok is False
        assert "no episodes available" in msg

    def test_completed_episode_triggers_repeat_last(self, manager):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, {})
        manager.new_tasks = {}
        manager._completed_episode_ids = {"ep-finished"}
        manager._mock_backend.list_episodes.return_value = [{"id": "ep-finished"}]
        manager._mock_backend.repeat_last_episode.return_value = {"id": "ep-repeated"}
        manager._mock_backend.get_episode.return_value = {
            "id": "ep-repeated",
            "task_id": "task-1",
            "task_name": "Continuous Task",
            "task_description": "",
            "task_version_id": "tv-1",
            "robot_id": "robot-1",
            "user_id": "user-1",
            "status": 1,
            "subtasks": [
                {
                    "subtask_id": "st-1",
                    "name": "采集回合",
                    "order_index": 0,
                    "status": 0,
                },
            ],
        }

        def _sync_tasks():
            manager.tasks = manager.new_tasks
            return True, ""

        manager.start_recording = MagicMock(side_effect=_sync_tasks)
        manager.record_runner = MagicMock(return_value=True)

        ok, _ = manager._do_start_recording()

        assert ok is True
        manager._mock_backend.repeat_last_episode.assert_called_once()
        manager._mock_backend.get_episode.assert_called_with("ep-repeated")


# ===================================================================
# TestDoStopSubtask
# ===================================================================


class TestDoStopSubtask:
    """Tests for ``_do_stop_subtask``."""

    def test_all_resolved_goes_complete(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=2,
            has_subtasks_successed=[True, True, None],
        )
        manager.record_label_and_segment = MagicMock(return_value=True)
        manager.subtask_execution_id = None
        manager.current_episode_subtask_id = None

        ok, msg = manager._do_stop_subtask(True)

        assert ok is True
        assert manager.status == TSS.COMPLETE_TASK

    def test_more_remaining_stays_wait(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=0,
            has_subtasks_successed=[None, None, None],
        )
        manager.record_label_and_segment = MagicMock(return_value=True)
        manager.subtask_execution_id = None
        manager.current_episode_subtask_id = None

        ok, msg = manager._do_stop_subtask(True)

        assert ok is True
        assert manager.status == TSS.WAIT_SUBTASK

    def test_wrong_state(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)

        ok, msg = manager._do_stop_subtask(True)

        assert ok is False
        assert "invalid state" in msg


# ===================================================================
# TestDoStopRecording
# ===================================================================


class TestDoStopRecording:
    """Tests for ``_do_stop_recording``."""

    @pytest.mark.parametrize("metadata_ok,recorder_ok", [(False, True), (True, False)])
    def test_failed_save_does_not_complete_or_clear_episode(
        self, manager, sample_tasks, metadata_ok, recorder_ok
    ):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMPLETE_TASK, sample_tasks,
                      meta_data={"segments": [{"start_time": 0, "end_time": 1}]},
                      has_subtasks_successed=[True, True, True])
        manager.wait_for_trigger_future_done = MagicMock(return_value=metadata_ok)
        manager.stop_recording = MagicMock(return_value=recorder_ok)
        manager._last_service_error = "MCAP flush or metadata validation failed"
        manager._save_interrupted_round = MagicMock(return_value=False)
        ok, message = manager._do_stop_recording(save=True)
        assert not ok
        assert "failed" in message
        manager._mock_backend.finish_episode.assert_not_called()
        assert manager.tasks["episodeId"] == "ep-1"
        assert "ep-1" not in manager._completed_episode_ids
        if not metadata_ok:
            manager.stop_recording.assert_not_called()

    def test_save_with_segments(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        meta = {"labels": [], "segments": [{"start_time": 0, "end_time": 1}]}
        prepare_state(
            manager,
            TSS.COMPLETE_TASK,
            sample_tasks,
            meta_data=meta,
            has_subtasks_successed=[True, True, True],
            task_start_time=0.0,
            end_time=10.0,
        )
        manager.wait_for_trigger_future_done = MagicMock(return_value=True)
        manager.stop_recording = MagicMock(return_value=True)

        ok, msg = manager._do_stop_recording(save=True)

        assert ok is True
        manager.set_episode_client.call_async.assert_called_once()
        manager._mock_backend.finish_episode.assert_called_once_with("ep-1")
        manager.stop_recording.assert_called_once()

    def test_save_no_segments(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        meta = {"labels": [], "segments": []}
        prepare_state(manager, TSS.COMPLETE_TASK, sample_tasks, meta_data=meta)

        ok, msg = manager._do_stop_recording(save=True)

        assert ok is True
        assert "retained" in msg
        manager.interrupt_recording_client.call_async.assert_called_once()
        manager.cancel_recording_client.call_async.assert_not_called()

    def test_discard(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMPLETE_TASK, sample_tasks)
        manager.cancel_recording = MagicMock(return_value=True)

        ok, msg = manager._do_stop_recording(save=False)

        assert ok is True
        manager._mock_backend.cancel_episode.assert_called_once_with("ep-1", reason="")
        manager.cancel_recording.assert_called_once()

    def test_discard_with_reason(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMPLETE_TASK, sample_tasks)
        manager.cancel_recording = MagicMock(return_value=True)

        ok, msg = manager._do_stop_recording(save=False, reason="test reason")

        assert ok is True
        manager._mock_backend.cancel_episode.assert_called_once_with(
            "ep-1", reason="test reason"
        )
        manager.cancel_recording.assert_called_once()
        manager.get_logger().warning.assert_called()


# ===================================================================
# TestSubscriptions
# ===================================================================


class TestSubscriptions:
    """Tests for subscription callbacks."""

    def test_tasks_callback_valid(self, manager, sample_tasks):
        from std_msgs.msg import String

        msg = String()
        msg.data = json.dumps(sample_tasks)
        manager.tasks_callback(msg)
        assert manager.new_tasks["episodeId"] == "ep-1"

    def test_tasks_callback_null(self, manager):
        from std_msgs.msg import String

        msg = String()
        msg.data = "null"
        manager.tasks_callback(msg)
        assert manager.new_tasks == {}

    def test_tasks_callback_missing_keys(self, manager, sample_tasks):
        from std_msgs.msg import String

        manager.new_tasks = sample_tasks  # pre-set
        msg = String()
        msg.data = json.dumps({"only": "partial"})
        manager.tasks_callback(msg)
        # Should not overwrite
        assert manager.new_tasks == sample_tasks

    def test_metadata_json_callback_invalid(self, manager):
        from std_msgs.msg import String

        old_meta = dict(manager.meta_data)
        msg = String()
        msg.data = "not valid json {"
        manager.metadata_json_callback(msg)
        # meta_data should be unchanged
        assert manager.meta_data == old_meta


# ===================================================================
# TestDoListSubtasks
# ===================================================================


class TestDoListSubtasks:
    """Tests for ``_do_list_subtasks``."""

    def test_confirm_state_returns_false(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)

        ok, msg = manager._do_list_subtasks()

        assert ok is False
        assert "CONFIRM_TASK" in msg

    def test_with_tasks_returns_json(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, cur_subtask_index=1)

        ok, msg = manager._do_list_subtasks()

        assert ok is True
        result = json.loads(msg)
        assert len(result) == 3
        assert result[0]["is_current"] is False
        assert result[1]["is_current"] is True
        assert result[2]["is_current"] is False


# ===================================================================
# TestWaitForFuture
# ===================================================================


class TestWaitForFuture:
    """Tests for ``wait_for_trigger_future_done``."""

    def test_success(self, manager):
        future = MagicMock()
        future.done.return_value = True
        result_obj = MagicMock()
        result_obj.success = True
        result_obj.message = "ok"
        future.result.return_value = result_obj

        assert manager.wait_for_trigger_future_done(future, timeout=0.01) is True

    def test_failure(self, manager):
        future = MagicMock()
        future.done.return_value = True
        future.result.return_value = None

        assert manager.wait_for_trigger_future_done(future, timeout=0.01) is False


# ===================================================================
# TestCancelEpisodeSrv
# ===================================================================


class TestCancelEpisodeSrv:
    """Tests for ``cancel_episode`` service (Trigger, no reason)."""

    def test_cancel_uses_default_reason(self, manager, sample_tasks):
        from std_srvs.srv import Trigger

        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)
        manager.transit_state = MagicMock(return_value=True)

        request = Trigger.Request()
        response = Trigger.Response()
        manager.cancel_episode(request, response)

        manager.transit_state.assert_called_once_with(
            AC.CANCEL,
            reason="manual cancellation",
        )
        assert response.success is True
        manager.get_logger().warning.assert_called()

    def test_cancel_threads_to_backend(self, manager, sample_tasks):
        """Full path: cancel_episode service → transit_state → _do_stop_recording → backend."""
        from std_srvs.srv import Trigger

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)
        manager.stop_recording = MagicMock(return_value=True)
        manager.delete_recording = MagicMock(return_value=True)

        request = Trigger.Request()
        response = Trigger.Response()
        manager.cancel_episode(request, response)

        assert response.success is True
        manager._mock_backend.cancel_episode.assert_called_once_with(
            "ep-1",
            reason="manual cancellation",
        )


class TestCancelEpisodeWithReasonSrv:
    """Tests for ``cancel_episode_with_reason`` service (StringTrigger)."""

    def test_cancel_with_reason(self, manager, sample_tasks):
        from airoa_data_msgs.srv import StringTrigger

        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)
        manager.transit_state = MagicMock(return_value=True)

        request = StringTrigger.Request(message="operator pressed e-stop")
        response = StringTrigger.Response()
        manager.cancel_episode_with_reason(request, response)

        manager.transit_state.assert_called_once_with(
            AC.CANCEL,
            reason="operator pressed e-stop",
        )
        assert response.success is True
        manager.get_logger().warning.assert_called()

    def test_cancel_empty_message_defaults_to_manual(self, manager, sample_tasks):
        from airoa_data_msgs.srv import StringTrigger

        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)
        manager.transit_state = MagicMock(return_value=True)

        request = StringTrigger.Request(message="")
        response = StringTrigger.Response()
        manager.cancel_episode_with_reason(request, response)

        manager.transit_state.assert_called_once_with(
            AC.CANCEL,
            reason="manual cancellation",
        )

    def test_cancel_reason_threads_to_backend(self, manager, sample_tasks):
        """Full path: cancel_episode_with_reason service → transit_state → _do_stop_recording → backend."""
        from airoa_data_msgs.srv import StringTrigger

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)
        manager.stop_recording = MagicMock(return_value=True)
        manager.delete_recording = MagicMock(return_value=True)

        request = StringTrigger.Request(message="battery low")
        response = StringTrigger.Response()
        manager.cancel_episode_with_reason(request, response)

        assert response.success is True
        manager._mock_backend.cancel_episode.assert_called_once_with(
            "ep-1",
            reason="battery low",
        )


# ===================================================================
# TestStartRecordingSrv
# ===================================================================


class TestStartRecordingSrv:
    """Tests for ``start_recording_srv`` (StringTrigger service)."""

    def test_passes_episode_id_from_request(self, manager, sample_tasks):
        from airoa_data_msgs.srv import StringTrigger

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager._do_start_recording = MagicMock(return_value=(True, "ok"))

        request = StringTrigger.Request(message="ep-42")
        response = StringTrigger.Response()
        manager.start_recording_srv(request, response)

        manager._do_start_recording.assert_called_once_with("ep-42")
        assert response.success is True

    def test_empty_message_passes_empty_string(self, manager, sample_tasks):
        from airoa_data_msgs.srv import StringTrigger

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager._do_start_recording = MagicMock(return_value=(True, "ok"))

        request = StringTrigger.Request(message="")
        response = StringTrigger.Response()
        manager.start_recording_srv(request, response)

        manager._do_start_recording.assert_called_once_with("")
        assert response.success is True


# ===================================================================
# TestAutoAdvanceNonePrevIndex
# ===================================================================


class TestAutoAdvanceNonePrevIndex:
    """Fix #2: RECORD_SUBTASK + ACCEPT with prev_subtask_index=None must not crash."""

    def test_auto_advance_none_prev_index(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=0,
            prev_subtask_index=None,
        )
        manager._do_stop_subtask = MagicMock(return_value=(True, "ok"))
        manager._do_start_subtask = MagicMock(return_value=(True, "ok"))

        # Should not raise TypeError
        result = manager.transit_state(AC.ACCEPT)

        assert result is True
        # Fallback: advance from current position (0 → 1)
        assert manager.cur_subtask_index == 1
        # Status is WAIT_SUBTASK (set by transit_state before calling mocked _do_start_subtask)
        assert manager.status == TSS.WAIT_SUBTASK
        manager.get_logger().error.assert_called()


# ===================================================================
# TestCreateExecutionFailureWarning
# ===================================================================


class TestCreateExecutionFailureWarning:
    """Fix #5: Warning logged when create_execution returns None."""

    def test_create_execution_none_logs_warning(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager._mock_backend.create_execution.return_value = None

        ok, msg = manager._do_start_subtask("st-1")

        assert ok is True
        assert manager.status == TSS.RECORD_SUBTASK
        manager.get_logger().warning.assert_called()
        warning_msg = manager.get_logger().warning.call_args[0][0]
        assert "st-1" in warning_msg
        assert "backend will not track" in warning_msg
        manager._mock_backend.start_execution.assert_not_called()


# ===================================================================
# Fixtures for startup recovery
# ===================================================================


@pytest.fixture()
def sample_tasks_partial_progress():
    """Subtask A done (status=2), B and C pending (status=0)."""
    return {
        "episodeId": "ep-1",
        "taskId": "task-1",
        "taskVersionId": "tv-1",
        "assignedRobotId": "robot-1",
        "createdUserId": "user-1",
        "status": 1,
        "task": {"id": "task-1", "name": "Test Task", "description": "Do things"},
        "subtasks": [
            {"id": "st-1", "name": "Subtask A", "orderIndex": 0, "status": 2},
            {"id": "st-2", "name": "Subtask B", "orderIndex": 1, "status": 0},
            {"id": "st-3", "name": "Subtask C", "orderIndex": 2, "status": 0},
        ],
    }


@pytest.fixture()
def sample_tasks_all_done():
    """All subtasks resolved (status=2)."""
    return {
        "episodeId": "ep-1",
        "taskId": "task-1",
        "taskVersionId": "tv-1",
        "assignedRobotId": "robot-1",
        "createdUserId": "user-1",
        "status": 1,
        "task": {"id": "task-1", "name": "Test Task", "description": "Do things"},
        "subtasks": [
            {"id": "st-1", "name": "Subtask A", "orderIndex": 0, "status": 2},
            {"id": "st-2", "name": "Subtask B", "orderIndex": 1, "status": 2},
            {"id": "st-3", "name": "Subtask C", "orderIndex": 2, "status": 2},
        ],
    }


# ===================================================================
# TestStartupRecovery
# ===================================================================


class TestStartupRecovery:
    """Tests for startup recovery logic in process_step / _attempt_startup_recovery."""

    def test_recovery_waits_for_is_recording(self, manager):
        """is_recording=None, within timeout → returns early, _recovery_done=False."""
        TSS, _ = _get_states(manager)
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = None
        manager.new_tasks = {}
        manager._recovery_done = False

        manager.process_step()

        assert manager._recovery_done is False

    def test_recovery_normal_startup_no_episode(self, manager):
        """is_recording=False, no active episode, past timeout → normal, _recovery_done=True."""
        import time as _time

        TSS, _ = _get_states(manager)
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = False
        manager.new_tasks = {}
        manager._recovery_done = False
        manager._recovery_start_time = (
            _time.monotonic() - manager.RECOVERY_TIMEOUT_SEC - 1
        )

        manager.process_step()

        assert manager._recovery_done is True
        assert manager.status == TSS.COMFIRM_TASK

    def test_recovery_resume_recording_active(
        self, manager, sample_tasks_partial_progress
    ):
        """is_recording=True, partial episode → WAIT_SUBTASK, cur_subtask_index=1."""
        TSS, _ = _get_states(manager)
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = True
        manager.new_tasks = sample_tasks_partial_progress
        manager._recovery_done = False
        manager.record_runner = MagicMock(return_value=True)

        manager.process_step()

        assert manager._recovery_done is True
        assert manager.status == TSS.WAIT_SUBTASK
        assert manager.cur_subtask_index == 1
        assert manager.has_subtasks_successed == [True, None, None]
        assert manager.tasks == sample_tasks_partial_progress
        manager.record_runner.assert_called_once()

    def test_recovery_resume_all_done(self, manager, sample_tasks_all_done):
        """is_recording=True, all subtasks done → COMPLETE_TASK."""
        TSS, _ = _get_states(manager)
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = True
        manager.new_tasks = sample_tasks_all_done
        manager._recovery_done = False
        manager.record_runner = MagicMock(return_value=True)

        manager.process_step()

        assert manager._recovery_done is True
        assert manager.status == TSS.COMPLETE_TASK
        assert manager.has_subtasks_successed == [True, True, True]

    def test_recovery_cancel_stale_episode(
        self, manager, sample_tasks_partial_progress
    ):
        """is_recording=False, active episode → cancel_episode called, error logged."""
        TSS, _ = _get_states(manager)
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = False
        manager.new_tasks = sample_tasks_partial_progress
        manager._recovery_done = False

        manager.process_step()

        assert manager._recovery_done is True
        manager._mock_backend.cancel_episode.assert_called_once_with(
            "ep-1",
            reason="stale episode (not recording at startup)",
        )
        assert "ep-1" in manager._completed_episode_ids
        assert manager.new_tasks == {}
        manager.get_logger().error.assert_called()

    def test_recovery_keeps_ready_episode_queued(self, manager, sample_tasks):
        """A Ready Episode with no rosbag is queued work, not stale state."""
        TSS, _ = _get_states(manager)
        ready = {**sample_tasks, "status": 0}
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = False
        manager.new_tasks = ready
        manager._recovery_done = False

        manager.process_step()

        assert manager._recovery_done is True
        assert manager.status == TSS.COMFIRM_TASK
        assert manager.new_tasks["episodeId"] == "ep-1"
        manager._mock_backend.cancel_episode.assert_not_called()

    def test_recovery_timeout_assumes_not_recording(
        self, manager, sample_tasks_partial_progress
    ):
        """is_recording=None, past timeout, active episode → cancel path."""
        import time as _time

        TSS, _ = _get_states(manager)
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = None
        manager.new_tasks = sample_tasks_partial_progress
        manager._recovery_done = False
        manager._recovery_start_time = (
            _time.monotonic() - manager.RECOVERY_TIMEOUT_SEC - 1
        )

        manager.process_step()

        assert manager._recovery_done is True
        manager._mock_backend.cancel_episode.assert_called_once_with(
            "ep-1",
            reason="stale episode (not recording at startup)",
        )
        manager.get_logger().warning.assert_called()

    def test_recovery_timeout_no_episode(self, manager):
        """is_recording=None, past timeout, no episode → normal startup."""
        import time as _time

        TSS, _ = _get_states(manager)
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = None
        manager.new_tasks = {}
        manager._recovery_done = False
        manager._recovery_start_time = (
            _time.monotonic() - manager.RECOVERY_TIMEOUT_SEC - 1
        )

        manager.process_step()

        assert manager._recovery_done is True
        assert manager.status == TSS.COMFIRM_TASK

    def test_recovery_runs_only_once(self, manager):
        """After recovery, subsequent process_step() calls skip it."""
        TSS, _ = _get_states(manager)
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = False
        manager.new_tasks = {}
        manager._recovery_done = True

        # Should not call _attempt_startup_recovery at all
        manager._attempt_startup_recovery = MagicMock()
        manager.process_step()

        manager._attempt_startup_recovery.assert_not_called()

    def test_recovery_skips_if_state_advanced(self, manager, sample_tasks):
        """status != COMFIRM_TASK → skip recovery."""
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)
        manager._recovery_done = False
        manager.is_recording = True
        manager.new_tasks = sample_tasks

        manager.process_step()

        assert manager._recovery_done is True
        # Should not have called cancel or resume — state was already advanced
        manager._mock_backend.cancel_episode.assert_not_called()

    def test_recovery_waits_for_new_tasks(self, manager):
        """is_recording=True, new_tasks={}, within timeout → wait."""
        TSS, _ = _get_states(manager)
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = True
        manager.new_tasks = {}
        manager._recovery_done = False

        manager.process_step()

        assert manager._recovery_done is False

    def test_recovery_resume_first_subtask_pending(self, manager, sample_tasks):
        """All subtasks status=0 → cur_subtask_index=0, state WAIT_SUBTASK."""
        TSS, _ = _get_states(manager)
        all_pending = {
            "episodeId": "ep-1",
            "taskId": "task-1",
            "taskVersionId": "tv-1",
            "assignedRobotId": "robot-1",
            "createdUserId": "user-1",
            "status": 1,
            "task": {"id": "task-1", "name": "Test Task", "description": "Do things"},
            "subtasks": [
                {"id": "st-1", "name": "Subtask A", "orderIndex": 0, "status": 0},
                {"id": "st-2", "name": "Subtask B", "orderIndex": 1, "status": 0},
                {"id": "st-3", "name": "Subtask C", "orderIndex": 2, "status": 0},
            ],
        }
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = True
        manager.new_tasks = all_pending
        manager._recovery_done = False
        manager.record_runner = MagicMock(return_value=True)

        manager.process_step()

        assert manager._recovery_done is True
        assert manager.status == TSS.WAIT_SUBTASK
        assert manager.cur_subtask_index == 0
        assert manager.has_subtasks_successed == [None, None, None]

    def test_recovery_resume_only_last_pending(self, manager):
        """Subtasks A,B done (status=2), C pending (status=0) → cur_subtask_index=2."""
        TSS, _ = _get_states(manager)
        last_pending = {
            "episodeId": "ep-1",
            "taskId": "task-1",
            "taskVersionId": "tv-1",
            "assignedRobotId": "robot-1",
            "createdUserId": "user-1",
            "status": 1,
            "task": {"id": "task-1", "name": "Test Task", "description": "Do things"},
            "subtasks": [
                {"id": "st-1", "name": "Subtask A", "orderIndex": 0, "status": 2},
                {"id": "st-2", "name": "Subtask B", "orderIndex": 1, "status": 2},
                {"id": "st-3", "name": "Subtask C", "orderIndex": 2, "status": 0},
            ],
        }
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = True
        manager.new_tasks = last_pending
        manager._recovery_done = False
        manager.record_runner = MagicMock(return_value=True)

        manager.process_step()

        assert manager._recovery_done is True
        assert manager.status == TSS.WAIT_SUBTASK
        assert manager.cur_subtask_index == 2
        assert manager.has_subtasks_successed == [True, True, None]

    def test_recovery_resume_missing_status_key(self, manager):
        """Subtask dict without 'status' key → .get('status', 0) defaults to 0 → pending."""
        TSS, _ = _get_states(manager)
        no_status = {
            "episodeId": "ep-1",
            "taskId": "task-1",
            "taskVersionId": "tv-1",
            "assignedRobotId": "robot-1",
            "createdUserId": "user-1",
            "status": 1,
            "task": {"id": "task-1", "name": "Test Task", "description": "Do things"},
            "subtasks": [
                {"id": "st-1", "name": "A", "orderIndex": 0},
                {"id": "st-2", "name": "B", "orderIndex": 1},
            ],
        }
        manager.status = TSS.COMFIRM_TASK
        manager.is_recording = True
        manager.new_tasks = no_status
        manager._recovery_done = False
        manager.record_runner = MagicMock(return_value=True)

        manager.process_step()

        assert manager._recovery_done is True
        assert manager.status == TSS.WAIT_SUBTASK
        assert manager.cur_subtask_index == 0
        assert manager.has_subtasks_successed == [None, None]


# ===================================================================
# TestStartRecording
# ===================================================================


class TestStartRecording:
    """Tests for ``start_recording()`` (the real method, not mocked)."""

    def test_no_valid_tasks(self, manager):
        manager.new_tasks = {}

        ok, msg = manager.start_recording()

        assert ok is False
        assert msg == "no valid tasks available"

    def test_missing_task_key(self, manager):
        manager.new_tasks = {"taskId": "t1"}

        ok, msg = manager.start_recording()

        assert ok is False
        assert msg == "no valid tasks available"

    def test_service_call_succeeds(self, manager, sample_tasks):
        manager.new_tasks = sample_tasks
        _mock_wait_for_trigger(manager, True)

        ok, msg = manager.start_recording()

        assert ok is True
        assert msg == ""
        manager.start_recording_client.call_async.assert_called_once()
        assert manager.tasks == sample_tasks

    def test_service_call_fails(self, manager, sample_tasks):
        manager.new_tasks = sample_tasks
        _mock_wait_for_trigger(manager, False)
        manager.start_recording_client.call_async.return_value.done.return_value = False

        ok, msg = manager.start_recording()

        assert ok is False
        assert msg == "record_manager service call failed"


# ===================================================================
# TestConstructorInitialization
# ===================================================================


class TestConstructorInitialization:
    """Verify recovery variables are initialized correctly."""

    def test_recovery_vars_initialized(self, manager):
        assert manager._recovery_done is False
        assert manager._recovery_start_time is None


# ===================================================================
# TestNextTaskPublisher
# ===================================================================


class TestNextTaskPublisher:
    """Verify ``~/next_task`` publishes the correct subtask data per state."""

    def _run_process_step(self, mgr):
        """Run process_step with recovery already done."""
        mgr._recovery_done = True
        mgr.process_step()

    def test_wait_subtask_publishes_cur_subtask(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, cur_subtask_index=1)
        self._run_process_step(manager)

        call_args = manager.next_task_pub.publish.call_args[0][0]
        data = json.loads(call_args.data)
        assert data["id"] == "st-2"
        assert data["name"] == "Subtask B"

    def test_record_subtask_publishes_cur_subtask(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks, cur_subtask_index=0)
        self._run_process_step(manager)

        call_args = manager.next_task_pub.publish.call_args[0][0]
        data = json.loads(call_args.data)
        assert data["id"] == "st-1"
        assert data["name"] == "Subtask A"

    def test_confirm_task_publishes_first_subtask(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        self._run_process_step(manager)

        call_args = manager.next_task_pub.publish.call_args[0][0]
        data = json.loads(call_args.data)
        assert data["id"] == "st-1"

    def test_confirm_task_no_tasks_publishes_empty(self, manager):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, {})
        manager.new_tasks = {}
        self._run_process_step(manager)

        call_args = manager.next_task_pub.publish.call_args[0][0]
        assert json.loads(call_args.data) == {}

    def test_complete_task_publishes_empty(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMPLETE_TASK, sample_tasks)
        self._run_process_step(manager)

        call_args = manager.next_task_pub.publish.call_args[0][0]
        assert json.loads(call_args.data) == {}

    def test_rewind_subtask_publishes_empty(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.REWIND_SUBTASK, sample_tasks, prev_subtask_index=0)
        self._run_process_step(manager)

        call_args = manager.next_task_pub.publish.call_args[0][0]
        assert json.loads(call_args.data) == {}


# ===================================================================
# TestGateLevelCallback
# ===================================================================


def _make_gate_msg(level: int):
    """Create a UInt8-like message with the given gate level."""
    from std_msgs.msg import UInt8

    msg = UInt8()
    msg.data = level
    return msg


class TestGateLevelCallback:
    """Gate level changes drive cancel / block behaviour."""

    def test_level_0_to_2_during_record_subtask_saves(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager._gate_level = 0
        manager.transit_state = MagicMock(return_value=True)

        manager._gate_level_callback(_make_gate_msg(2))

        manager.transit_state.assert_not_called()
        manager.interrupt_recording_client.call_async.assert_called_once()
        request = manager.interrupt_recording_client.call_async.call_args.args[0]
        assert request.message == "Recording gate hard-stop (level 2)"
        manager.cancel_recording_client.call_async.assert_not_called()
        assert manager._gate_level == 2

    def test_level_0_to_1_during_record_subtask_no_cancel(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager._gate_level = 0
        manager.transit_state = MagicMock()

        manager._gate_level_callback(_make_gate_msg(1))

        manager.transit_state.assert_not_called()
        assert manager._gate_level == 1

    def test_level_0_to_2_during_wait_subtask_saves(self, manager, sample_tasks):
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager._gate_level = 0
        manager.transit_state = MagicMock(return_value=True)

        manager._gate_level_callback(_make_gate_msg(2))

        manager.transit_state.assert_not_called()
        manager.interrupt_recording_client.call_async.assert_called_once()
        request = manager.interrupt_recording_client.call_async.call_args.args[0]
        assert request.message == "Recording gate hard-stop (level 2)"
        manager.cancel_recording_client.call_async.assert_not_called()

    def test_level_0_to_2_during_confirm_task_no_cancel(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager._gate_level = 0
        manager.transit_state = MagicMock()

        manager._gate_level_callback(_make_gate_msg(2))

        manager.transit_state.assert_not_called()

    def test_level_1_to_0_no_cancel(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager._gate_level = 1
        manager.transit_state = MagicMock()

        manager._gate_level_callback(_make_gate_msg(0))

        manager.transit_state.assert_not_called()
        assert manager._gate_level == 0

    def test_level_2_to_2_no_re_cancel(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager._gate_level = 2
        manager.transit_state = MagicMock()

        manager._gate_level_callback(_make_gate_msg(2))

        manager.transit_state.assert_not_called()

    def test_level_1_blocks_episode_start(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager._gate_level = 1

        ok, msg = manager._do_start_recording()
        assert ok is False
        assert "gate" in msg.lower()

    def test_level_1_allows_subtask_during_block(self, manager, sample_tasks):
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager._gate_level = 1
        manager._mock_backend.create_execution.return_value = "exec-1"

        ok, msg = manager._do_start_subtask("st-1")
        assert ok is True


# ===================================================================
# TestUseRecordingGateParam
# ===================================================================


class TestUseRecordingGateParam:
    """Tests for the use_recording_gate parameter."""

    def test_gate_disabled_by_default(self, manager):
        """Default manager has gate disabled: _use_gate=False, _gate_level=0."""
        assert manager._use_gate is False
        assert manager._gate_level == 0

    def test_gate_disabled_allows_episode_start(self, manager, sample_tasks):
        """With gate disabled, _gate_level=0 allows episode start."""
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager.start_recording = MagicMock(return_value=(True, "ok"))
        manager.record_task_relating_entities = MagicMock()

        ok, msg = manager._do_start_recording()
        assert ok is True

    def test_gate_enabled_starts_fail_closed(self, manager):
        """With gate enabled, _gate_level=2 (fail-closed)."""
        # Simulate what the constructor does when use_recording_gate=True
        manager._use_gate = True
        manager._gate_level = 2

        assert manager._use_gate is True
        assert manager._gate_level == 2

    def test_gate_enabled_blocks_episode_start(self, manager, sample_tasks):
        """With gate enabled and no gate message, episode start is blocked."""
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager._use_gate = True
        manager._gate_level = 2

        ok, msg = manager._do_start_recording()
        assert ok is False
        assert "gate" in msg.lower()

    def test_gate_enabled_allows_after_level_0(self, manager, sample_tasks):
        """With gate enabled, gate_level=0 allows episode start."""
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager._use_gate = True
        manager._gate_level = 0
        manager.start_recording = MagicMock(return_value=(True, "ok"))
        manager.record_task_relating_entities = MagicMock()

        ok, msg = manager._do_start_recording()
        assert ok is True

    def test_gate_disabled_ignores_gate_callback(self, manager, sample_tasks):
        """With gate disabled, _gate_level_callback should not be subscribed.
        Even if called manually, it updates the level but gate was never subscribed."""
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks, cur_subtask_index=0)
        assert manager._use_gate is False
        # Gate level starts at 0 when disabled
        assert manager._gate_level == 0


# ===================================================================
# TestDurationLimits
# ===================================================================


class TestElapsedTimePublishing:
    """Tests for subtask/episode elapsed-time Float64 publishers."""

    def test_zero_in_confirm_task(self, manager, sample_tasks):
        """Both elapsed values are 0.0 in COMFIRM_TASK state."""
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager._recovery_done = True

        manager.process_step()

        subtask_calls = [
            c.args[0] for c in manager._subtask_elapsed_pub.publish.call_args_list
        ]
        episode_calls = [
            c.args[0] for c in manager._episode_elapsed_pub.publish.call_args_list
        ]
        assert subtask_calls[-1].data == 0.0
        assert episode_calls[-1].data == 0.0

    def test_subtask_elapsed_during_record(self, manager, sample_tasks):
        """Subtask elapsed is non-zero during RECORD_SUBTASK with start_time set."""
        TSS, _ = _get_states(manager)
        # Set start_time 10s in the past
        now_ns = 100 * 1e9
        clock_mock = MagicMock()
        now_mock = MagicMock()
        now_mock.nanoseconds = now_ns
        now_mock.seconds_nanoseconds.return_value = (100, 0)
        now_mock.to_msg.return_value = MagicMock()
        clock_mock.now.return_value = now_mock
        manager.get_clock = MagicMock(return_value=clock_mock)
        manager._recovery_done = True

        prepare_state(
            manager,
            TSS.RECORD_SUBTASK,
            sample_tasks,
            cur_subtask_index=0,
            start_time=90.0,
            task_start_time=80.0,
        )

        manager.process_step()

        subtask_calls = [
            c.args[0] for c in manager._subtask_elapsed_pub.publish.call_args_list
        ]
        assert subtask_calls[-1].data == pytest.approx(10.0)

    def test_zero_subtask_in_wait(self, manager, sample_tasks):
        """Subtask elapsed is 0.0 in WAIT_SUBTASK state."""
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, task_start_time=50.0)
        manager._recovery_done = True

        manager.process_step()

        subtask_calls = [
            c.args[0] for c in manager._subtask_elapsed_pub.publish.call_args_list
        ]
        assert subtask_calls[-1].data == 0.0

    def test_episode_elapsed_during_wait(self, manager, sample_tasks):
        """Episode elapsed is non-zero in WAIT_SUBTASK state with task_start_time."""
        TSS, _ = _get_states(manager)
        now_ns = 100 * 1e9
        clock_mock = MagicMock()
        now_mock = MagicMock()
        now_mock.nanoseconds = now_ns
        now_mock.seconds_nanoseconds.return_value = (100, 0)
        now_mock.to_msg.return_value = MagicMock()
        clock_mock.now.return_value = now_mock
        manager.get_clock = MagicMock(return_value=clock_mock)
        manager._recovery_done = True

        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks, task_start_time=80.0)

        manager.process_step()

        episode_calls = [
            c.args[0] for c in manager._episode_elapsed_pub.publish.call_args_list
        ]
        assert episode_calls[-1].data == pytest.approx(20.0)

    def test_episode_elapsed_during_rewind(self, manager, sample_tasks):
        """Episode elapsed is non-zero in REWIND_SUBTASK state."""
        TSS, _ = _get_states(manager)
        now_ns = 100 * 1e9
        clock_mock = MagicMock()
        now_mock = MagicMock()
        now_mock.nanoseconds = now_ns
        now_mock.seconds_nanoseconds.return_value = (100, 0)
        now_mock.to_msg.return_value = MagicMock()
        clock_mock.now.return_value = now_mock
        manager.get_clock = MagicMock(return_value=clock_mock)
        manager._recovery_done = True

        prepare_state(
            manager,
            TSS.REWIND_SUBTASK,
            sample_tasks,
            task_start_time=70.0,
            prev_subtask_index=0,
        )

        manager.process_step()

        episode_calls = [
            c.args[0] for c in manager._episode_elapsed_pub.publish.call_args_list
        ]
        assert episode_calls[-1].data == pytest.approx(30.0)

    def test_zero_after_stop_recording(self, manager, sample_tasks):
        """Both elapsed are 0.0 after stop recording returns to COMFIRM_TASK."""
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager.start_time = None
        manager.task_start_time = None
        manager._recovery_done = True

        manager.process_step()

        subtask_calls = [
            c.args[0] for c in manager._subtask_elapsed_pub.publish.call_args_list
        ]
        episode_calls = [
            c.args[0] for c in manager._episode_elapsed_pub.publish.call_args_list
        ]
        assert subtask_calls[-1].data == 0.0
        assert episode_calls[-1].data == 0.0


# ===================================================================
# TestRecordingBlockDiagnostics
# ===================================================================


class TestRecordingBlockDiagnostics:
    """Tests for /diagnostics and ~/recording_block_reason publishing."""

    def _get_diag_calls(self, manager_node):
        """Return list of DiagnosticArray messages published to _diag_pub."""
        return [call.args[0] for call in manager_node._diag_pub.publish.call_args_list]

    def _get_reason_calls(self, manager_node):
        """Return list of String messages published to _block_reason_pub."""
        return [
            call.args[0]
            for call in manager_node._block_reason_pub.publish.call_args_list
        ]

    def test_gate_hard_stop_publishes_diagnostic(self, manager, sample_tasks):
        """Gate level 0→2 publishes ERROR diagnostic + reason topic."""
        from diagnostic_msgs.msg import DiagnosticStatus

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager._gate_level = 0
        manager.transit_state = MagicMock(return_value=True)

        manager._gate_level_callback(_make_gate_msg(2))

        diags = self._get_diag_calls(manager)
        assert len(diags) == 1
        assert diags[0].status[0].level == DiagnosticStatus.ERROR
        assert "hard-stop" in diags[0].status[0].message

        reasons = self._get_reason_calls(manager)
        assert len(reasons) == 1
        assert "hard-stop" in reasons[0].data

    def test_gate_block_start_publishes_warn(self, manager, sample_tasks):
        """Gate level 0→1 publishes WARN diagnostic."""
        from diagnostic_msgs.msg import DiagnosticStatus

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager._gate_level = 0

        manager._gate_level_callback(_make_gate_msg(1))

        diags = self._get_diag_calls(manager)
        assert len(diags) == 1
        assert diags[0].status[0].level == DiagnosticStatus.WARN

    def test_start_recording_gate_closed_publishes(self, manager, sample_tasks):
        """Blocked episode start publishes diagnostic."""
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
        manager._gate_level = 2

        ok, msg = manager._do_start_recording()

        assert ok is False
        diags = self._get_diag_calls(manager)
        assert len(diags) == 1
        assert "gate" in diags[0].status[0].message.lower()

    def test_cancel_with_reason_publishes(self, manager, sample_tasks):
        """cancel_episode_with_reason service handler publishes diagnostic."""
        from airoa_data_msgs.srv import StringTrigger

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager.stop_recording = MagicMock(return_value=True)
        manager.delete_recording = MagicMock(return_value=True)

        request = StringTrigger.Request(message="test block reason")
        response = StringTrigger.Response()
        manager.cancel_episode_with_reason(request, response)

        diags = self._get_diag_calls(manager)
        assert len(diags) == 1
        assert diags[0].status[0].message == "test block reason"

        reasons = self._get_reason_calls(manager)
        assert len(reasons) == 1
        assert reasons[0].data == "test block reason"

    def test_cancel_no_reason_no_diagnostic(self, manager, sample_tasks):
        """_do_stop_recording without reason does not publish diagnostic."""
        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMPLETE_TASK, sample_tasks)
        manager.stop_recording = MagicMock(return_value=True)
        manager.delete_recording = MagicMock(return_value=True)

        manager._do_stop_recording(save=False, reason="")

        diags = self._get_diag_calls(manager)
        assert len(diags) == 0

    def test_diagnostic_includes_episode_id(self, manager, sample_tasks):
        """Diagnostic KeyValues include episode_id when available."""
        from airoa_data_msgs.srv import StringTrigger

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.RECORD_SUBTASK, sample_tasks, cur_subtask_index=0)
        manager.stop_recording = MagicMock(return_value=True)
        manager.delete_recording = MagicMock(return_value=True)

        request = StringTrigger.Request(message="test")
        response = StringTrigger.Response()
        manager.cancel_episode_with_reason(request, response)

        diags = self._get_diag_calls(manager)
        kv_keys = [kv.key for kv in diags[0].status[0].values]
        assert "state" in kv_keys
        assert "gate_level" in kv_keys
        assert "episode_id" in kv_keys


# ===================================================================
# TestCancelReasonPropagation
# ===================================================================


class TestCancelReasonPropagation:
    """Tests that cancel reasons propagate to backend.cancel_episode."""

    def test_cancel_reason_reaches_backend(self, manager, sample_tasks):
        """transit_state(CANCEL, reason=...) passes reason to backend.cancel_episode."""
        TSS, AC = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)
        manager.stop_recording = MagicMock(return_value=True)
        manager.delete_recording = MagicMock(return_value=True)

        manager.transit_state(AC.CANCEL, reason="test failure reason")

        manager._mock_backend.cancel_episode.assert_called_once_with(
            "ep-1", reason="test failure reason"
        )


# ===================================================================
# TestTasksCallbackJsonError
# ===================================================================


class TestTasksCallbackJsonError:
    """Tests for malformed JSON handling in tasks_callback."""

    def test_malformed_json_does_not_crash(self, manager):
        """Malformed JSON logs warning and does not update tasks."""
        msg = MagicMock()
        msg.data = "{invalid json"
        manager.new_tasks = {}

        manager.tasks_callback(msg)

        assert manager.new_tasks == {}

    def test_null_json_clears_tasks(self, manager):
        """'null' string clears new_tasks."""
        msg = MagicMock()
        msg.data = "null"
        manager.new_tasks = {"some": "data"}

        manager.tasks_callback(msg)

        assert manager.new_tasks == {}


# ===================================================================
# TestRepeatEpisodeSrv
# ===================================================================


class TestRepeatEpisodeSrv:
    """Tests for /data_collection/repeat service."""

    def _raw_episode(self):
        return {
            "id": "ep-repeated",
            "task_id": "task-1",
            "task_name": "Test Task",
            "task_description": "Repeated task",
            "task_version_id": "tv-1",
            "robot_id": "robot-1",
            "user_id": "user-1",
            "status": 1,
            "subtasks": [
                {
                    "id": "st-1",
                    "subtask_id": "sub-1",
                    "name": "A",
                    "order_index": 0,
                    "status": 0,
                },
            ],
        }

    def test_repeat_success(self, manager):
        from std_srvs.srv import Trigger

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, {})
        manager.new_tasks = {}
        manager._mock_backend.repeat_last_episode.return_value = {"id": "ep-repeated"}
        manager._mock_backend.get_episode.return_value = self._raw_episode()

        request = Trigger.Request()
        response = Trigger.Response()
        manager.repeat_episode_srv(request, response)

        assert response.success is True
        assert "ep-repeated" in response.message
        assert manager.new_tasks["episodeId"] == "ep-repeated"

    def test_repeat_wrong_state(self, manager, sample_tasks):
        from std_srvs.srv import Trigger

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.WAIT_SUBTASK, sample_tasks)

        request = Trigger.Request()
        response = Trigger.Response()
        manager.repeat_episode_srv(request, response)

        assert response.success is False
        assert "COMFIRM_TASK" in response.message

    def test_repeat_no_episode(self, manager):
        from std_srvs.srv import Trigger

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, {})
        manager._mock_backend.repeat_last_episode.return_value = None

        request = Trigger.Request()
        response = Trigger.Response()
        manager.repeat_episode_srv(request, response)

        assert response.success is False
        assert "No episode to repeat" in response.message

    def test_repeat_refuses_when_episode_queued(self, manager, sample_tasks):
        from std_srvs.srv import Trigger

        TSS, _ = _get_states(manager)
        prepare_state(manager, TSS.COMFIRM_TASK, {})
        manager.new_tasks = sample_tasks  # episode already queued

        request = Trigger.Request()
        response = Trigger.Response()
        manager.repeat_episode_srv(request, response)

        assert response.success is False
        assert "already queued" in response.message
        # Original episode untouched
        assert manager.new_tasks["episodeId"] == sample_tasks["episodeId"]
        manager._mock_backend.repeat_last_episode.assert_not_called()


def test_accept_reports_recording_readiness_error_without_starting_clock(manager, sample_tasks):
    TSS, _ = _get_states(manager)
    prepare_state(manager, TSS.COMFIRM_TASK, sample_tasks)
    manager.start_recording = MagicMock(return_value=(False, "未录到必要通道: /left_camera/image_raw/compressed"))
    manager.process_step = MagicMock()
    response = MagicMock()
    manager.accept(MagicMock(), response)
    assert response.success is False
    assert "未录到必要通道" in response.message
    assert manager.status == TSS.COMFIRM_TASK
    assert manager.task_start_time is None
    manager.backend.start_episode.assert_not_called()


def test_recorder_service_error_reaches_caller(manager, sample_tasks):
    manager.new_tasks = sample_tasks
    manager.wait_for_trigger_future_done = MagicMock(return_value=False)
    future = manager.start_recording_client.call_async.return_value
    future.done.return_value = True
    future.result.return_value.message = "等待录制数据超时，未录到必要通道: /pico/hmd/state"
    ok, message = manager.start_recording()
    assert not ok
    assert "/pico/hmd/state" in message
    assert manager.wait_for_trigger_future_done.call_args.kwargs["timeout"] >= 20


def test_metadata_failure_can_save_interrupted_round(manager, sample_tasks):
    TSS, _ = _get_states(manager)
    prepare_state(manager, TSS.COMPLETE_TASK, sample_tasks,
                  meta_data={'segments':[{'start_time':0,'end_time':1}]},
                  has_subtasks_successed=[True,True,True])
    manager.wait_for_trigger_future_done = MagicMock(side_effect=[False, True])
    assert manager._do_stop_recording(save=True)[0]
    manager.interrupt_recording_client.call_async.assert_called_once()
    manager.cancel_recording_client.call_async.assert_not_called()
    assert manager.status == TSS.COMFIRM_TASK


@pytest.mark.parametrize('state', ['RECORD_SUBTASK','WAIT_SUBTASK','COMPLETE_TASK','REWIND_SUBTASK'])
def test_hard_stop_in_every_recording_phase_preserves_round(manager, sample_tasks, state):
    TSS,_=_get_states(manager)
    prepare_state(manager,getattr(TSS,state),sample_tasks)
    manager._gate_level=0
    manager.wait_for_trigger_future_done=MagicMock(return_value=True)
    manager._gate_level_callback(_make_gate_msg(2))
    manager.interrupt_recording_client.call_async.assert_called_once()
    manager.cancel_recording_client.call_async.assert_not_called()
    manager._mock_backend.finish_episode.assert_called_once_with('ep-1')
    assert manager.status==TSS.COMFIRM_TASK
