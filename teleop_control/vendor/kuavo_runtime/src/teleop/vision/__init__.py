"""Robot-independent live-video contracts and bridge."""

from .bridge import VideoBridge, VideoHub
from .contracts import VideoFrame, VideoStreamCapability
from .mailbox import LatestFrameMailbox
from .preview import (
    PreviewSummary,
    build_preview_html,
    build_preview_status,
    camera_display_name,
    encode_multipart_jpeg,
    forward_preview_frames,
)
from .protocol import ProtocolError, decode_frame, encode_frame, read_frame, send_frame
from .source import RobotVideoSource

__all__ = [
    "LatestFrameMailbox",
    "PreviewSummary",
    "ProtocolError",
    "RobotVideoSource",
    "VideoBridge",
    "VideoFrame",
    "VideoHub",
    "VideoStreamCapability",
    "build_preview_html",
    "build_preview_status",
    "camera_display_name",
    "decode_frame",
    "encode_multipart_jpeg",
    "encode_frame",
    "forward_preview_frames",
    "read_frame",
    "send_frame",
]
