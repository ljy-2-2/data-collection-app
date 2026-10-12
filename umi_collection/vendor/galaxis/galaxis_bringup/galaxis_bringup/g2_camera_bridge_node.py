"""Receive G2 GDK frames from the host and publish the center-camera source topics."""

from __future__ import annotations

import queue
import socket
import threading
import time
from typing import Any

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, Image

from .g2_camera_protocol import receive_frame


class G2CameraBridgeNode(Node):
    """Translate the host-only CPython 3.10 GDK stream into regular ROS images."""

    OUTPUT_WIDTH = 640
    OUTPUT_HEIGHT = 480

    def __init__(self) -> None:
        super().__init__("g2_camera_bridge")
        self.declare_parameter("host", "127.0.0.1")
        self.declare_parameter("port", 64002)
        self.declare_parameter("frame_id", "center_camera_optical_frame")
        self.declare_parameter("image_topic", "/center_camera/image_raw_unflipped")
        self.declare_parameter("camera_info_topic", "/center_camera/camera_info_unflipped")
        self._host = str(self.get_parameter("host").value)
        self._port = int(self.get_parameter("port").value)
        self._frame_id = str(self.get_parameter("frame_id").value)
        image_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        info_qos = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._image_pub = self.create_publisher(
            Image, str(self.get_parameter("image_topic").value), image_qos
        )
        self._info_pub = self.create_publisher(
            CameraInfo,
            str(self.get_parameter("camera_info_topic").value),
            info_qos,
        )
        self._frames: queue.Queue[tuple[dict[str, Any], bytes]] = queue.Queue(maxsize=1)
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._receive_loop, daemon=True)
        self._worker.start()
        self.create_timer(0.005, self._publish_latest)

    def destroy_node(self):
        self._stop_event.set()
        self._worker.join(timeout=2.0)
        return super().destroy_node()

    def _receive_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with socket.create_connection((self._host, self._port), timeout=2.0) as connection:
                    connection.settimeout(2.0)
                    self.get_logger().info(
                        f"connected to G2 camera source at {self._host}:{self._port}"
                    )
                    while not self._stop_event.is_set():
                        frame = receive_frame(connection)
                        try:
                            self._frames.get_nowait()
                        except queue.Empty:
                            pass
                        self._frames.put_nowait(frame)
            except (ConnectionError, OSError, ValueError) as exc:
                if not self._stop_event.is_set():
                    self.get_logger().warning(f"G2 camera source unavailable: {exc}")
                    time.sleep(1.0)

    @staticmethod
    def _decode_frame(metadata: dict[str, Any], data: bytes) -> np.ndarray:
        encoding = str(metadata.get("encoding", "")).upper()
        color_format = str(metadata.get("color_format", "")).upper()
        width = int(metadata["width"])
        height = int(metadata["height"])
        if encoding in {"JPEG", "PNG"}:
            pixels = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if pixels is None:
                raise ValueError(f"failed to decode G2 {encoding} frame")
            return pixels
        if encoding != "UNCOMPRESSED":
            raise ValueError(f"unsupported G2 encoding: {encoding}")
        if color_format == "RGB":
            pixels = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
            return cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
        if color_format == "BGR":
            return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
        if color_format == "GRAY8":
            gray = np.frombuffer(data, dtype=np.uint8).reshape(height, width)
            return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        raise ValueError(f"unsupported G2 color format: {color_format}")

    def _publish_latest(self) -> None:
        latest = None
        while True:
            try:
                latest = self._frames.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        metadata, data = latest
        try:
            pixels = self._decode_frame(metadata, data)
        except (KeyError, TypeError, ValueError) as exc:
            self.get_logger().error(f"invalid G2 camera frame: {exc}")
            return
        source_height, source_width = pixels.shape[:2]
        if (source_width, source_height) != (self.OUTPUT_WIDTH, self.OUTPUT_HEIGHT):
            pixels = cv2.resize(
                pixels,
                (self.OUTPUT_WIDTH, self.OUTPUT_HEIGHT),
                interpolation=cv2.INTER_LINEAR,
            )
        height, width = pixels.shape[:2]
        stamp_ns = int(metadata.get("timestamp_ns", 0))
        stamp = rclpy.time.Time(nanoseconds=stamp_ns).to_msg()

        image = Image()
        image.header.stamp = stamp
        image.header.frame_id = self._frame_id
        image.height = height
        image.width = width
        image.encoding = "bgr8"
        image.is_bigendian = False
        image.step = width * 3
        image.data = pixels.tobytes()
        self._image_pub.publish(image)

        intrinsic = metadata.get("intrinsic", [0.0, 0.0, 0.0, 0.0])
        distortion = metadata.get("distortion", [])
        if len(intrinsic) != 4:
            intrinsic = [0.0, 0.0, 0.0, 0.0]
        fx, fy, cx, cy = (float(value) for value in intrinsic)
        if source_width > 0 and source_height > 0:
            scale_x = width / source_width
            scale_y = height / source_height
            fx *= scale_x
            cx *= scale_x
            fy *= scale_y
            cy *= scale_y
        info = CameraInfo()
        info.header = image.header
        info.height = height
        info.width = width
        info.distortion_model = "plumb_bob"
        info.d = [float(value) for value in distortion]
        info.k = [fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0]
        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        info.p = [fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0]
        self._info_pub.publish(info)


def main(args=None) -> None:
    rclpy.init(args=args)
    node = G2CameraBridgeNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
