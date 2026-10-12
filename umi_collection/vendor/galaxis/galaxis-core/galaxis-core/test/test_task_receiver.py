"""Unit tests for TaskReceiver.

ROS2 dependencies are mocked via ``conftest.mock_rclpy``.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def receiver(mock_rclpy):
    """Create a TaskReceiver with a mocked backend via create_backend."""
    mock_client = MagicMock()
    with patch("galaxis_core.backend_client.create_backend", return_value=mock_client):
        from galaxis_core.task_receiver import TaskReceiver

        node = TaskReceiver()
        node._mock_client = mock_client
    return node


def _sample_robot_data(episode_id="ep-1", active_operator=None):
    data = {"active_episode_id": episode_id}
    if active_operator is not None:
        data["active_operator"] = active_operator
    return data


def _sample_active_operator(user_id="op-9", display_name="Operator Nine"):
    return {"user_id": user_id, "display_name": display_name}


def _sample_episode(episode_id="ep-1", task_id="task-1", **overrides):
    data = {
        "id": episode_id,
        "task_id": task_id,
        "task_name": "Pick and place",
        "task_description": "Move the block to the bin",
        "task_version_id": "tv-1",
        "robot_id": "galaxis_000",
        "user_id": "user-1",
        "status": 1,
        "subtasks": [
            {
                "id": "st-2",
                "subtask_id": "sub-def-2",
                "name": "B",
                "order_index": 2,
                "status": 0,
            },
            {
                "id": "st-1",
                "subtask_id": "sub-def-1",
                "name": "A",
                "order_index": 1,
                "status": 0,
            },
        ],
    }
    data.update(overrides)
    return data


# ===================================================================
# TestGetEpisodeFromServer
# ===================================================================


class TestGetEpisodeFromServer:
    """Tests for ``get_episode_from_server``."""

    def test_happy_path(self, receiver):
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data()
        receiver._mock_client.get_episode.return_value = _sample_episode()

        result = receiver.get_episode_from_server()

        assert result["episodeId"] == "ep-1"
        assert result["taskId"] == "task-1"
        assert result["task"]["name"] == "Pick and place"
        assert result["task"]["description"] == "Move the block to the bin"
        assert len(result["subtasks"]) == 2

    def test_subtasks_sorted_by_order_index(self, receiver):
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data()
        receiver._mock_client.get_episode.return_value = _sample_episode()

        result = receiver.get_episode_from_server()

        assert result["subtasks"][0]["orderIndex"] == 1
        assert result["subtasks"][1]["orderIndex"] == 2
        assert result["subtasks"][0]["name"] == "A"

    def test_enriched_episode_keys(self, receiver):
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data()
        receiver._mock_client.get_episode.return_value = _sample_episode()

        result = receiver.get_episode_from_server()

        expected_keys = {
            "episodeId",
            "taskId",
            "taskVersionId",
            "assignedRobotId",
            "createdUserId",
            "assignedUserName",
            "recordedBy",
            "status",
            "subtaskIndex",
            "task",
            "subtasks",
        }
        assert expected_keys.issubset(result.keys())

    def test_recorded_by_uses_active_operator_user_id(self, receiver):
        """active_operator.user_id is the source of truth for recordedBy.

        Backend stamps episode.recorded_by only at /start, so the value on
        the freshly-fetched episode is typically null.
        """
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data(
            active_operator=_sample_active_operator(
                user_id="op-42", display_name="Op42"
            ),
        )
        receiver._mock_client.get_episode.return_value = _sample_episode()

        result = receiver.get_episode_from_server()

        assert result["recordedBy"] == "op-42"
        assert result["assignedUserName"] == "Op42"

    def test_recorded_by_falls_back_to_episode_field(self, receiver):
        """When no active_operator is present, fall back to episode.recorded_by."""
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data()
        receiver._mock_client.get_episode.return_value = _sample_episode(
            recorded_by="ep-stamped-7"
        )

        result = receiver.get_episode_from_server()

        assert result["recordedBy"] == "ep-stamped-7"
        assert result["assignedUserName"] == ""

    def test_recorded_by_empty_when_neither_source_set(self, receiver):
        """Neither operator nor episode field set → recordedBy is empty string."""
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data()
        receiver._mock_client.get_episode.return_value = _sample_episode()

        result = receiver.get_episode_from_server()

        assert result["recordedBy"] == ""

    def test_task_name_falls_back_to_task_id(self, receiver):
        """When episode has no task_name, task_id is used as fallback."""
        episode = _sample_episode()
        del episode["task_name"]
        del episode["task_description"]
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data()
        receiver._mock_client.get_episode.return_value = episode

        result = receiver.get_episode_from_server()

        assert result["task"]["name"] == "task-1"
        assert result["task"]["description"] == ""

    def test_task_name_from_episode(self, receiver):
        """task_name and task_description are read from episode response."""
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data()
        receiver._mock_client.get_episode.return_value = _sample_episode(
            task_name="Custom task",
            task_description="Do something special",
        )

        result = receiver.get_episode_from_server()

        assert result["task"]["name"] == "Custom task"
        assert result["task"]["description"] == "Do something special"

    def test_subtask_id_uses_instance_id(self, receiver):
        """Subtask 'id' is taken from the backend 'id' field (instance ID)."""
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data()
        receiver._mock_client.get_episode.return_value = _sample_episode()

        result = receiver.get_episode_from_server()

        # 'id' = episode_subtask instance ID (used in API paths)
        assert result["subtasks"][0]["id"] == "st-1"
        assert result["subtasks"][1]["id"] == "st-2"
        # 'subtask_id' = definition ref (unchanged)
        assert result["subtasks"][0]["subtask_id"] == "sub-def-1"
        assert result["subtasks"][1]["subtask_id"] == "sub-def-2"

    def test_robot_no_active_episode(self, receiver):
        receiver._mock_client.get_robot_self.return_value = {}
        receiver._mock_client.list_episodes.return_value = []
        result = receiver.get_episode_from_server()
        assert result == {}

    def test_newest_ready_episode_is_preloaded_while_idle(self, receiver):
        receiver._mock_client.get_robot_self.return_value = {
            "active_episode_id": None
        }
        receiver._mock_client.list_episodes.return_value = [
            {"id": "ep-newest"},
            {"id": "ep-older"},
        ]
        receiver._mock_client.get_episode.return_value = _sample_episode(
            episode_id="ep-newest",
            task_id="task-cli",
            task_name="CLI Task",
        )

        result = receiver.get_episode_from_server()

        receiver._mock_client.get_episode.assert_called_once_with("ep-newest")
        assert result["episodeId"] == "ep-newest"
        assert result["task"]["name"] == "CLI Task"

    def test_robot_returns_none(self, receiver):
        receiver._mock_client.get_robot_self.return_value = None
        result = receiver.get_episode_from_server()
        assert result == {}

    def test_episode_fetch_fails(self, receiver):
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data()
        receiver._mock_client.get_episode.return_value = None
        result = receiver.get_episode_from_server()
        assert result == {}

    def test_exception_returns_empty(self, receiver):
        receiver._mock_client.get_robot_self.side_effect = RuntimeError("boom")
        result = receiver.get_episode_from_server()
        assert result == {}


# ===================================================================
# TestPublishTask
# ===================================================================


class TestPublishTask:
    """Tests for ``publish_task``."""

    def test_publishes_json(self, receiver):
        receiver._mock_client.get_robot_self.return_value = _sample_robot_data()
        receiver._mock_client.get_episode.return_value = _sample_episode()

        receiver.publish_task()

        receiver.tasks_publisher.publish.assert_called_once()
        msg = receiver.tasks_publisher.publish.call_args[0][0]
        data = json.loads(msg.data)
        assert "episodeId" in data

    def test_publishes_empty_on_no_tasks(self, receiver):
        receiver._mock_client.get_robot_self.return_value = None

        receiver.publish_task()

        receiver.tasks_publisher.publish.assert_called_once()
        msg = receiver.tasks_publisher.publish.call_args[0][0]
        assert json.loads(msg.data) == {}


# ===================================================================
# TestEnrichEpisode
# ===================================================================


class TestEnrichEpisode:
    """Tests for the static ``enrich_episode`` helper."""

    def test_enrich_returns_expected_keys(self, mock_rclpy):
        from galaxis_core.task_receiver import TaskReceiver

        episode = _sample_episode()
        result = TaskReceiver.enrich_episode(episode)

        expected_keys = {
            "episodeId",
            "taskId",
            "taskVersionId",
            "assignedRobotId",
            "createdUserId",
            "assignedUserName",
            "recordedBy",
            "status",
            "subtaskIndex",
            "task",
            "subtasks",
        }
        assert expected_keys.issubset(result.keys())

    def test_enrich_with_active_operator(self, mock_rclpy):
        from galaxis_core.task_receiver import TaskReceiver

        result = TaskReceiver.enrich_episode(
            _sample_episode(),
            active_operator=_sample_active_operator(
                user_id="op-1", display_name="Alice"
            ),
        )

        assert result["recordedBy"] == "op-1"
        assert result["assignedUserName"] == "Alice"

    def test_enrich_episode_recorded_by_takes_precedence(self, mock_rclpy):
        """An explicit episode.recorded_by wins over the live operator.

        This honors deliberate queue-time assignment; the live operator is
        only used as a fallback when the episode has no recorded_by yet.
        """
        from galaxis_core.task_receiver import TaskReceiver

        result = TaskReceiver.enrich_episode(
            _sample_episode(recorded_by="assigned-1"),
            active_operator=_sample_active_operator(user_id="live-2"),
        )

        assert result["recordedBy"] == "assigned-1"

    def test_enrich_sorts_subtasks(self, mock_rclpy):
        from galaxis_core.task_receiver import TaskReceiver

        episode = _sample_episode()
        result = TaskReceiver.enrich_episode(episode)

        assert result["subtasks"][0]["orderIndex"] == 1
        assert result["subtasks"][0]["name"] == "A"

    def test_enrich_task_name_fallback(self, mock_rclpy):
        from galaxis_core.task_receiver import TaskReceiver

        episode = _sample_episode()
        del episode["task_name"]
        result = TaskReceiver.enrich_episode(episode)

        assert result["task"]["name"] == "task-1"

    def test_enrich_preserves_episode_id(self, mock_rclpy):
        from galaxis_core.task_receiver import TaskReceiver

        episode = _sample_episode(episode_id="ep-42")
        result = TaskReceiver.enrich_episode(episode)

        assert result["episodeId"] == "ep-42"


# ===================================================================
# TestPublishEpisodes
# ===================================================================


class TestPublishEpisodes:
    """Tests for ``publish_episodes``."""

    def test_publishes_episodes_list(self, receiver):
        episodes = [{"id": "ep-1"}, {"id": "ep-2"}]
        receiver._mock_client.list_episodes.return_value = episodes

        receiver.publish_episodes()

        receiver.episodes_publisher.publish.assert_called_once()
        msg = receiver.episodes_publisher.publish.call_args[0][0]
        data = json.loads(msg.data)
        assert len(data) == 2
        assert data[0]["id"] == "ep-1"

    def test_publishes_empty_on_none(self, receiver):
        receiver._mock_client.list_episodes.return_value = None

        receiver.publish_episodes()

        receiver.episodes_publisher.publish.assert_called_once()
        msg = receiver.episodes_publisher.publish.call_args[0][0]
        assert json.loads(msg.data) == []

    def test_publishes_empty_on_exception(self, receiver):
        receiver._mock_client.list_episodes.side_effect = RuntimeError("boom")

        receiver.publish_episodes()

        receiver.episodes_publisher.publish.assert_called_once()
        msg = receiver.episodes_publisher.publish.call_args[0][0]
        assert json.loads(msg.data) == []
