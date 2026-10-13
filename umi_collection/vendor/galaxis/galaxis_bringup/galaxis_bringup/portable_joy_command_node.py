#!/usr/bin/env python3
"""Aggregate portable input devices into a unified Joy stream.

Subscribes to per-device button states (left/right gripper double-click, PICO controllers)
and publishes them as a single `sensor_msgs/Joy` topic with a fixed button
layout.

Publishes:
  * ``/portable_joy_command``                      (sensor_msgs/Joy)
"""

from typing import Tuple

import rclpy
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy

from sensor_msgs.msg import Joy

# Button index layout for the published Joy. The order is preserved so
# downstream consumers can rely on it.
#
# buttons:
#   [0] start_round: left-controller X OR right-controller A
#   [1] save_and_pause: left-controller Y OR right-controller B
_BTN_START_ROUND = 0
_BTN_SAVE_AND_PAUSE = 1
_NUM_BUTTONS = 2

# (parameter name, default topic) for every topic this node touches.
_TOPIC_PARAMS: Tuple[Tuple[str, str], ...] = (
    ("portable_command_topic", "/portable_joy_command"),
    ("left_double_click_topic", "/galaxis/gripper/left/double_click_state"),
    ("right_double_click_topic", "/galaxis/gripper/right/double_click_state"),
    ("left_pico_topic", "/pico/controller/left/joy"),
    ("right_pico_topic", "/pico/controller/right/joy"),
)


class PortableJoyCommandNode(Node):
    def __init__(self):
        super().__init__("portable_joy_command_publisher")

        for name, default in _TOPIC_PARAMS:
            self.declare_parameter(name, default)

        # PICO face-button state.  Gripper double-click subscriptions are kept
        # for hardware compatibility but deliberately do not control recording.
        self._left_double_click = 0
        self._right_double_click = 0
        self._a_button_state = 0
        self._b_button_state = 0
        self._x_button_state = 0
        self._y_button_state = 0

        # Aggregated buttons
        self._button_states = [0] * _NUM_BUTTONS

        # QoS for PICO topics
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
        )

        # publisher
        self._portable_joy_command_publisher = self.create_publisher(
            Joy,
            self.get_parameter("portable_command_topic").value,
            10,
        )

        # subscribers
        self._left_double_click_subscriber = self.create_subscription(
            Joy,
            self.get_parameter("left_double_click_topic").value,
            self._left_double_click_state_callback,
            10,
        )
        self._right_double_click_subscriber = self.create_subscription(
            Joy,
            self.get_parameter("right_double_click_topic").value,
            self._right_double_click_state_callback,
            10,
        )

        self._left_pico_subscriber = self.create_subscription(
            Joy,
            self.get_parameter("left_pico_topic").value,
            self._left_pico_state_callback,
            qos,
        )
        self._right_pico_subscriber = self.create_subscription(
            Joy,
            self.get_parameter("right_pico_topic").value,
            self._right_pico_state_callback,
            qos,
        )

    # Callbacks. Each updates its own state and trigger publish
    def _left_double_click_state_callback(self, msg: Joy) -> None:
        self._left_double_click = int(msg.buttons[0]) if msg.buttons else 0
        self._publish_portable_joy_command()

    def _right_double_click_state_callback(self, msg: Joy) -> None:
        self._right_double_click = int(msg.buttons[0]) if msg.buttons else 0
        self._publish_portable_joy_command()

    def _left_pico_state_callback(self, msg: Joy) -> None:
        self._x_button_state = int(msg.buttons[0]) if msg.buttons else 0
        self._y_button_state = int(msg.buttons[1]) if len(msg.buttons) > 1 else 0
        self._publish_portable_joy_command()

    def _right_pico_state_callback(self, msg: Joy) -> None:
        self._a_button_state = int(msg.buttons[0]) if msg.buttons else 0
        self._b_button_state = int(msg.buttons[1]) if msg.buttons else 0
        self._publish_portable_joy_command()

    # Publish joy message
    def _publish_portable_joy_command(self) -> None:
        self._button_states[_BTN_START_ROUND] = (
            self._x_button_state | self._a_button_state
        )
        self._button_states[_BTN_SAVE_AND_PAUSE] = (
            self._y_button_state | self._b_button_state
        )

        joy = Joy()
        joy.header.stamp = self.get_clock().now().to_msg()
        joy.header.frame_id = "portable_joy_command"
        joy.axes = []
        joy.buttons = list(self._button_states)
        self._portable_joy_command_publisher.publish(joy)


def main(args=None):
    rclpy.init()
    node = PortableJoyCommandNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
