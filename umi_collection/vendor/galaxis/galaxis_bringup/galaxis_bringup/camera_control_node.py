"""Apply a V4L2 camera control after a USB camera starts streaming."""

from .camera_devices import discover, read_selection, resolve_roles
from .camera_supervisor import SELECTION

from pathlib import Path
import re
import subprocess

import rclpy
from rclpy.node import Node


class CameraControlNode(Node):
    """Set and read back one hardware V4L2 brightness control."""

    def __init__(self) -> None:
        super().__init__("center_camera_brightness_controller")
        self.declare_parameter("video_device", "/dev/galaxis_center_camera")
        self.declare_parameter("brightness", 0)
        self.declare_parameter("startup_delay_sec", 1.0)
        self.declare_parameter("max_attempts", 10)
        self.declare_parameter("required_confirmations", 2)

        self._video_device = str(self.get_parameter("video_device").value)
        self._role = self._video_device.removeprefix("/dev/galaxis_").removesuffix("_camera")
        self._generation = None
        self._brightness = int(self.get_parameter("brightness").value)
        self._remaining_attempts = int(self.get_parameter("max_attempts").value)
        self._required_confirmations = max(
            1, int(self.get_parameter("required_confirmations").value)
        )
        self._confirmations = 0
        delay = float(self.get_parameter("startup_delay_sec").value)
        self._timer = self.create_timer(max(delay, 0.1), self._apply_brightness)

    def _run_v4l2(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run v4l2-ctl against the selected camera, never touching frames."""
        return subprocess.run(
            ["v4l2-ctl", "--device", self._video_device, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

    def _read_brightness(self) -> int | None:
        result = self._run_v4l2("--get-ctrl=brightness")
        if result.returncode != 0:
            return None
        match = re.search(r"(?:^|\n)\s*brightness\s*:\s*(-?\d+)", result.stdout)
        return int(match.group(1)) if match else None

    def _apply_brightness(self) -> None:
        if SELECTION.exists():
            saved = read_selection(SELECTION)
            selected = resolve_roles(saved, discover()).get(self._role)
            if not selected:
                return
            brightness = saved.get(self._role, {}).get('brightness', self._brightness)
            token = (selected['generation'], brightness)
            if token != self._generation:
                self._generation = token
                self._video_device = selected['device']
                self._brightness = int(brightness)
                self._remaining_attempts = int(self.get_parameter('max_attempts').value)
                self._confirmations = 0
            if self._confirmations >= self._required_confirmations or self._remaining_attempts <= 0:
                return
        if self._remaining_attempts <= 0:
            self.get_logger().error(
                f"could not set brightness={self._brightness} on {self._video_device}"
            )
            if not SELECTION.exists():
                self._timer.cancel()
            return
        self._remaining_attempts -= 1

        if not Path(self._video_device).exists():
            self.get_logger().warning(
                f"camera device not ready: {self._video_device}; retrying"
            )
            return

        try:
            result = self._run_v4l2("--set-ctrl", f"brightness={self._brightness}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.get_logger().warning(f"brightness control failed; retrying: {exc}")
            return

        if result.returncode == 0:
            actual = self._read_brightness()
            if actual != self._brightness:
                detail = "unavailable" if actual is None else str(actual)
                self.get_logger().warning(
                    f"hardware brightness readback is {detail}, expected "
                    f"{self._brightness}; retrying"
                )
                self._confirmations = 0
                return
            self._confirmations += 1
            self.get_logger().info(
                f"verified hardware brightness={actual} on {self._video_device} "
                f"({self._confirmations}/{self._required_confirmations})"
            )
            if self._confirmations >= self._required_confirmations and not SELECTION.exists():
                self._timer.cancel()
            return

        detail = (result.stderr or result.stdout).strip()
        self.get_logger().warning(f"brightness control failed; retrying: {detail}")


def main(args=None) -> None:
    rclpy.init(args=args)
    node = CameraControlNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
