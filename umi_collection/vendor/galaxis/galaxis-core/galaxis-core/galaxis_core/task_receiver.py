#!/usr/bin/env python3
import json
import rclpy
from rclpy.node import Node

from std_msgs.msg import String

from galaxis_core.backend_client import create_backend


class TaskReceiver(Node):
    def __init__(self):
        super().__init__("task_receiver")

        self.declare_parameter("base_url", "http://localhost:8000/api")
        self.declare_parameter("api_key", "")
        self.declare_parameter("offline_mode", False)
        self.declare_parameter("task_file", "")

        base_url = self.get_parameter("base_url").get_parameter_value().string_value
        api_key = self.get_parameter("api_key").get_parameter_value().string_value
        offline_mode = bool(self.get_parameter("offline_mode").value)
        task_file = str(self.get_parameter("task_file").value)

        self.client = create_backend(
            offline_mode=offline_mode, task_file=task_file,
            base_url=base_url, api_key=api_key,
        )

        self.tasks_publisher = self.create_publisher(String, "/task_receiver/tasks", 10)
        self.episodes_publisher = self.create_publisher(String, "/task_receiver/episodes", 10)
        self.create_timer(1.0, self.publish_task)
        self.create_timer(1.0, self.publish_episodes)

    @staticmethod
    def enrich_episode(episode: dict, active_operator: dict | None = None) -> dict:
        """Transform a raw backend episode into the enriched format used by task_sequence_manager.

        Sorts subtasks by order_index, maps field names, and falls back
        task_name → task_id when task_name is absent.

        ``active_operator`` is the ``RobotOperator`` payload from ``GET /robot/me``
        (live teleop heartbeat). When ``episode.recorded_by`` is already set
        (e.g. assigned at queue creation), we honor that explicit assignment;
        otherwise we fall back to the live operator's ``user_id`` — which is
        the same value the backend will stamp onto ``recorded_by`` at
        ``/start`` time anyway.
        """
        raw_subtasks = episode.get("subtasks", [])
        subtasks = sorted(
            [
                {
                    "id": st.get("id", ""),  # episode_subtask instance ID for API paths
                    "subtask_id": st.get("subtask_id", ""),  # definition ref
                    "name": st.get("name", ""),
                    "orderIndex": st.get("order_index", 0),
                    "status": st.get("status", 0),
                }
                for st in raw_subtasks
            ],
            key=lambda x: x["orderIndex"],
        )

        task_name = episode.get("task_name") or episode["task_id"]
        task_description = episode.get("task_description", "")

        operator = active_operator or {}
        recorded_by = episode.get("recorded_by") or operator.get("user_id") or ""

        return {
            "episodeId": episode["id"],
            "taskId": episode["task_id"],
            "taskVersionId": episode.get("task_version_id", ""),
            "assignedRobotId": episode["robot_id"],
            "createdUserId": episode["user_id"],
            "assignedUserName": operator.get("display_name", ""),
            "recordedBy": recorded_by,
            "status": episode.get("status", 0),
            "subtaskIndex": 0,
            "task": {
                "id": episode["task_id"],
                "name": task_name,
                "description": task_description,
                "round_index": episode.get("task_round_index", 0),
            },
            "subtasks": subtasks,
        }

    def get_episode_from_server(self):
        """Fetch current episode with all enriched data.

        Uses only robot-facing (X-API-Key) endpoints. An already-active
        Episode wins. While idle, the newest Ready Episode is preloaded so a
        command-line-created Episode is visible before the operator presses A.
        """
        try:
            robot_data = self.client.get_robot_self()
            if robot_data is None:
                return {}

            episode_id = robot_data.get("active_episode_id")
            if not episode_id:
                # Robot endpoint is ordered newest-first by created_at.
                ready_episodes = self.client.list_episodes() or []
                if not ready_episodes:
                    return {}
                episode_id = ready_episodes[0].get("id")
                if not episode_id:
                    return {}

            episode = self.client.get_episode(episode_id)
            if not episode:
                self.get_logger().error("Failed to fetch episode details.")
                return {}

            self.get_logger().info("Episode data fetched successfully.")
            return self.enrich_episode(episode, active_operator=robot_data.get("active_operator"))

        except Exception as exc:
            self.get_logger().error(f"Error fetching episode: {exc}")

        return {}

    def publish_task(self):
        episode = self.get_episode_from_server()

        msg = String()
        msg.data = json.dumps(episode)
        self.tasks_publisher.publish(msg)
        self.get_logger().info("Published episode data.")

    def publish_episodes(self):
        """Publish all available episodes on /task_receiver/episodes."""
        try:
            episodes = self.client.list_episodes()
            if episodes is None:
                episodes = []
        except Exception as exc:
            self.get_logger().error(f"Error fetching episodes list: {exc}")
            episodes = []

        msg = String()
        msg.data = json.dumps(episodes)
        self.episodes_publisher.publish(msg)
        self.get_logger().info("Published episodes list.")


def main():
    from galaxis_core.sentry_setup import init_sentry
    init_sentry()
    rclpy.init()
    task_receiver = TaskReceiver()
    try:
        rclpy.spin(task_receiver)
    except KeyboardInterrupt:
        if rclpy.ok():
            task_receiver.get_logger().info("Shutting down task receiver.")
        pass
    finally:
        task_receiver.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
