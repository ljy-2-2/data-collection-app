"""Vertically flip the portable center-camera stream in software."""

from copy import copy, deepcopy
from pathlib import Path

from .camera_devices import read_selection
from .zed_camera import calibration, left_image_layout, serial_number

import cv2
import numpy as np

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, CompressedImage, Image


class CenterCameraFlipNode(Node):
    """Flip raw images and republish their JPEG transport and camera info."""

    def __init__(self) -> None:
        super().__init__("center_camera_flip")
        self.declare_parameter("vertical_flip", False)
        self.declare_parameter('stereo_left_only', False)
        self.declare_parameter('zed_calibration_dir', '/opt/camera-selection/zed-calibrations')
        self.declare_parameter("input_image_topic", "/center_camera/image_raw_unflipped")
        self.declare_parameter("input_transport", "raw")
        self.declare_parameter("input_compressed_topic", "/center_camera/image_raw_unflipped/compressed")
        self.declare_parameter("output_image_topic", "/center_camera/image_raw")
        self.declare_parameter(
            "output_compressed_topic", "/center_camera/image_raw/compressed"
        )
        self.declare_parameter(
            "input_camera_info_topic", "/center_camera/camera_info_unflipped"
        )
        self.declare_parameter("output_camera_info_topic", "/center_camera/camera_info")
        self.declare_parameter("jpeg_quality", 100)

        self._vertical_flip = bool(self.get_parameter("vertical_flip").value)
        self._stereo_left_only = bool(self.get_parameter('stereo_left_only').value)
        self._zed_calibration = None
        self._zed_calibration_key = None
        if self._stereo_left_only and self.get_parameter('input_transport').value != 'raw':
            raise ValueError('ZED stereo crop requires the raw UVC image topic')
        if self._stereo_left_only:
            cv2.setNumThreads(1)
        self._jpeg_quality = max(
            1, min(100, int(self.get_parameter("jpeg_quality").value))
        )
        image_qos = QoSProfile(
            depth=5,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        # web_video_server subscribes to the public raw topic with RELIABLE
        # QoS. Depth one preserves latest-frame behavior without breaking it.
        output_image_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        # CameraInfo is consumed by recording_gate with RELIABLE QoS. Keep the
        # metadata reliable while high-rate images remain latest-frame-only.
        camera_info_qos = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._image_pub = self.create_publisher(
            Image,
            str(self.get_parameter("output_image_topic").value),
            output_image_qos,
        )
        self._compressed_pub = self.create_publisher(
            CompressedImage,
            str(self.get_parameter("output_compressed_topic").value),
            # Recording must not use a one-frame, best-effort writer history:
            # a large JPEG can still be in flight when the next frame arrives.
            # Preview readers may remain best-effort; the bag can use reliable.
            QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE,
                       durability=DurabilityPolicy.VOLATILE),
        )
        self._camera_info_pub = self.create_publisher(
            CameraInfo,
            str(self.get_parameter("output_camera_info_topic").value),
            camera_info_qos,
        )
        if self.get_parameter("input_transport").value == "compressed":
            self.create_subscription(
                CompressedImage,
                str(self.get_parameter("input_compressed_topic").value),
                self._on_compressed,
                image_qos,
            )
        else:
            self.create_subscription(
                Image,
                str(self.get_parameter("input_image_topic").value),
                self._on_image,
                image_qos,
            )
        self.create_subscription(
            CameraInfo,
            str(self.get_parameter("input_camera_info_topic").value),
            self._on_camera_info,
            camera_info_qos,
        )

    def _on_compressed(self, message: CompressedImage) -> None:
        # USB cameras already provide JPEG. Passing it through avoids moving
        # multi-megabyte raw frames between processes and a second lossy encode.
        # The raw path remains available for the G2 bridge, which has no JPEG topic.
        needs_raw = self._image_pub.get_subscription_count() > 0
        if not self._vertical_flip and not needs_raw:
            self._compressed_pub.publish(message)
            return
        pixels = cv2.imdecode(np.frombuffer(message.data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if pixels is None:
            self.get_logger().warning("invalid center-camera JPEG; skipping frame")
            return
        if self._vertical_flip:
            pixels = cv2.flip(pixels, 0)
            ok, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality])
            if not ok:
                self.get_logger().error("failed to JPEG-compress flipped center-camera image")
                return
            output = CompressedImage()
            output.header = message.header
            output.format = "bgr8; jpeg compressed bgr8"
            output.data = encoded.tobytes()
            self._compressed_pub.publish(output)
        else:
            self._compressed_pub.publish(message)
        if needs_raw:
            raw = Image()
            raw.header = message.header
            raw.height, raw.width = pixels.shape[:2]
            raw.encoding = "bgr8"
            raw.step = raw.width * 3
            raw.data = pixels.tobytes()
            self._image_pub.publish(raw)

    def _on_image(self, message: Image) -> None:
        expected_size = message.height * message.step
        if message.height <= 0 or message.step <= 0 or len(message.data) < expected_size:
            self.get_logger().warning("invalid center-camera image; skipping frame")
            return

        rows = np.frombuffer(message.data, dtype=np.uint8, count=expected_size)
        rows = rows.reshape(message.height, message.step)
        if self._stereo_left_only:
            try:
                width,step = left_image_layout(message.width,message.height,message.encoding,message.step)
            except ValueError as exc:
                self.get_logger().error(str(exc))
                return
            # Keep the original capture timestamp and copy no right-eye pixels.
            message = copy(message)
            message.width,message.step = width,step
            rows = rows[:, :step]
        if self._vertical_flip:
            rows = np.flip(rows, axis=0).copy()

        # Recording and the web monitor consume the compressed topic. Avoid a
        # multi-megabyte deepcopy unless a real raw-image subscriber exists.
        if self._image_pub.get_subscription_count() > 0:
            output = copy(message)
            output.data = rows.tobytes()
            self._image_pub.publish(output)
        self._publish_compressed(message, rows)

    def _publish_compressed(self, image: Image, rows: np.ndarray) -> None:
        channels_and_conversion = {
            "rgb8": (3, cv2.COLOR_RGB2BGR),
            "bgr8": (3, None),
            "mono8": (1, None),
        }
        details = channels_and_conversion.get(image.encoding)
        if details is None:
            self.get_logger().error(
                f"cannot JPEG-compress unsupported image encoding {image.encoding!r}"
            )
            return

        channels, conversion = details
        pixels = rows[:, : image.width * channels].reshape(
            image.height, image.width, channels
        )
        if conversion is not None:
            pixels = cv2.cvtColor(pixels, conversion)
        ok, encoded = cv2.imencode(
            ".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality]
        )
        if not ok:
            self.get_logger().error("failed to JPEG-compress flipped center-camera image")
            return

        compressed = CompressedImage()
        compressed.header = image.header
        compressed.format = f"{image.encoding}; jpeg compressed {image.encoding}"
        compressed.data = encoded.tobytes()
        self._compressed_pub.publish(compressed)

    def _on_camera_info(self, message: CameraInfo) -> None:
        output = deepcopy(message)
        if self._stereo_left_only:
            if output.width % 2:
                self.get_logger().error('Invalid ZED stereo CameraInfo width')
                return
            output.width //= 2
            try:
                role = read_selection('/opt/camera-selection/roles.json').get('center',{})
                serial = serial_number(role.get('serial',''))
                key = (serial,output.width,output.height)
                if self._zed_calibration_key != key:
                    p = Path(str(self.get_parameter('zed_calibration_dir').value))/f'SN{serial}.conf'
                    self._zed_calibration = calibration(p.read_text(),output.width,output.height)
                    self._zed_calibration_key = key
                c = self._zed_calibration
            except (OSError,ValueError,KeyError) as exc:
                self.get_logger().error(f'ZED factory calibration unavailable: {exc}')
                return
            output.distortion_model = 'plumb_bob'
            output.d = [c[k] for k in ('k1','k2','p1','p2','k3')]
            output.k = [c['fx'],0.,c['cx'],0.,c['fy'],c['cy'],0.,0.,1.]
            output.r = [1.,0.,0.,0.,1.,0.,0.,0.,1.]
            output.p = [c['fx'],0.,c['cx'],0.,0.,c['fy'],c['cy'],0.,0.,0.,1.,0.]
        if self._vertical_flip and output.height:
            output.k[5] = output.height - 1 - output.k[5]
            output.p[6] = output.height - 1 - output.p[6]
        self._camera_info_pub.publish(output)


def main(args=None) -> None:
    rclpy.init(args=args)
    node = CenterCameraFlipNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
