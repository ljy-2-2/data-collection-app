#!/usr/bin/env python3
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import serial

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64, String

from .encoder_protocol import ENCODER_BAUD, open_encoder_serial, parse_encoder_line


RECONNECT_INTERVAL_SEC = 1.0


@dataclass
class SideState:
    port: str
    baud: int
    lock: threading.Lock
    ser: Optional[serial.Serial] = None
    latest_wrapped: Optional[float] = None
    prev_wrapped: Optional[float] = None
    continuous: Optional[float] = None
    device_id: Optional[str] = None
    connected: bool = False
    last_update_time: Optional[float] = None
    stale: bool = False


def parse_line(ser: serial.Serial, logger=None) -> Tuple[Optional[str], Optional[float]]:
    """Parse a line from the encoder serial port.

    Returns (device_id, angle) tuple. Lines may be:
      "L001,1.57079632"  -> ("L001", 1.5707...)
      "1.57079632"       -> (None, 1.5707...)   # backward compat
      "# ..."            -> (None, None)         # comment/header
    """
    return parse_encoder_line(ser.readline(), logger)


class DualEncoderJointStateNode(Node):
    def __init__(self):
        super().__init__("dual_encoder_jointstate_node")

        # --- Parameters ---
        self.declare_parameter("left_port", "/dev/galaxis_left_esp32c6")
        self.declare_parameter("right_port", "/dev/galaxis_right_esp32c6")
        self.declare_parameter("baud", ENCODER_BAUD)
        self.declare_parameter("active_sides", ["left", "right"])
        self.declare_parameter("collection_profile", "dual")

        self.declare_parameter("left_joint_name", "left_joint")
        self.declare_parameter("right_joint_name", "right_joint")

        self.declare_parameter("frame_id", "")
        self.declare_parameter("topic", "/joint_states")
        self.declare_parameter("device_id_topic", "/device_ids")
        self.declare_parameter("left_joint_state_topic", "/galaxis/gripper/left/joint_state")
        self.declare_parameter("right_joint_state_topic", "/galaxis/gripper/right/joint_state")
        self.declare_parameter("left_device_id_topic", "/galaxis/gripper/left/id")
        self.declare_parameter("right_device_id_topic", "/galaxis/gripper/right/id")
        self.declare_parameter("left_encoder_offset_topic", "/galaxis/gripper/left/encoder_offset")
        self.declare_parameter("right_encoder_offset_topic", "/galaxis/gripper/right/encoder_offset")
        self.declare_parameter("publish_rate_hz", 100.0)
        self.declare_parameter("data_timeout_sec", 0.5)
        
        # Raw minimum values for offset
        self.declare_parameter("left_min_raw", 0.0)
        self.declare_parameter("right_min_raw", 0.0)

        # Rotation direction (+1 normal, -1 inverted)
        self.declare_parameter("left_rotation_direction", 1)
        self.declare_parameter("right_rotation_direction", 1)

        # --- Read params ---
        left_port = self.get_parameter("left_port").get_parameter_value().string_value
        right_port = self.get_parameter("right_port").get_parameter_value().string_value
        baud = int(self.get_parameter("baud").value)
        active_sides = list(self.get_parameter("active_sides").value)
        if not active_sides or any(side not in ("left", "right") for side in active_sides):
            raise ValueError(f"active_sides must contain left and/or right, got {active_sides!r}")
        if len(set(active_sides)) != len(active_sides):
            raise ValueError(f"active_sides contains duplicates: {active_sides!r}")
        self.active_sides = tuple(active_sides)
        self.collection_profile = str(self.get_parameter("collection_profile").value)

        self.left_joint_name = self.get_parameter("left_joint_name").get_parameter_value().string_value
        self.right_joint_name = self.get_parameter("right_joint_name").get_parameter_value().string_value
        self.frame_id = self.get_parameter("frame_id").get_parameter_value().string_value
        topic = self.get_parameter("topic").get_parameter_value().string_value
        device_id_topic = self.get_parameter("device_id_topic").get_parameter_value().string_value
        left_joint_state_topic = self.get_parameter("left_joint_state_topic").get_parameter_value().string_value
        right_joint_state_topic = self.get_parameter("right_joint_state_topic").get_parameter_value().string_value
        left_device_id_topic = self.get_parameter("left_device_id_topic").get_parameter_value().string_value
        right_device_id_topic = self.get_parameter("right_device_id_topic").get_parameter_value().string_value
        left_encoder_offset_topic = self.get_parameter("left_encoder_offset_topic").get_parameter_value().string_value
        right_encoder_offset_topic = self.get_parameter("right_encoder_offset_topic").get_parameter_value().string_value
        rate = float(self.get_parameter("publish_rate_hz").value)
        self.data_timeout_sec = float(self.get_parameter("data_timeout_sec").value)

        self.left_offset = float(self.get_parameter("left_min_raw").value)
        self.right_offset = float(self.get_parameter("right_min_raw").value)

        self.left_direction = int(self.get_parameter("left_rotation_direction").value)
        self.right_direction = int(self.get_parameter("right_rotation_direction").value)

        if self.left_direction == 0:
            self.get_logger().warning("left_rotation_direction cannot be 0. Defaulting to 1.")
            self.left_direction = 1
        if self.right_direction == 0:
            self.get_logger().warning("right_rotation_direction cannot be 0. Defaulting to 1.")
            self.right_direction = 1

        # --- Serial port state (opened lazily in reader threads) ---
        self.left_state = SideState(
            port=left_port,
            baud=baud,
            lock=threading.Lock(),
        )
        self.right_state = SideState(
            port=right_port,
            baud=baud,
            lock=threading.Lock(),
        )

        # --- Background reader threads (so one port never blocks the other) ---
        self._stop = threading.Event()
        self._states = {"left": self.left_state, "right": self.right_state}
        self._threads = [
            threading.Thread(
                target=self._reader_loop,
                args=(side, self._states[side]),
                daemon=True,
            )
            for side in self.active_sides
        ]
        for thread in self._threads:
            thread.start()

        # --- Publisher/timer ---
        self.pub = self.create_publisher(JointState, topic, 10)
        self.left_pub = self.create_publisher(JointState, left_joint_state_topic, 10)
        self.right_pub = self.create_publisher(JointState, right_joint_state_topic, 10)
        self.timer = self.create_timer(1.0 / rate, self.on_timer)

        # --- Device ID publisher (transient_local so rosbag captures it) ---
        id_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.id_pub = self.create_publisher(String, device_id_topic, id_qos)
        self.left_id_pub = self.create_publisher(String, left_device_id_topic, id_qos)
        self.right_id_pub = self.create_publisher(String, right_device_id_topic, id_qos)
        self.left_offset_pub = self.create_publisher(Float64, left_encoder_offset_topic, id_qos)
        self.right_offset_pub = self.create_publisher(Float64, right_encoder_offset_topic, id_qos)
        self.profile_pub = self.create_publisher(String, "/collection_profile", id_qos)
        self._last_id_data = None
        self._last_left_id = None
        self._last_right_id = None

        # --- Publish encoder offsets once (latched via TRANSIENT_LOCAL) ---
        if "left" in self.active_sides:
            left_offset_msg = Float64()
            left_offset_msg.data = self.left_offset
            self.left_offset_pub.publish(left_offset_msg)
        if "right" in self.active_sides:
            right_offset_msg = Float64()
            right_offset_msg.data = self.right_offset
            self.right_offset_pub.publish(right_offset_msg)
        self.profile_pub.publish(String(data=self.collection_profile))

    def destroy_node(self):
        # stop threads and close ports
        try:
            self._stop.set()
        except Exception as e:
            self.get_logger().warning(f"Failed to signal reader threads to stop: {e}")
        for side in self.active_sides:
            st = self._states[side]
            if st.ser is None:
                continue
            try:
                st.ser.close()
            except Exception as e:
                self.get_logger().warning(f"Failed to close {side} serial ({st.port}): {e}")
        super().destroy_node()

    def _wrap_zero_to_twopi(self, rad: float) -> float:
        # Keep final joint positions within [0, 2π)
        rad = math.fmod(rad, 2.0 * math.pi)
        if rad < 0.0:
            rad += 2.0 * math.pi
        return rad

    def _wrap_to_pi(self, rad: float) -> float:
        # Normalize to [-π, π) for delta-based unwrap.
        return (rad + math.pi) % (2.0 * math.pi) - math.pi

    def _apply_offset_invert(self, side: str, rad: float) -> float:
        if side == "left":
            rad = (rad - self.left_offset) * self.left_direction
        else:
            rad = (rad - self.right_offset) * self.right_direction
        return self._wrap_zero_to_twopi(rad)

    def _mark_disconnected(self, side: str, st: SideState, reason: str):
        with st.lock:
            if st.ser is not None:
                try:
                    st.ser.close()
                except Exception as e:
                    self.get_logger().warning(
                        f"Failed to close {side} serial ({st.port}) on disconnect: {e}"
                    )
            st.ser = None
            was_connected = st.connected
            st.connected = False
            st.latest_wrapped = None
            st.prev_wrapped = None
            st.continuous = None
            st.last_update_time = None
            st.stale = False
        if was_connected:
            self.get_logger().warning(
                f"{side} encoder disconnected ({st.port}): {reason}. "
                "Pausing publishing and retrying..."
            )

    def _try_open(self, side: str, st: SideState) -> bool:
        try:
            ser = open_encoder_serial(st.port, baud=st.baud)
        except (serial.SerialException, OSError) as e:
            self.get_logger().debug(f"{side} encoder open failed ({st.port}): {e}")
            return False
        with st.lock:
            st.ser = ser
            st.connected = True
        self.get_logger().info(f"Opened {side} serial: {st.port} @ {st.baud}")
        return True

    def _reader_loop(self, side: str, st: SideState):
        while not self._stop.is_set():
            if st.ser is None:
                if not self._try_open(side, st):
                    if self._stop.wait(RECONNECT_INTERVAL_SEC):
                        return
                    continue

            try:
                device_id, v = parse_line(st.ser, self.get_logger())
            except (serial.SerialException, OSError) as e:
                self._mark_disconnected(side, st, str(e))
                if self._stop.wait(RECONNECT_INTERVAL_SEC):
                    return
                continue

            if v is None:
                continue

            v_wrapped = self._apply_offset_invert(side, v)

            with st.lock:
                if st.prev_wrapped is None:
                    st.continuous = self._wrap_to_pi(v_wrapped)
                else:
                    delta = self._wrap_to_pi(v_wrapped - st.prev_wrapped)
                    st.continuous = st.continuous + delta
                st.prev_wrapped = v_wrapped
                st.latest_wrapped = v_wrapped
                st.last_update_time = time.monotonic()
                if device_id is not None and st.device_id is None:
                    st.device_id = device_id
                    self.get_logger().info(f"Detected {side} encoder device_id: {device_id}")
                    if " " in device_id or "CHANGE" in device_id.upper():
                        self.get_logger().warning(
                            f"{side} encoder has placeholder device_id: {device_id!r}. "
                            "Please flash a proper ID before recording data."
                        )

    def _get_latest(self, side: str, st: SideState) -> Optional[float]:
        with st.lock:
            if st.continuous is None or st.last_update_time is None:
                return None
            age = time.monotonic() - st.last_update_time
            if age > self.data_timeout_sec:
                if not st.stale:
                    st.stale = True
                    self.get_logger().warning(
                        f"{side} encoder data is stale ({age:.2f}s > "
                        f"{self.data_timeout_sec:.2f}s). Pausing publishing."
                    )
                return None
            if st.stale:
                st.stale = False
                self.get_logger().info(f"{side} encoder data resumed.")
            # The calibrated closed position is the lower bound for both the
            # monitor and recordings. Keep the signed unwrap state untouched so
            # noise below zero cannot accumulate into a false opening angle.
            return max(0.0, st.continuous)

    def on_timer(self):
        values = {
            side: self._get_latest(side, self._states[side])
            for side in self.active_sides
        }
        left_v = values.get("left")
        right_v = values.get("right")

        stamp = self.get_clock().now().to_msg()

        if left_v is not None:
            left_msg = JointState()
            left_msg.header.stamp = stamp
            if self.frame_id:
                left_msg.header.frame_id = self.frame_id
            left_msg.name = [self.left_joint_name]
            left_msg.position = [float(left_v)]
            self.left_pub.publish(left_msg)

        if right_v is not None:
            right_msg = JointState()
            right_msg.header.stamp = stamp
            if self.frame_id:
                right_msg.header.frame_id = self.frame_id
            right_msg.name = [self.right_joint_name]
            right_msg.position = [float(right_v)]
            self.right_pub.publish(right_msg)

        if all(values[side] is not None for side in self.active_sides):
            msg = JointState()
            msg.header.stamp = stamp
            if self.frame_id:
                msg.header.frame_id = self.frame_id
            joint_names = {
                "left": self.left_joint_name,
                "right": self.right_joint_name,
            }
            msg.name = [joint_names[side] for side in self.active_sides]
            msg.position = [float(values[side]) for side in self.active_sides]
            self.pub.publish(msg)

        # Publish device IDs only when they change
        with self.left_state.lock:
            left_id = self.left_state.device_id
        with self.right_state.lock:
            right_id = self.right_state.device_id

        if left_id is not None and self._last_left_id != left_id:
            self._last_left_id = left_id
            left_id_msg = String()
            left_id_msg.data = left_id
            self.left_id_pub.publish(left_id_msg)

        if right_id is not None and self._last_right_id != right_id:
            self._last_right_id = right_id
            right_id_msg = String()
            right_id_msg.data = right_id
            self.right_id_pub.publish(right_id_msg)

        ids = {"left": left_id, "right": right_id}
        if all(ids[side] is not None for side in self.active_sides):
            new_data = json.dumps({side: ids[side] for side in self.active_sides})
            if self._last_id_data != new_data:
                self._last_id_data = new_data
                id_msg = String()
                id_msg.data = new_data
                self.id_pub.publish(id_msg)


def main():
    rclpy.init()
    node = DualEncoderJointStateNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
