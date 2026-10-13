from collections.abc import Callable
import importlib
import math
from numbers import Integral
import time
from typing import Any

from teleop.contracts.xr import ControllerButtons, ControllerState, Pose, XRFrame
from teleop.xr.base import XRInputDataError, XRInputUnavailableError


_REQUIRED_SDK_CALLS = (
    "init",
    "close",
    "get_time_stamp_ns",
    "get_headset_pose",
    "get_left_controller_pose",
    "get_right_controller_pose",
    "get_left_axis",
    "get_right_axis",
    "get_left_grip",
    "get_right_grip",
    "get_left_trigger",
    "get_right_trigger",
    "get_X_button",
    "get_Y_button",
    "get_A_button",
    "get_B_button",
    "get_left_menu_button",
    "get_right_menu_button",
    "get_left_axis_click",
    "get_right_axis_click",
)


class XRoboToolkitInputAdapter:
    """Wrap `xrobotoolkit_sdk` and emit robot-independent `XRFrame` values."""

    def __init__(
        self,
        *,
        sdk: Any | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        stale_after_s: float = 0.3,
    ) -> None:
        if isinstance(stale_after_s, bool) or not isinstance(stale_after_s, (int, float)):
            raise ValueError("stale_after_s must be a finite positive number")
        if not math.isfinite(stale_after_s) or stale_after_s <= 0:
            raise ValueError("stale_after_s must be a finite positive number")
        self._sdk = sdk
        self._monotonic_ns = monotonic_ns
        self._stale_after_ns = int(float(stale_after_s) * 1_000_000_000)
        self._started = False
        self._sequence_id = 0
        self._last_device_timestamp_ns = 0
        self._last_advance_monotonic_ns: int | None = None

    def start(self) -> None:
        if self._started:
            return
        sdk = self._sdk
        if sdk is None:
            try:
                sdk = importlib.import_module("xrobotoolkit_sdk")
            except ModuleNotFoundError as error:
                raise XRInputUnavailableError(
                    "xrobotoolkit_sdk is not installed in this Python environment"
                ) from error
            self._sdk = sdk
        missing = [
            name for name in _REQUIRED_SDK_CALLS if not callable(getattr(sdk, name, None))
        ]
        if missing:
            raise XRInputUnavailableError(
                f"xrobotoolkit_sdk is missing required functions: {', '.join(missing)}"
            )
        try:
            sdk.init()
        except Exception as error:
            raise XRInputUnavailableError(
                f"failed to initialize xrobotoolkit_sdk: {error}"
            ) from error
        self._sequence_id = 0
        self._last_device_timestamp_ns = 0
        self._last_advance_monotonic_ns = None
        self._started = True

    def read_frame(self) -> XRFrame | None:
        if not self._started or self._sdk is None:
            raise RuntimeError("XR input adapter must start before read_frame")

        try:
            received_ns = self._read_monotonic_ns()
            timestamp_ns = self._read_device_timestamp_ns()
        except XRInputDataError:
            raise
        except Exception as error:
            raise XRInputDataError(f"failed to read XR timing: {error}") from error
        if timestamp_ns == 0 and self._last_device_timestamp_ns == 0:
            return None
        if timestamp_ns <= 0:
            raise XRInputDataError("device timestamp became invalid after streaming started")
        if timestamp_ns < self._last_device_timestamp_ns:
            raise XRInputDataError("device timestamp moved backwards")
        if timestamp_ns > self._last_device_timestamp_ns:
            self._last_device_timestamp_ns = timestamp_ns
            self._last_advance_monotonic_ns = received_ns

        if self._last_advance_monotonic_ns is None:
            return None
        stale = received_ns - self._last_advance_monotonic_ns > self._stale_after_ns

        try:
            frame = XRFrame(
                sequence_id=self._sequence_id,
                device_timestamp_ns=timestamp_ns,
                received_monotonic_ns=received_ns,
                stale=stale,
                head_pose=Pose.from_sequence(self._sdk.get_headset_pose()),
                left_controller=self._read_controller("left"),
                right_controller=self._read_controller("right"),
                source="xrobotoolkit_sdk",
            )
        except XRInputDataError:
            raise
        except Exception as error:
            raise XRInputDataError(f"invalid XR sample: {error}") from error
        self._sequence_id += 1
        return frame

    def close(self) -> None:
        if not self._started or self._sdk is None:
            return
        try:
            self._sdk.close()
        except Exception as error:
            raise XRInputUnavailableError(
                f"failed to close xrobotoolkit_sdk: {error}"
            ) from error
        self._started = False

    def _read_controller(self, side: str) -> ControllerState:
        if self._sdk is None:
            raise RuntimeError("XR SDK is not initialized")
        if side == "left":
            pose = self._sdk.get_left_controller_pose()
            axis = self._sdk.get_left_axis()
            grip = self._sdk.get_left_grip()
            trigger = self._sdk.get_left_trigger()
            buttons = ControllerButtons(
                primary=self._read_bool("get_X_button"),
                secondary=self._read_bool("get_Y_button"),
                menu=self._read_bool("get_left_menu_button"),
                axis_click=self._read_bool("get_left_axis_click"),
            )
        elif side == "right":
            pose = self._sdk.get_right_controller_pose()
            axis = self._sdk.get_right_axis()
            grip = self._sdk.get_right_grip()
            trigger = self._sdk.get_right_trigger()
            buttons = ControllerButtons(
                primary=self._read_bool("get_A_button"),
                secondary=self._read_bool("get_B_button"),
                menu=self._read_bool("get_right_menu_button"),
                axis_click=self._read_bool("get_right_axis_click"),
            )
        else:
            raise ValueError(f"unsupported controller side: {side}")
        return ControllerState(
            pose=Pose.from_sequence(pose),
            axis=axis,
            grip=grip,
            trigger=trigger,
            buttons=buttons,
        )

    def _read_bool(self, getter_name: str) -> bool:
        if self._sdk is None:
            raise RuntimeError("XR SDK is not initialized")
        value = getattr(self._sdk, getter_name)()
        if not isinstance(value, bool):
            raise XRInputDataError(f"{getter_name} must return bool")
        return value

    def _read_device_timestamp_ns(self) -> int:
        if self._sdk is None:
            raise RuntimeError("XR SDK is not initialized")
        value = self._sdk.get_time_stamp_ns()
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise XRInputDataError("device timestamp must be an integer")
        return int(value)

    def _read_monotonic_ns(self) -> int:
        value = self._monotonic_ns()
        if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
            raise XRInputDataError("monotonic clock must return a non-negative integer")
        return int(value)
