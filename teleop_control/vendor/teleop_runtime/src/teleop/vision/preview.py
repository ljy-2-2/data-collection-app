"""Robot-independent helpers for displaying a received JPEG video stream."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html import escape

from .contracts import VideoFrame


MULTIPART_BOUNDARY = "galaxis-frame"


@dataclass(frozen=True)
class PreviewSummary:
    frames_displayed: int
    last_sequence: int | None


def build_preview_html(*, camera_name: str, stream_path: str) -> bytes:
    """Build the tiny local-only page used by the PC browser preview."""

    safe_camera = escape(camera_name, quote=True)
    safe_stream = escape(stream_path, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>G2 Head Camera</title>
  <style>
    html, body {{ margin: 0; width: 100%; height: 100%; background: #111; }}
    body {{ display: grid; place-items: center; overflow: hidden; }}
    img {{ max-width: 100vw; max-height: 100vh; object-fit: contain; }}
    .label {{ position: fixed; top: 10px; left: 12px; color: white;
              font: 14px sans-serif; background: #0009; padding: 6px 9px;
              border-radius: 6px; }}
  </style>
</head>
<body>
  <div class="label">G2 Head Camera · {safe_camera}</div>
  <img src="{safe_stream}" alt="Live G2 head camera">
</body>
</html>
""".encode("utf-8")


def forward_preview_frames(
    *,
    read_next: Callable[[], VideoFrame | None],
    display_jpeg: Callable[[bytes], None],
    frame_limit: int | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
) -> PreviewSummary:
    """Forward complete JPEG payloads until the stream or the viewer stops."""

    if frame_limit is not None and (
        isinstance(frame_limit, bool)
        or not isinstance(frame_limit, int)
        or frame_limit < 1
    ):
        raise ValueError("frame_limit must be a positive integer or None")

    displayed = 0
    last_sequence: int | None = None
    while not stop_requested():
        if frame_limit is not None and displayed >= frame_limit:
            break
        frame = read_next()
        if frame is None:
            break
        display_jpeg(frame.payload)
        displayed += 1
        last_sequence = frame.sequence_id

    return PreviewSummary(
        frames_displayed=displayed,
        last_sequence=last_sequence,
    )


def encode_multipart_jpeg(
    payload: bytes, *, boundary: str = MULTIPART_BOUNDARY
) -> bytes:
    """Wrap one JPEG so a streaming decoder can recover variable frame boundaries."""

    if not isinstance(payload, bytes) or not payload.startswith(b"\xff\xd8"):
        raise ValueError("payload must be JPEG bytes")
    if not payload.endswith(b"\xff\xd9"):
        raise ValueError("payload must end with a JPEG marker")
    try:
        boundary_bytes = boundary.encode("ascii")
    except (AttributeError, UnicodeEncodeError) as error:
        raise ValueError("boundary must be a non-empty ASCII string") from error
    if not boundary_bytes or b"\r" in boundary_bytes or b"\n" in boundary_bytes:
        raise ValueError("boundary must be a non-empty ASCII string without newlines")
    return (
        b"--"
        + boundary_bytes
        + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(payload)).encode("ascii")
        + b"\r\n\r\n"
        + payload
        + b"\r\n"
    )
