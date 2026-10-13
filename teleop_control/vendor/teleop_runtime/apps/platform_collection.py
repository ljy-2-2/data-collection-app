"""Data-platform recording bridge for the vendored teleop runtime.

The robot control path stays in ``teleop-runtime``.  This small adapter only
preserves the data-platform's existing A/B recording protocol and action log.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Mapping

ARM_JOINTS = {
    "left": tuple(f"idx2{index}_arm_l_joint{index}" for index in range(1, 8)),
    "right": tuple(f"idx6{index}_arm_r_joint{index}" for index in range(1, 8)),
}
GRIPPER_JOINTS = {
    "left": "idx31_gripper_l_inner_joint1",
    "right": "idx71_gripper_r_inner_joint1",
}
GRIPPER_OPEN_RAD = -0.785
GRIPPER_CLOSED_RAD = 0.0
ACTION_NAMES = (
    *ARM_JOINTS["left"],
    GRIPPER_JOINTS["left"],
    *ARM_JOINTS["right"],
    GRIPPER_JOINTS["right"],
)


class _CollectionClient:
    def __init__(self, socket_path: str, timeout_s: float = 5.0) -> None:
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self._socket.connect(socket_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    self._socket.close()
                    raise RuntimeError(f"collector IPC not ready: {socket_path}") from None
                time.sleep(0.1)
        self._lock = threading.Lock()
        self._closed = False

    def send(self, kind: str, **payload: object) -> bool:
        if self._closed:
            return False
        data = (json.dumps({"type": kind, **payload}, separators=(",", ":")) + "\n").encode()
        try:
            with self._lock:
                self._socket.sendall(data)
            return True
        except OSError:
            self._closed = True
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.close()
        except OSError:
            pass


def _gripper_position(fraction: float) -> float:
    return GRIPPER_OPEN_RAD + float(fraction) * (GRIPPER_CLOSED_RAD - GRIPPER_OPEN_RAD)


class PlatformCollectionBridge:
    """Translate new-runtime frames/results to the existing collector IPC."""

    def __init__(
        self,
        socket_path: str,
        *,
        joint_positions: Mapping[str, float],
        gripper_fractions: Mapping[str, float],
    ) -> None:
        self._client = _CollectionClient(socket_path)
        self._action_state = {
            name: float(joint_positions[name])
            for side in ("left", "right")
            for name in ARM_JOINTS[side]
        }
        for side, name in GRIPPER_JOINTS.items():
            self._action_state[name] = _gripper_position(gripper_fractions[side])
        self._recording_requested = False
        self._previous_a = False
        self._previous_b = False

    def process_buttons(self, frame: object) -> str | None:
        buttons = frame.right_controller.buttons
        pressed_a = buttons.primary and not self._previous_a
        pressed_b = buttons.secondary and not self._previous_b
        self._previous_a = buttons.primary
        self._previous_b = buttons.secondary

        # Preserve the legacy rule: stop wins if both edges arrive together.
        if pressed_b:
            if self._recording_requested and self._client.send(
                "stop", pc_ts_ns=time.time_ns()
            ):
                self._recording_requested = False
                return "stop"
        elif pressed_a and not self._recording_requested:
            if self._client.send("start", pc_ts_ns=time.time_ns()):
                self._recording_requested = True
                return "start"
        return None

    def process_result(self, result: object) -> None:
        changed = False
        if result.validated is not None:
            for name, value in result.validated.targets.items():
                if name in self._action_state:
                    self._action_state[name] = float(value)
                    changed = True
        if result.gripper_validated is not None:
            for side in result.gripper_sides:
                fraction = result.gripper_validated.targets[side]
                self._action_state[GRIPPER_JOINTS[side]] = _gripper_position(fraction)
                changed = True
        if changed:
            self._client.send(
                "action",
                pc_ts_ns=time.time_ns(),
                names=list(ACTION_NAMES),
                action=[self._action_state[name] for name in ACTION_NAMES],
            )

    def close(self) -> None:
        self._client.send("shutdown", pc_ts_ns=time.time_ns())
        self._client.close()
