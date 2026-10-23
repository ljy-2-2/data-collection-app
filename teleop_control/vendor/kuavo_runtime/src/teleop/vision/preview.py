"""Robot-independent helpers for displaying a received JPEG video stream."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html import escape
import json

from .contracts import VideoFrame


MULTIPART_BOUNDARY = "galaxis-frame"

CAMERA_DISPLAY_NAMES = {
    "kHeadColor": "智元｜精灵 G2｜头部彩色相机",
    "kuavo5w-head": "乐聚｜夸父 5-W｜头部彩色相机",
}


@dataclass(frozen=True)
class PreviewSummary:
    frames_displayed: int
    last_sequence: int | None


def camera_display_name(camera_name: str) -> str:
    """Map a protocol camera identity to its user-facing robot source."""

    if not isinstance(camera_name, str) or not camera_name:
        raise ValueError("camera_name must be a non-empty string")
    return CAMERA_DISPLAY_NAMES.get(camera_name, f"机器人相机｜{camera_name}")


def build_preview_status(frame: VideoFrame | None) -> dict[str, object]:
    """Build the browser status from the actual newest protocol frame."""

    if frame is None:
        return {
            "camera_name": None,
            "display_name": "等待视频来源",
            "sequence_id": None,
            "width": 0,
            "height": 0,
        }
    if not isinstance(frame, VideoFrame):
        raise TypeError("frame must be VideoFrame or None")
    return {
        "camera_name": frame.camera_name,
        "display_name": camera_display_name(frame.camera_name),
        "sequence_id": frame.sequence_id,
        "width": frame.width,
        "height": frame.height,
    }


def build_preview_html(
    *,
    camera_name: str | None,
    stream_path: str,
    status_path: str = "/status.json",
) -> bytes:
    """Build the tiny local-only page used by the PC browser preview."""

    if camera_name is None:
        display_name = "等待视频来源"
        camera_identity = ""
    else:
        display_name = camera_display_name(camera_name)
        camera_identity = camera_name
    initial_label = display_name
    if camera_identity:
        initial_label += f" · {camera_identity}"
    safe_title = escape(display_name, quote=True)
    safe_label = escape(initial_label, quote=True)
    safe_stream = escape(stream_path, quote=True)
    status_path_json = json.dumps(status_path).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
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
  <div class="label" id="camera-label">{safe_label}</div>
  <img id="camera-image" src="{safe_stream}" alt="{safe_title}">
  <script>
    const label = document.getElementById("camera-label");
    const image = document.getElementById("camera-image");
    async function refreshCameraSource() {{
      try {{
        const response = await fetch({status_path_json}, {{ cache: "no-store" }});
        if (!response.ok) return;
        const status = await response.json();
        const identity = status.camera_name ? ` · ${{status.camera_name}}` : "";
        label.textContent = `${{status.display_name}}${{identity}}`;
        document.title = status.display_name;
        image.alt = status.display_name;
      }} catch (_error) {{
        // Keep the last verified source label during a short local HTTP interruption.
      }}
    }}
    refreshCameraSource();
    setInterval(refreshCameraSource, 500);
  </script>
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
