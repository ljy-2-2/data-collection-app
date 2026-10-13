#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

CAMERA_IDS = (
    "head_color", "head_stereo_left", "head_stereo_right",
    "head_back_fisheye", "head_left_fisheye", "head_right_fisheye",
    "hand_left_color", "hand_right_color", "head_depth", "hand_left_depth", "hand_right_depth",
)
PR_SET_PDEATHSIG = 1


def exit_with_parent(parent_pid: int) -> None:
    """Ensure a service restart cannot leave this GDK reader orphaned."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def camera_type(gdk, name: str):
    enum_name = "k" + "".join(part.capitalize() for part in name.split("_"))
    return getattr(gdk.CameraType, enum_name)


def camera_timestamps(gdk, bundle_data) -> dict[str, int]:
    timestamps: dict[str, int] = {}
    if bundle_data is None:
        return timestamps
    for key in bundle_data.bundle_info.keys():
        if not key.startswith("timestamp__camera__"):
            continue
        name = key.removeprefix("timestamp__camera__")
        if name in CAMERA_IDS:
            raw = np.asarray(bundle_data.bundle_info[key].data, dtype=np.uint8)
            timestamps[name] = int(raw.view(np.uint64)[0])
    return timestamps


def camera_frame_is_new(last_timestamps: dict[str, int], name: str, timestamp: int) -> bool:
    return last_timestamps.get(name) != timestamp


def image_as_jpeg(gdk, image) -> bytes | None:
    """Normalize every GDK camera encoding to the endpoint's JPEG contract."""
    raw = (
        bytes(image.data)
        if isinstance(image.data, (bytes, bytearray, memoryview))
        else np.asarray(image.data, dtype=np.uint8).tobytes()
    )
    if image.encoding == gdk.Encoding.JPEG:
        return raw
    import cv2

    if image.encoding == gdk.Encoding.PNG:
        decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
    elif image.encoding == gdk.Encoding.UNCOMPRESSED:
        if image.color_format in (gdk.ColorFormat.GRAY16, gdk.ColorFormat.RS2_FORMAT_Z16):
            depth = np.frombuffer(raw, dtype=np.uint16).reshape((image.height, image.width))
            valid = depth > 0
            normalized = np.zeros(depth.shape, dtype=np.uint8)
            if np.any(valid):
                low, high = np.percentile(depth[valid], (2, 98))
                if high > low:
                    normalized[valid] = np.clip(
                        (depth[valid] - low) * 255 / (high - low), 0, 255
                    ).astype(np.uint8)
            decoded = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
        elif image.color_format == gdk.ColorFormat.GRAY8:
            decoded = np.frombuffer(raw, dtype=np.uint8).reshape((image.height, image.width))
        else:
            decoded = np.frombuffer(raw, dtype=np.uint8).reshape((image.height, image.width, 3))
            if image.color_format == gdk.ColorFormat.RGB:
                decoded = cv2.cvtColor(decoded, cv2.COLOR_RGB2BGR)
    else:
        return None
    if decoded is None:
        return None
    encoded, buffer = cv2.imencode(".jpg", decoded, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return buffer.tobytes() if encoded else None


def fetch_exact(camera, camera_kind, timestamp: int, retries: int = 4):
    for attempt in range(retries + 1):
        try:
            image = camera.get_nearest_image(camera_kind, timestamp, 0.0)
        except RuntimeError:
            image = None
        if image is not None and image.timestamp_ns == timestamp:
            return image
        if attempt < retries:
            time.sleep(0.005)
    return None


def joint_positions(robot) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in robot.get_joint_states().get("states", []):
        name = row.get("name", row.get("joint_name"))
        value = row.get("motor_position")
        if name is not None and value is not None and math.isfinite(float(value)):
            result[str(name)] = float(value)
    return result


def dagger_recording(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        payload = json.loads((path / "active.json").read_text())
        os.kill(int(payload["pid"]), 0)
        return payload.get("state") == "recording"
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False


def main() -> int:
    parent_pid = os.getppid()
    exit_with_parent(parent_pid)
    parser = argparse.ArgumentParser(description="Read-only G2 joint and camera preview")
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--selection-file", required=True, type=Path)
    parser.add_argument("--dagger-preview-dir", type=Path)
    args = parser.parse_args()
    args.runtime_dir.mkdir(parents=True, exist_ok=True)

    import agibot_gdk as gdk

    if gdk.gdk_init() != gdk.GDKRes.kSuccess:
        return 1
    robot = gdk.Robot()
    bundle = gdk.Bundle()
    camera = gdk.Camera()
    types = {name: camera_type(gdk, name) for name in CAMERA_IDS}
    last_camera_frame = {name: 0.0 for name in CAMERA_IDS}
    last_camera_timestamp: dict[str, int] = {}
    camera_updated_at: dict[str, str] = {}
    try:
        time.sleep(3.0)
        while True:
            timestamps = camera_timestamps(gdk, bundle.get_latest_bundle(0.0))
            try:
                selected = args.selection_file.read_text().strip()
            except OSError:
                selected = ""
            if selected in {"head_color", "hand_right_color"} and dagger_recording(
                args.dagger_preview_dir
            ):
                selected = ""
            timestamp = timestamps.get(selected)
            if (
                selected in types
                and timestamp is not None
                and camera_frame_is_new(last_camera_timestamp, selected, timestamp)
            ):
                image = fetch_exact(camera, types[selected], timestamp)
                jpeg = image_as_jpeg(gdk, image) if image is not None else None
                if jpeg is not None:
                    atomic_write(args.runtime_dir / f"{selected}.jpg", jpeg)
                    last_camera_frame[selected] = time.monotonic()
                    last_camera_timestamp[selected] = timestamp
                    camera_updated_at[selected] = datetime.now(timezone.utc).isoformat()  # noqa: UP017
            available = [
                name for name, seen_at in last_camera_frame.items()
                if name == selected and time.monotonic() - seen_at < 0.5
            ]
            payload = {
                # This worker runs under the vendor Python 3.10; datetime.UTC
                # is only available in Python 3.11+.
                "updated_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
                "joint_positions": joint_positions(robot),
                "cameras": available,
                "camera_updated_at": {
                    name: camera_updated_at[name]
                    for name in available
                    if name in camera_updated_at
                },
            }
            atomic_write(
                args.runtime_dir / "status.json",
                json.dumps(payload, separators=(",", ":")).encode(),
            )
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            camera.close_camera()
            bundle.close()
            gdk.gdk_release()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
