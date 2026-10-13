#!/usr/bin/env python3
"""Detect double-click on the galaxis grippers and publish them as Joy.

Subscribes to per-gripper `sensor_msgs/JointState` topics (left and right),
feeds the gripper width to a per-side `DoubleClickDetector`, and publishes
the resulting double-click state as `sensor_msgs/Joy` on a per-side topic.

The published `buttons[0]=1` is **latched** for `latch_sec` after each
DOUBLE_CLICK event.

Publishes:
  * ``/galaxis/gripper/left/double_click_state``   (sensor_msgs/Joy)
  * ``/galaxis/gripper/right/double_click_state``  (sensor_msgs/Joy)
"""

import time
from typing import Dict, Tuple

import rclpy
from rclpy.node import Node

from sensor_msgs.msg import JointState, Joy

from galaxis_bringup.double_click_detector import (
    DoubleClickDetector,
    Event,
)

# Button index layout for the published Joy.
#
# buttons:
#   [0] double_click  (1 when a double-click is detected, held for latch_sec)
_BTN_DOUBLE_CLICK = 0
_NUM_BUTTONS = 1

# Default latch duration (seconds). Must exceed task_command_dispatch_node's
# button_check_interval (default 50 ms) so its polling reliably sees the rising
# edge. Keep below debounce_sec (default 250 ms) so back-to-back detections are
# not merged into a single press.
_DEFAULT_LATCH_SEC = 0.2

# (parameter name, default topic) for every topic this node touches.
_TOPIC_PARAMS: Tuple[Tuple[str, str], ...] = (
    ("left_joint_state_topic", "/galaxis/gripper/left/joint_state"),
    ("right_joint_state_topic", "/galaxis/gripper/right/joint_state"),
    ("left_double_click_topic", "/galaxis/gripper/left/double_click_state"),
    ("right_double_click_topic", "/galaxis/gripper/right/double_click_state"),
)

class GripperDoubleClickNode(Node):
    def __init__(self):
        super().__init__("gripper_double_click_publisher")

        for name, default in _TOPIC_PARAMS:
            self.declare_parameter(name, default)
        self.declare_parameter("latch_sec", _DEFAULT_LATCH_SEC)
        self._latch_sec = float(self.get_parameter("latch_sec").value)

        # left/right double-click detectors
        self._left_detector = DoubleClickDetector()
        self._right_detector = DoubleClickDetector()

        # Per-detector monotonic deadline; buttons[0]=1 while now < deadline.
        self._latch_until: Dict[DoubleClickDetector, float] = {}

        # publisher
        self._left_publisher = self.create_publisher(
            Joy,
            self.get_parameter("left_double_click_topic").value,
            10,
        )
        self._right_publisher = self.create_publisher(
            Joy,
            self.get_parameter("right_double_click_topic").value,
            10,
        )

        # subscribers
        self._gripper_left_subscription = self.create_subscription(
            JointState,
            self.get_parameter("left_joint_state_topic").value,
            self.left_joint_state_callback,
            10,
        )
        self._gripper_right_subscription = self.create_subscription(
            JointState,
            self.get_parameter("right_joint_state_topic").value,
            self.right_joint_state_callback,
            10,
        )
    
    # Callbacks
    def left_joint_state_callback(self, msg: JointState) -> None:
        self._publish_double_click_state(msg, self._left_detector, self._left_publisher)
    
    def right_joint_state_callback(self, msg: JointState) -> None:
        self._publish_double_click_state(msg, self._right_detector, self._right_publisher)
    
    # Detect double click and publish as Joy
    def _publish_double_click_state(
        self,
        msg: JointState,
        detector: DoubleClickDetector,
        publisher,
    ) -> None:
        if not msg.position:
            return
        width = msg.position[0]
        events = detector.update(width, self)
        now = time.monotonic()

        if Event.DOUBLE_CLICK in events:
            self._latch_until[detector] = now + self._latch_sec
        latched = now < self._latch_until.get(detector, 0.0)

        joy = Joy()
        joy.header.stamp = self.get_clock().now().to_msg()
        joy.header.frame_id = "gripper_double_click"
        joy.axes = []
        joy.buttons = [0] * _NUM_BUTTONS
        joy.buttons[_BTN_DOUBLE_CLICK] = 1 if latched else 0
        publisher.publish(joy)

def main(args=None):
    rclpy.init(args=args)
    node = GripperDoubleClickNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
