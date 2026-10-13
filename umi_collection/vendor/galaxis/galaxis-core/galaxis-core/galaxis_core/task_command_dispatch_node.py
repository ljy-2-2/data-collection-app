#!/usr/bin/env python3
from typing import Optional, Dict, List

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy
from std_srvs.srv import Trigger
from sensor_msgs.msg import Joy


class TaskCommandDispatchNode(Node):
    def __init__(self):
        super().__init__("task_command_dispatch_node")

        qos_best_effort = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
        )
        self.create_subscription(Joy, "/joy", self.joy_callback, qos_best_effort)

        # Setup parameters for joy buttons
        button_check_interval = self.declare_parameter("button_check_interval", 0.05).get_parameter_value().double_value
        self.debounce_sec = self.declare_parameter("debounce_sec", 0.25).get_parameter_value().double_value
        self.joy_accept_button = self.declare_parameter("joy_accept_button", 2).get_parameter_value().integer_value
        self.joy_reject_button = self.declare_parameter("joy_reject_button", 3).get_parameter_value().integer_value
        self.joy_cancel_episode_button = self.declare_parameter("joy_cancel_episode_button", 0).get_parameter_value().integer_value
        self.joy_next_round_button = self.declare_parameter("joy_next_round_button", -1).get_parameter_value().integer_value
        self.joy_rewind_button = self.declare_parameter("joy_rewind_button", 1).get_parameter_value().integer_value

        # Setup service clients
        self.task_accept_client = self.create_client(Trigger, "/data_collection/accept")
        self.task_reject_client = self.create_client(Trigger, "/data_collection/reject")
        self.task_cancel_episode_client = self.create_client(Trigger, "/data_collection/cancel_episode")
        self.task_rewind_client = self.create_client(Trigger, "/data_collection/rewind")
        self.task_next_round_client = self.create_client(Trigger, "/data_collection/next_round")

        # wait for service servers to be available
        for c in [
            self.task_accept_client,
            self.task_reject_client,
            self.task_cancel_episode_client,
            self.task_rewind_client,
            self.task_next_round_client,
        ]:
            while not c.wait_for_service(timeout_sec=1.0):
                self.get_logger().info(f"Waiting for service {c.srv_name} to be available...")

        self.joy_msg: Optional[Joy] = None
        self.prev_buttons: Optional[List[int]] = None
        self.last_fire_time: Dict[int, float] = {}
        self.inflight: Dict[str, bool] = {
            "accept": False,
            "reject": False,
            "cancel_episode": False,
            "rewind": False,
            "next_round": False,
        }

        self.timer = self.create_timer(button_check_interval, self.task_process_callback)

    def joy_callback(self, msg: Joy):
        self.joy_msg = msg

    def _finish(self, key: str, future: rclpy.Future, ok_msg: str, ng_msg: str):
        self.inflight[key] = False
        try:
            result = future.result()
        except Exception as e:
            self.get_logger().error(f"{ng_msg} Exception: {e}")
            return 
        if result.success:
            self.get_logger().info(f"{ok_msg}: {result.message}")
        else:
            self.get_logger().warn(f"{ng_msg}: {result.message}")

    def accept_response_cb(self, future: rclpy.Future):
        self._finish("accept", future, "Task accepted", "Failed to accept task")

    def reject_response_cb(self, future: rclpy.Future):
        self._finish("reject", future, "Task rejected", "Failed to reject task")

    def cancel_episode_response_cb(self, future: rclpy.Future):
        self._finish("cancel_episode", future, "Episode cancelled", "Failed to cancel episode")

    def rewind_response_cb(self, future: rclpy.Future):
        self._finish("rewind", future, "Rewinded successfully", "Failed to rewind")

    def next_round_response_cb(self, future: rclpy.Future):
        self._finish("next_round", future, "Round saved; collection paused", "Failed to save round")

    @staticmethod
    def _safe_button(buttons: List[int], index: int) -> Optional[int]:
        if index < 0 or index >= len(buttons):
            return 0
        v = buttons[index]
        return 1 if v else 0

    def _rising_edge(self, button_index: int, curr_buttons: List[int]) -> bool:
        curr = self._safe_button(curr_buttons, button_index)
        prev = 0 if self.prev_buttons is None else self._safe_button(self.prev_buttons, button_index)
        if prev == 0 and curr == 1:
            now = self.get_clock().now().seconds_nanoseconds()[0] + self.get_clock().now().seconds_nanoseconds()[1] * 1e-9
            last_fire = self.last_fire_time.get(button_index, 0.0)
            if now - last_fire >= self.debounce_sec:
                self.last_fire_time[button_index] = now
                return True
        return False

    def task_process_callback(self):
        msg  = self.joy_msg
        if msg is None:
            return

        curr_buttons = list(msg.buttons) if msg.buttons is not None else []
        if self._rising_edge(self.joy_accept_button, curr_buttons):
            if not self.inflight["accept"]:
                self.inflight["accept"] = True
                future = self.task_accept_client.call_async(Trigger.Request())
                future.add_done_callback(self.accept_response_cb)
        if self._rising_edge(self.joy_reject_button, curr_buttons):
            if not self.inflight["reject"]:
                self.inflight["reject"] = True
                future = self.task_reject_client.call_async(Trigger.Request())
                future.add_done_callback(self.reject_response_cb)
        if self._rising_edge(self.joy_cancel_episode_button, curr_buttons):
            if not self.inflight["cancel_episode"]:
                self.inflight["cancel_episode"] = True
                future = self.task_cancel_episode_client.call_async(Trigger.Request())
                future.add_done_callback(self.cancel_episode_response_cb)
        if self._rising_edge(self.joy_rewind_button, curr_buttons): 
            if not self.inflight["rewind"]:
                self.inflight["rewind"] = True
                future = self.task_rewind_client.call_async(Trigger.Request())
                future.add_done_callback(self.rewind_response_cb)
        if self._rising_edge(self.joy_next_round_button, curr_buttons):
            if not self.inflight["next_round"]:
                self.inflight["next_round"] = True
                future = self.task_next_round_client.call_async(Trigger.Request())
                future.add_done_callback(self.next_round_response_cb)

        self.prev_buttons = curr_buttons

def main(args=None):
    from galaxis_core.sentry_setup import init_sentry
    init_sentry()
    rclpy.init(args=args)
    node = TaskCommandDispatchNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        print("Keyboard interrupt received, shutting down task command dispatch node.")
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == "__main__":
    main()
