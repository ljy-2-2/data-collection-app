"""XR input adapters that feed the robot-independent teleoperation core."""

from .base import XRInputAdapter, XRInputDataError, XRInputError, XRInputUnavailableError
from .xrobotoolkit import XRoboToolkitInputAdapter
from .wire import (
    XRFrameMessage,
    decode_xr_frame_message,
    encode_xr_frame_message,
    message_age_ns,
)

__all__ = [
    "XRInputAdapter",
    "XRInputDataError",
    "XRInputError",
    "XRInputUnavailableError",
    "XRoboToolkitInputAdapter",
    "XRFrameMessage",
    "decode_xr_frame_message",
    "encode_xr_frame_message",
    "message_age_ns",
]
