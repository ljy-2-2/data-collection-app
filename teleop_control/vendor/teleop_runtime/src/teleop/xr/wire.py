"""Strict newline-delimited JSON transport for robot-independent XRFrame values."""

from dataclasses import dataclass
import json
from typing import Mapping

from teleop.contracts.xr import (
    ControllerButtons,
    ControllerState,
    Pose,
    XRFrame,
)


WIRE_VERSION = 1
DEFAULT_MAX_MESSAGE_AGE_NS = 200_000_000
_MESSAGE_KEYS = {"version", "emitted_monotonic_ns", "frame"}
_FRAME_KEYS = {
    "sequence_id",
    "device_timestamp_ns",
    "received_monotonic_ns",
    "stale",
    "head_pose",
    "left_controller",
    "right_controller",
    "source",
    "coordinate_frame",
}
_CONTROLLER_KEYS = {"pose", "axis", "grip", "trigger", "buttons"}
_BUTTON_KEYS = {"primary", "secondary", "menu", "axis_click"}


@dataclass(frozen=True)
class XRFrameMessage:
    """One XRFrame plus the host monotonic time at which it entered the pipe."""

    emitted_monotonic_ns: int
    frame: XRFrame

    def __post_init__(self) -> None:
        if type(self.emitted_monotonic_ns) is not int or self.emitted_monotonic_ns < 0:
            raise ValueError("emitted_monotonic_ns must be a non-negative integer")
        if not isinstance(self.frame, XRFrame):
            raise ValueError("frame must be an XRFrame")


def _pose_payload(pose: Pose) -> list[float]:
    return list(pose.xyz_xyzw)


def _controller_payload(controller: ControllerState) -> dict[str, object]:
    return {
        "pose": _pose_payload(controller.pose),
        "axis": list(controller.axis),
        "grip": controller.grip,
        "trigger": controller.trigger,
        "buttons": {
            "primary": controller.buttons.primary,
            "secondary": controller.buttons.secondary,
            "menu": controller.buttons.menu,
            "axis_click": controller.buttons.axis_click,
        },
    }


def encode_xr_frame_message(message: XRFrameMessage) -> str:
    if not isinstance(message, XRFrameMessage):
        raise TypeError("message must be an XRFrameMessage")
    frame = message.frame
    return json.dumps(
        {
            "version": WIRE_VERSION,
            "emitted_monotonic_ns": message.emitted_monotonic_ns,
            "frame": {
                "sequence_id": frame.sequence_id,
                "device_timestamp_ns": frame.device_timestamp_ns,
                "received_monotonic_ns": frame.received_monotonic_ns,
                "stale": frame.stale,
                "head_pose": _pose_payload(frame.head_pose),
                "left_controller": _controller_payload(frame.left_controller),
                "right_controller": _controller_payload(frame.right_controller),
                "source": frame.source,
                "coordinate_frame": frame.coordinate_frame,
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _strict_mapping(value: object, keys: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name} fields do not match XR wire version 1")
    return value


def _decode_controller(value: object, name: str) -> ControllerState:
    payload = _strict_mapping(value, _CONTROLLER_KEYS, name)
    buttons = _strict_mapping(payload["buttons"], _BUTTON_KEYS, f"{name}.buttons")
    return ControllerState(
        pose=Pose.from_sequence(payload["pose"]),  # type: ignore[arg-type]
        axis=payload["axis"],  # type: ignore[arg-type]
        grip=payload["grip"],  # type: ignore[arg-type]
        trigger=payload["trigger"],  # type: ignore[arg-type]
        buttons=ControllerButtons(
            primary=buttons["primary"],  # type: ignore[arg-type]
            secondary=buttons["secondary"],  # type: ignore[arg-type]
            menu=buttons["menu"],  # type: ignore[arg-type]
            axis_click=buttons["axis_click"],  # type: ignore[arg-type]
        ),
    )


def decode_xr_frame_message(line: str) -> XRFrameMessage:
    if not isinstance(line, str):
        raise ValueError("XR frame message must be text")
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError("invalid XR frame message JSON") from error
    payload = _strict_mapping(raw, _MESSAGE_KEYS, "message")
    if type(payload["version"]) is not int or payload["version"] != WIRE_VERSION:
        raise ValueError("unsupported XR frame wire version")
    frame = _strict_mapping(payload["frame"], _FRAME_KEYS, "frame")
    try:
        decoded_frame = XRFrame(
            sequence_id=frame["sequence_id"],  # type: ignore[arg-type]
            device_timestamp_ns=frame["device_timestamp_ns"],  # type: ignore[arg-type]
            received_monotonic_ns=frame["received_monotonic_ns"],  # type: ignore[arg-type]
            stale=frame["stale"],  # type: ignore[arg-type]
            head_pose=Pose.from_sequence(frame["head_pose"]),  # type: ignore[arg-type]
            left_controller=_decode_controller(
                frame["left_controller"], "left_controller"
            ),
            right_controller=_decode_controller(
                frame["right_controller"], "right_controller"
            ),
            source=frame["source"],  # type: ignore[arg-type]
            coordinate_frame=frame["coordinate_frame"],  # type: ignore[arg-type]
        )
        return XRFrameMessage(
            emitted_monotonic_ns=payload["emitted_monotonic_ns"],  # type: ignore[arg-type]
            frame=decoded_frame,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid XR frame message: {error}") from error


def message_age_ns(
    message: XRFrameMessage,
    now_ns: int,
    *,
    max_age_ns: int = DEFAULT_MAX_MESSAGE_AGE_NS,
) -> int:
    if not isinstance(message, XRFrameMessage):
        raise TypeError("message must be an XRFrameMessage")
    if type(now_ns) is not int or now_ns < 0:
        raise ValueError("monotonic clock must return a non-negative integer")
    if type(max_age_ns) is not int or max_age_ns <= 0:
        raise ValueError("max_age_ns must be a positive integer")
    age_ns = now_ns - message.emitted_monotonic_ns
    if age_ns < 0:
        raise ValueError("XR frame timestamp is in the future")
    if age_ns > max_age_ns:
        raise ValueError("XR frame message is stale")
    return age_ns
