#!/usr/bin/env python3
"""Package a completed rosbag2 MCAP recording as a validated HDF5 bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
from pathlib import Path
from typing import Any

import h5py
import numpy as np


FORMAT_NAME = "galaxis-hdf5"
FORMAT_VERSION = 2
COMPACT_FORMAT_VERSION = 3
LOSSLESS_PROFILE = "lossless-cdr"
COMPACT_PROFILE = "compact-video"
FULL_SIGNAL_PROFILE = "all"
UMI_SIGNAL_PROFILE = "umi-pose-gripper-v1"
POSE_STAMPED_TYPE = "geometry_msgs/msg/PoseStamped"
UMI_POSE_TOPICS = {
    "/pico/hmd/state": "galaxis_pico_msgs/msg/PicoHmd",
    "/pico/controller/left/state": "galaxis_pico_msgs/msg/PicoController",
    "/pico/controller/right/state": "galaxis_pico_msgs/msg/PicoController",
}
UMI_GRIPPER_TOPICS = {
    f"/galaxis/gripper/{side}/joint_state": f"{side}_right_finger_joint"
    for side in ("left", "right")
}
UMI_OFFSET_TOPICS = {
    f"/galaxis/gripper/{side}/encoder_offset": f"{side}_encoder_offset_rad"
    for side in ("left", "right")
}
UMI_METADATA_TOPICS = {
    "/device_ids", "/collection_profile",
    "/galaxis/gripper/left/id", "/galaxis/gripper/right/id",
}
COMPRESSED_IMAGE_TYPE = "sensor_msgs/msg/CompressedImage"
STORAGE_EXACT_CDR = 0
STORAGE_EXTERNAL_H264_FRAME = 1
NO_FRAME_INDEX = np.iinfo(np.uint64).max
DEFAULT_VIDEO_CRF = 14
DEFAULT_VIDEO_FPS = 30
DEFAULT_VIDEO_PRESET = "medium"
DEFAULT_BATCH_BYTES = 4 * 1024 * 1024
DEFAULT_BATCH_MESSAGES = 256
VALIDATION_READ_BYTES = 16 * 1024 * 1024


class _CdrNumericReader:
    def __init__(self, payload: bytes) -> None:
        if len(payload) < 4:
            raise ValueError("truncated CDR payload")
        self.payload = payload
        self.endian = "<" if payload[1] & 1 else ">"
        self.base = self.pos = 4

    def _read(self, code: str, alignment: int):
        self.pos += (-(self.pos - self.base)) % alignment
        size = struct.calcsize(code)
        if self.pos + size > len(self.payload):
            raise ValueError("truncated CDR value")
        value = struct.unpack_from(self.endian + code, self.payload, self.pos)[0]
        self.pos += size
        return value

    def u32(self):
        return int(self._read("I", 4))

    def i32(self):
        return int(self._read("i", 4))

    def u64(self):
        return int(self._read("Q", 8))

    def f32(self):
        return float(self._read("f", 4))

    def f64(self):
        return float(self._read("d", 8))

    def boolean(self):
        return bool(self._read("B", 1))

    def string(self) -> str:
        length = self.u32()
        if self.pos + length > len(self.payload):
            raise ValueError("truncated CDR string")
        value = self.payload[self.pos:self.pos + length]
        self.pos += length
        return value.rstrip(b"\0").decode("utf-8", errors="replace")

    def header(self) -> None:
        self.i32()
        self.u32()
        self.string()

    def strings(self):
        return [self.string() for _ in range(self.u32())]

    def floats32(self):
        return [self.f32() for _ in range(self.u32())]

    def floats64(self):
        return [self.f64() for _ in range(self.u32())]

    def ints32(self):
        return [self.i32() for _ in range(self.u32())]


def _numeric_signals(topic: str, message_type: str, payload: bytes) -> dict[str, float]:
    """Extract common robotics signals while retaining the byte-exact payload."""
    reader = _CdrNumericReader(payload)
    if message_type == POSE_STAMPED_TYPE or message_type.startswith("galaxis_pico_msgs/msg/Pico"):
        reader.header()
        if message_type != POSE_STAMPED_TYPE:
            reader.u64()
            reader.u64()
            reader.u32()
            reader.string()
        pose = [reader.f64() for _ in range(7)]
        part = "head" if "/hmd/" in topic else "left_wrist" if "/left/" in topic else "right_wrist"
        result = {
            **{f"{part}.position.{axis}": value for axis, value in zip("xyz", pose[:3])},
            **{f"{part}.orientation.{axis}": value for axis, value in zip("xyzw", pose[3:])},
        }
        if message_type.endswith("PicoController"):
            result[f"controller.{part}.tracked"] = float(reader.boolean())
            result[f"controller.{part}.valid"] = float(reader.boolean())
        return result
    if message_type == "sensor_msgs/msg/JointState":
        reader.header()
        names = reader.strings()
        fields = (("position", reader.floats64()), ("velocity", reader.floats64()),
                  ("effort", reader.floats64()))
        prefix = "observation.state" if topic == "/joint_states" else topic.strip("/").replace("/", ".")
        return {
            f"{prefix}.{re.sub(r'[^a-zA-Z0-9_.-]+', '_', names[index] if index < len(names) else str(index))}.{field}": value
            for field, values in fields for index, value in enumerate(values)
        }
    if message_type == "sensor_msgs/msg/Joy" and topic.startswith("/pico/controller/"):
        reader.header()
        axes = reader.floats32()
        buttons = reader.ints32()
        side = "left" if "/left/" in topic else "right"
        axis_names = ("joystick_x", "joystick_y", "trigger", "grip")
        result = {f"controller.{side}.{axis_names[i] if i < 4 else f'axis_{i}'}": value
                  for i, value in enumerate(axes)}
        result.update({f"controller.{side}.button_{i}": float(value)
                       for i, value in enumerate(buttons)})
        return result
    if message_type == "std_msgs/msg/Float64":
        return {topic.strip("/").replace("/", "."): reader.f64()}
    return {}


def _selected_topic_type(topic: str, message_type: str, profile: str) -> str | None:
    """Select persisted data only; live ROS topics/subscriptions are untouched."""
    if profile == FULL_SIGNAL_PROFILE:
        return message_type
    if profile != UMI_SIGNAL_PROFILE:
        raise ValueError(f"unsupported signal profile: {profile}")
    if message_type == COMPRESSED_IMAGE_TYPE:
        return message_type
    expected = UMI_POSE_TOPICS.get(topic)
    if expected:
        if message_type != expected:
            raise ValueError(f"unexpected pose message type on {topic}: {message_type}")
        return POSE_STAMPED_TYPE
    if topic in UMI_GRIPPER_TOPICS:
        if message_type != "sensor_msgs/msg/JointState":
            raise ValueError(f"unexpected gripper message type on {topic}")
        return message_type
    if topic in UMI_METADATA_TOPICS and message_type == "std_msgs/msg/String":
        return message_type
    return None


def _project_umi_payload(topic: str, payload: bytes) -> bytes:
    """Remove unwanted fields from CDR as well as from the numeric tables.

    Keep the original Header (including its source timestamp and frame), and
    encode standard PoseStamped/JointState CDR. Alignment is relative to the
    four-byte encapsulation, for both little- and big-endian ROS messages.
    """
    if topic not in UMI_POSE_TOPICS and topic not in UMI_GRIPPER_TOPICS:
        return payload
    reader = _CdrNumericReader(payload)
    reader.header()
    result = bytearray(payload[:reader.pos])

    def pack(code: str, alignment: int, value: Any) -> None:
        result.extend(b"\0" * ((-(len(result) - 4)) % alignment))
        result.extend(struct.pack(reader.endian + code, value))

    if topic in UMI_POSE_TOPICS:
        reader.u64()
        reader.u64()
        reader.u32()
        reader.string()
        for _ in range(7):
            value = reader.f64()
            if not np.isfinite(value):
                raise ValueError(f"non-finite pose on {topic}")
            pack("d", 8, value)
    else:
        names = reader.strings()
        positions = reader.floats64()
        name = UMI_GRIPPER_TOPICS[topic]
        if names.count(name) != 1 or names.index(name) >= len(positions):
            raise ValueError(f"missing or ambiguous gripper position on {topic}: {name}")
        position = positions[names.index(name)]
        if not np.isfinite(position):
            raise ValueError(f"non-finite gripper position on {topic}")
        pack("I", 4, 1)  # one joint name
        encoded_name = name.encode("utf-8") + b"\0"
        pack("I", 4, len(encoded_name))
        result.extend(encoded_name)
        pack("I", 4, 1)  # one position
        pack("d", 8, position)
        pack("I", 4, 0)  # no velocity
        pack("I", 4, 0)  # no effort
    return bytes(result)


def _capture_calibration(topic: str, payload: bytes, calibration: dict[str, float]) -> None:
    key = UMI_OFFSET_TOPICS[topic]
    value = _CdrNumericReader(payload).f64()
    if not np.isfinite(value):
        raise ValueError(f"non-finite encoder calibration on {topic}")
    if key in calibration and calibration[key] != value:
        raise ValueError(f"encoder calibration changed during recording on {topic}")
    calibration[key] = value


def _update_digest(
    digest: Any, topic_name: str, timestamp_ns: int, payload: bytes
) -> None:
    """Hash one message with unambiguous field boundaries and global order."""
    topic = topic_name.encode("utf-8")
    digest.update(struct.pack("<Q", len(topic)))
    digest.update(topic)
    digest.update(struct.pack("<qQ", timestamp_ns, len(payload)))
    digest.update(payload)


class Hdf5Writer:
    """Append rosbag messages in their original global read order."""

    def __init__(
        self,
        output: Path,
        *,
        compression: str | None = "gzip",
        compression_level: int = 4,
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        self.output = output
        self._file = h5py.File(output, "w", libver="latest")
        self._file.attrs["format"] = FORMAT_NAME
        self._file.attrs["format_version"] = FORMAT_VERSION
        self._file.attrs["source_storage_id"] = "mcap"
        self._file.attrs["serialization_format"] = "cdr"
        self._file.attrs["complete"] = False
        self._topics_group = self._file.create_group("topics")
        messages = self._file.create_group("messages")

        compression_args: dict[str, Any] = {}
        if compression is not None:
            compression_args["compression"] = compression
            if compression == "gzip":
                compression_args["compression_opts"] = compression_level
        self._data = messages.create_dataset(
            "data", shape=(0,), maxshape=(None,), dtype="u1",
            chunks=(1024 * 1024,), **compression_args,
        )
        self._topic_ids = messages.create_dataset(
            "topic_ids", shape=(0,), maxshape=(None,), dtype="u4",
            chunks=(4096,), **compression_args,
        )
        self._timestamps = messages.create_dataset(
            "timestamps_ns", shape=(0,), maxshape=(None,), dtype="i8",
            chunks=(4096,), **compression_args,
        )
        self._offsets = messages.create_dataset(
            "offsets", shape=(0,), maxshape=(None,), dtype="u8",
            chunks=(4096,), **compression_args,
        )
        self._lengths = messages.create_dataset(
            "lengths", shape=(0,), maxshape=(None,), dtype="u8",
            chunks=(4096,), **compression_args,
        )
        self._topic_ids_by_name: dict[str, int] = {}
        self._pending_data = bytearray()
        self._pending_topic_ids: list[int] = []
        self._pending_timestamps: list[int] = []
        self._pending_offsets: list[int] = []
        self._pending_lengths: list[int] = []
        self._next_offset = 0
        self.message_count = 0

    def register_topic(
        self,
        topic_name: str,
        message_type: str,
        *,
        serialization_format: str = "cdr",
        offered_qos_profiles: str = "",
    ) -> None:
        if topic_name in self._topic_ids_by_name:
            return
        topic_id = len(self._topic_ids_by_name)
        group = self._topics_group.create_group(f"{topic_id:04d}")
        group.attrs["id"] = topic_id
        group.attrs["name"] = topic_name
        group.attrs["type"] = message_type
        group.attrs["serialization_format"] = serialization_format
        group.attrs["offered_qos_profiles"] = offered_qos_profiles
        self._topic_ids_by_name[topic_name] = topic_id

    def set_rosbag_metadata(self, metadata_yaml: str) -> None:
        self._file.create_dataset(
            "rosbag_metadata_yaml",
            data=metadata_yaml,
            dtype=h5py.string_dtype(encoding="utf-8"),
        )

    def append(self, topic_name: str, timestamp_ns: int, payload: bytes) -> None:
        topic_id = self._topic_ids_by_name[topic_name]
        payload_length = len(payload)
        self._pending_topic_ids.append(topic_id)
        self._pending_timestamps.append(timestamp_ns)
        self._pending_offsets.append(self._next_offset)
        self._pending_lengths.append(payload_length)
        self._pending_data.extend(payload)
        self._next_offset += payload_length
        self.message_count += 1
        if (
            len(self._pending_data) >= DEFAULT_BATCH_BYTES
            or len(self._pending_lengths) >= DEFAULT_BATCH_MESSAGES
        ):
            self.flush()

    def flush(self) -> None:
        if not self._pending_lengths:
            return
        data_start = self._data.shape[0]
        data_end = data_start + len(self._pending_data)
        self._data.resize((data_end,))
        if self._pending_data:
            self._data[data_start:data_end] = np.frombuffer(
                self._pending_data, dtype=np.uint8
            )
        index_start = self._lengths.shape[0]
        index_end = index_start + len(self._pending_lengths)
        for dataset, values in (
            (self._topic_ids, self._pending_topic_ids),
            (self._timestamps, self._pending_timestamps),
            (self._offsets, self._pending_offsets),
            (self._lengths, self._pending_lengths),
        ):
            dataset.resize((index_end,))
            dataset[index_start:index_end] = values
        self._pending_data.clear()
        self._pending_topic_ids.clear()
        self._pending_timestamps.clear()
        self._pending_offsets.clear()
        self._pending_lengths.clear()

    def close(self, *, source_digest: str) -> None:
        self.flush()
        self._file.attrs["message_count"] = self.message_count
        self._file.attrs["content_sha256"] = source_digest
        self._file.attrs["complete"] = True
        self._file.flush()
        self._file.close()

    def abort(self) -> None:
        """Close an incomplete output so its temporary file can be discarded."""
        if self._file.id.valid:
            self._file.close()


def _safe_camera_key(topic_name: str, used: set[str]) -> str:
    """Return a stable filename-safe key for one compressed image topic."""
    base = topic_name.strip("/")
    for suffix in ("/image_raw/compressed", "/compressed", "/image_raw"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("_.-") or "camera"
    candidate = base
    serial = 2
    while candidate in used:
        candidate = f"{base}_{serial}"
        serial += 1
    used.add(candidate)
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    """Read JPEG width/height without adding an image-library dependency."""
    if len(payload) < 4 or payload[:2] != b"\xff\xd8":
        raise ValueError("CompressedImage payload is not a JPEG stream")
    index = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while index + 3 < len(payload):
        if payload[index] != 0xFF:
            index += 1
            continue
        while index < len(payload) and payload[index] == 0xFF:
            index += 1
        if index >= len(payload):
            break
        marker = payload[index]
        index += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(payload):
            break
        segment_length = int.from_bytes(payload[index:index + 2], "big")
        if segment_length < 2 or index + segment_length > len(payload):
            break
        if marker in sof_markers:
            if segment_length < 7:
                break
            height = int.from_bytes(payload[index + 3:index + 5], "big")
            width = int.from_bytes(payload[index + 5:index + 7], "big")
            if width <= 0 or height <= 0:
                break
            return width, height
        index += segment_length
    raise ValueError("Could not determine JPEG dimensions")


def _deserialize_compressed_image(payload: bytes) -> tuple[bytes, str]:
    """Return the encoded image bytes and declared ROS image format."""
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import CompressedImage

    message = deserialize_message(payload, CompressedImage)
    return bytes(message.data), str(message.format)


class H264Encoder:
    """Stream JPEG frames to one crash-safe H.264 MP4 partial file."""

    def __init__(
        self,
        output: Path,
        *,
        crf: int,
        fps: int,
        preset: str,
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        self.output = output
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "image2pipe", "-framerate", str(fps),
            "-vcodec", "mjpeg", "-i", "pipe:0", "-an",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-g", str(fps),
            "-movflags", "+faststart", "-f", "mp4", str(output),
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg is required for compact HDF5 video") from exc
        self.frame_count = 0

    def append(self, jpeg: bytes) -> int:
        if self._process.stdin is None:
            raise RuntimeError("ffmpeg input pipe is unavailable")
        frame_index = self.frame_count
        try:
            self._process.stdin.write(jpeg)
        except BrokenPipeError as exc:
            detail = self._read_error()
            raise RuntimeError(f"ffmpeg stopped while encoding: {detail}") from exc
        self.frame_count += 1
        return frame_index

    def _read_error(self) -> str:
        if self._process.stderr is None:
            return "unknown ffmpeg error"
        return self._process.stderr.read().decode("utf-8", errors="replace").strip()

    def close(self) -> None:
        if self._process.stdin is not None and not self._process.stdin.closed:
            self._process.stdin.close()
        detail = self._read_error()
        return_code = self._process.wait()
        if return_code != 0:
            raise RuntimeError(
                f"ffmpeg failed for {self.output} with status {return_code}: {detail}"
            )

    def abort(self) -> None:
        if self._process.poll() is None:
            if self._process.stdin is not None and not self._process.stdin.closed:
                self._process.stdin.close()
            self._process.terminate()
            try:
                self._process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()


class CompactHdf5Writer:
    """Write exact non-image CDR plus a global external-video timeline."""

    def __init__(self, output: Path, *, compression_level: int = 4,
                 signal_profile: str = FULL_SIGNAL_PROFILE) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        self.output = output
        self._file = h5py.File(output, "w", libver="latest")
        self._file.attrs["format"] = FORMAT_NAME
        self._file.attrs["format_version"] = COMPACT_FORMAT_VERSION
        self._file.attrs["storage_profile"] = COMPACT_PROFILE
        self._file.attrs["signal_profile"] = signal_profile
        self.calibration: dict[str, float] = {}
        self._file.attrs["source_storage_id"] = "mcap"
        self._file.attrs["serialization_format"] = "cdr"
        self._file.attrs["complete"] = False
        self._topics_group = self._file.create_group("topics")
        timeline = self._file.create_group("timeline")
        cdr = self._file.create_group("cdr")
        self._videos_group = self._file.create_group("videos")
        compressed = {"compression": "gzip", "compression_opts": compression_level}
        self._cdr_data = cdr.create_dataset(
            "data", shape=(0,), maxshape=(None,), dtype="u1",
            chunks=(1024 * 1024,), **compressed,
        )
        self._topic_ids = timeline.create_dataset(
            "topic_ids", shape=(0,), maxshape=(None,), dtype="u4",
            chunks=(4096,), **compressed,
        )
        self._timestamps = timeline.create_dataset(
            "timestamps_ns", shape=(0,), maxshape=(None,), dtype="i8",
            chunks=(4096,), **compressed,
        )
        self._storage_kinds = timeline.create_dataset(
            "storage_kinds", shape=(0,), maxshape=(None,), dtype="u1",
            chunks=(4096,), **compressed,
        )
        self._cdr_offsets = timeline.create_dataset(
            "cdr_offsets", shape=(0,), maxshape=(None,), dtype="u8",
            chunks=(4096,), **compressed,
        )
        self._cdr_lengths = timeline.create_dataset(
            "cdr_lengths", shape=(0,), maxshape=(None,), dtype="u8",
            chunks=(4096,), **compressed,
        )
        self._video_frame_indices = timeline.create_dataset(
            "video_frame_indices", shape=(0,), maxshape=(None,), dtype="u8",
            chunks=(4096,), **compressed,
        )
        self._topic_ids_by_name: dict[str, int] = {}
        self._topic_storage: dict[str, int] = {}
        self._topic_types: dict[str, str] = {}
        self._signals: dict[str, tuple[str, list[int], list[float]]] = {}
        self._camera_keys: dict[str, str] = {}
        self._camera_timestamps: dict[str, list[int]] = {}
        self._pending_data = bytearray()
        self._pending_topic_ids: list[int] = []
        self._pending_timestamps: list[int] = []
        self._pending_storage_kinds: list[int] = []
        self._pending_cdr_offsets: list[int] = []
        self._pending_cdr_lengths: list[int] = []
        self._pending_video_indices: list[int] = []
        self._next_cdr_offset = 0
        self.message_count = 0
        self.non_image_message_count = 0

    def register_topic(
        self,
        topic_name: str,
        message_type: str,
        *,
        serialization_format: str,
        offered_qos_profiles: str,
        camera_key: str | None = None,
    ) -> None:
        topic_id = len(self._topic_ids_by_name)
        storage_kind = (
            STORAGE_EXTERNAL_H264_FRAME
            if camera_key is not None else STORAGE_EXACT_CDR
        )
        group = self._topics_group.create_group(f"{topic_id:04d}")
        group.attrs["id"] = topic_id
        group.attrs["name"] = topic_name
        group.attrs["type"] = message_type
        group.attrs["serialization_format"] = serialization_format
        group.attrs["offered_qos_profiles"] = offered_qos_profiles
        group.attrs["storage_kind"] = storage_kind
        if camera_key is not None:
            group.attrs["camera_key"] = camera_key
            group.attrs["video_path"] = f"{camera_key}.mp4"
            camera = self._videos_group.create_group(camera_key)
            camera.attrs["source_topic"] = topic_name
            camera.attrs["relative_path"] = f"{camera_key}.mp4"
            self._camera_keys[topic_name] = camera_key
            self._camera_timestamps[camera_key] = []
        self._topic_ids_by_name[topic_name] = topic_id
        self._topic_storage[topic_name] = storage_kind
        self._topic_types[topic_name] = message_type

    def set_rosbag_metadata(self, metadata_yaml: str) -> None:
        self._file.create_dataset(
            "rosbag_metadata_yaml",
            data=metadata_yaml,
            dtype=h5py.string_dtype(encoding="utf-8"),
        )

    def append_exact(self, topic_name: str, timestamp_ns: int, payload: bytes) -> None:
        self._append_timeline(
            topic_name,
            timestamp_ns,
            storage_kind=STORAGE_EXACT_CDR,
            cdr_offset=self._next_cdr_offset,
            cdr_length=len(payload),
            video_frame_index=int(NO_FRAME_INDEX),
        )
        self._pending_data.extend(payload)
        try:
            signals = _numeric_signals(
                topic_name, self._topic_types[topic_name], payload
            )
        except (KeyError, ValueError, struct.error):
            signals = {}
        for key, value in signals.items():
            if not np.isfinite(value):
                continue
            source, timestamps, values = self._signals.setdefault(
                key, (topic_name, [], [])
            )
            timestamps.append(timestamp_ns)
            values.append(float(value))
        self._next_cdr_offset += len(payload)
        self.non_image_message_count += 1

    def append_video(
        self, topic_name: str, timestamp_ns: int, frame_index: int
    ) -> None:
        camera_key = self._camera_keys[topic_name]
        self._camera_timestamps[camera_key].append(timestamp_ns)
        self._append_timeline(
            topic_name,
            timestamp_ns,
            storage_kind=STORAGE_EXTERNAL_H264_FRAME,
            cdr_offset=0,
            cdr_length=0,
            video_frame_index=frame_index,
        )

    def _append_timeline(
        self,
        topic_name: str,
        timestamp_ns: int,
        *,
        storage_kind: int,
        cdr_offset: int,
        cdr_length: int,
        video_frame_index: int,
    ) -> None:
        if self._topic_storage[topic_name] != storage_kind:
            raise ValueError(f"storage kind mismatch for topic {topic_name}")
        self._pending_topic_ids.append(self._topic_ids_by_name[topic_name])
        self._pending_timestamps.append(timestamp_ns)
        self._pending_storage_kinds.append(storage_kind)
        self._pending_cdr_offsets.append(cdr_offset)
        self._pending_cdr_lengths.append(cdr_length)
        self._pending_video_indices.append(video_frame_index)
        self.message_count += 1
        if (
            len(self._pending_data) >= DEFAULT_BATCH_BYTES
            or len(self._pending_topic_ids) >= DEFAULT_BATCH_MESSAGES
        ):
            self.flush()

    def flush(self) -> None:
        if not self._pending_topic_ids:
            return
        data_start = self._cdr_data.shape[0]
        data_end = data_start + len(self._pending_data)
        self._cdr_data.resize((data_end,))
        if self._pending_data:
            self._cdr_data[data_start:data_end] = np.frombuffer(
                self._pending_data, dtype=np.uint8
            )
        index_start = self._topic_ids.shape[0]
        index_end = index_start + len(self._pending_topic_ids)
        for dataset, values in (
            (self._topic_ids, self._pending_topic_ids),
            (self._timestamps, self._pending_timestamps),
            (self._storage_kinds, self._pending_storage_kinds),
            (self._cdr_offsets, self._pending_cdr_offsets),
            (self._cdr_lengths, self._pending_cdr_lengths),
            (self._video_frame_indices, self._pending_video_indices),
        ):
            dataset.resize((index_end,))
            dataset[index_start:index_end] = values
        self._pending_data.clear()
        self._pending_topic_ids.clear()
        self._pending_timestamps.clear()
        self._pending_storage_kinds.clear()
        self._pending_cdr_offsets.clear()
        self._pending_cdr_lengths.clear()
        self._pending_video_indices.clear()

    def close(
        self,
        *,
        source_digest: str,
        non_image_digest: str,
        source_image_digest: str,
        video_metadata: dict[str, dict[str, Any]],
    ) -> None:
        self.flush()
        calibration = self._file.create_group("calibration")
        for key, value in self.calibration.items():
            calibration.attrs[key] = value
        signals_group = self._file.create_group("signals")
        for index, (key, (source_topic, timestamps, values)) in enumerate(
            sorted(self._signals.items())
        ):
            signal = signals_group.create_group(f"{index:04d}")
            signal.attrs["key"] = key
            signal.attrs["label"] = key.replace("_", " ")
            signal.attrs["source_topic"] = source_topic
            signal.create_dataset(
                "timestamps_ns", data=np.asarray(timestamps, dtype=np.int64),
                compression="gzip", compression_opts=4,
            )
            signal.create_dataset(
                "values", data=np.asarray(values, dtype=np.float64),
                compression="gzip", compression_opts=4,
            )
        for camera_key, timestamps in self._camera_timestamps.items():
            camera = self._videos_group[camera_key]
            camera.create_dataset(
                "timestamps_ns",
                data=np.asarray(timestamps, dtype=np.int64),
                compression="gzip",
                compression_opts=4,
            )
            camera.create_dataset(
                "frame_indices",
                data=np.arange(len(timestamps), dtype=np.uint64),
                compression="gzip",
                compression_opts=4,
            )
            for key, value in video_metadata[camera_key].items():
                camera.attrs[key] = value
        self._file.attrs["message_count"] = self.message_count
        self._file.attrs["non_image_message_count"] = self.non_image_message_count
        self._file.attrs["source_content_sha256"] = source_digest
        self._file.attrs["non_image_content_sha256"] = non_image_digest
        self._file.attrs["source_image_payload_sha256"] = source_image_digest
        self._file.attrs["complete"] = True
        self._file.flush()
        self._file.close()

    def abort(self) -> None:
        if self._file.id.valid:
            self._file.close()


def validate_hdf5(
    output: Path,
    expected_count: int,
    expected_digest: str,
    *,
    expected_topics: dict[str, tuple[str, str, str]] | None = None,
    expected_metadata_yaml: str | None = None,
) -> None:
    """Re-read HDF5 and prove its ordered message stream matches the MCAP."""
    digest = hashlib.sha256()
    with h5py.File(output, "r") as handle:
        topics = {
            int(group.attrs["id"]): str(group.attrs["name"])
            for group in handle["topics"].values()
        }
        topic_metadata = {
            str(group.attrs["name"]): (
                str(group.attrs["type"]),
                str(group.attrs["serialization_format"]),
                str(group.attrs["offered_qos_profiles"]),
            )
            for group in handle["topics"].values()
        }
        if expected_topics is not None and topic_metadata != expected_topics:
            raise ValueError("HDF5 topic/type/QoS metadata differs from MCAP")
        if expected_metadata_yaml is not None:
            stored_metadata = handle["rosbag_metadata_yaml"].asstr()[()]
            if stored_metadata != expected_metadata_yaml:
                raise ValueError("HDF5 embedded metadata.yaml differs from MCAP")
        messages = handle["messages"]
        count = len(messages["lengths"])
        if count != expected_count:
            raise ValueError(
                f"HDF5 message count mismatch: expected {expected_count}, got {count}"
            )
        # Read index arrays once and cache large contiguous payload windows.
        # Reading one compressed HDF5 slice per ROS message repeatedly inflates
        # the same chunks and can make finalization many times longer than the
        # recording itself, causing later rounds to remain queued until exit.
        topic_ids = messages["topic_ids"][:]
        timestamps = messages["timestamps_ns"][:]
        offsets = messages["offsets"][:]
        lengths = messages["lengths"][:]
        data = messages["data"]
        data_size = int(data.shape[0])
        cache_start = 0
        cache_end = 0
        cache = np.empty((0,), dtype=np.uint8)
        for index in range(count):
            topic_name = topics[int(topic_ids[index])]
            timestamp_ns = int(timestamps[index])
            offset = int(offsets[index])
            length = int(lengths[index])
            payload_end = offset + length
            if offset < cache_start or payload_end > cache_end:
                cache_start = (offset // VALIDATION_READ_BYTES) * VALIDATION_READ_BYTES
                cache_end = min(
                    data_size,
                    max(cache_start + VALIDATION_READ_BYTES, payload_end),
                )
                cache = data[cache_start:cache_end]
            payload = memoryview(cache)[offset - cache_start : payload_end - cache_start]
            _update_digest(digest, topic_name, timestamp_ns, payload)
        stored_digest = str(handle.attrs["content_sha256"])
        if not bool(handle.attrs["complete"]):
            raise ValueError("HDF5 file is not marked complete")
    actual_digest = digest.hexdigest()
    if actual_digest != expected_digest or stored_digest != expected_digest:
        raise ValueError(
            "HDF5 content checksum mismatch: converted messages differ from MCAP"
        )


def convert_rosbag_lossless(
    input_bag: Path,
    output: Path,
    *,
    compression: str | None = "gzip",
    compression_level: int = 4,
) -> dict[str, Any]:
    """Convert MCAP to legacy HDF5 v2 with byte-exact image CDR."""
    import rosbag2_py

    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing HDF5 file: {output}")
    partial = output.with_name(output.name + ".partial")
    if partial.exists():
        partial.unlink()
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(input_bag), storage_id="mcap"),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr",
            output_serialization_format="cdr",
        ),
    )
    writer = Hdf5Writer(
        partial, compression=compression, compression_level=compression_level
    )
    topic_counts: dict[str, int] = {}
    topic_metadata: dict[str, tuple[str, str, str]] = {}
    try:
        for topic in reader.get_all_topics_and_types():
            serialization_format = getattr(topic, "serialization_format", "cdr")
            offered_qos_profiles = str(getattr(topic, "offered_qos_profiles", ""))
            writer.register_topic(
                topic.name,
                topic.type,
                serialization_format=serialization_format,
                offered_qos_profiles=offered_qos_profiles,
            )
            topic_counts[topic.name] = 0
            topic_metadata[topic.name] = (
                topic.type,
                serialization_format,
                offered_qos_profiles,
            )
        metadata_path = input_bag / "metadata.yaml"
        metadata_yaml = (
            metadata_path.read_text(encoding="utf-8")
            if metadata_path.is_file()
            else ""
        )
        writer.set_rosbag_metadata(metadata_yaml)
        digest = hashlib.sha256()
        message_count = 0
        while reader.has_next():
            topic_name, serialized_data, timestamp_ns = reader.read_next()
            payload = bytes(serialized_data)
            writer.append(topic_name, int(timestamp_ns), payload)
            _update_digest(digest, topic_name, int(timestamp_ns), payload)
            topic_counts[topic_name] = topic_counts.get(topic_name, 0) + 1
            message_count += 1
        source_digest = digest.hexdigest()
        writer.close(source_digest=source_digest)
        validate_hdf5(
            partial,
            message_count,
            source_digest,
            expected_topics=topic_metadata,
            expected_metadata_yaml=metadata_yaml,
        )
        os.replace(partial, output)
    except BaseException:
        writer.abort()
        if partial.exists():
            partial.unlink()
        raise
    return {
        "message_count": message_count,
        "topic_counts": topic_counts,
        "content_sha256": source_digest,
    }


def _probe_video(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-count_frames", "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,nb_read_frames,duration",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe is required for compact HDF5 validation") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"ffprobe failed for {path}: {exc.stderr.strip()}") from exc
    streams = json.loads(completed.stdout).get("streams", [])
    if len(streams) != 1:
        raise ValueError(f"expected one video stream in {path}, got {len(streams)}")
    stream = streams[0]
    required = ("codec_name", "width", "height", "avg_frame_rate", "nb_read_frames")
    if any(key not in stream for key in required):
        raise ValueError(f"ffprobe returned incomplete video metadata for {path}")
    return stream


def _video_metadata(
    path: Path,
    *,
    frame_count: int,
    dimensions: tuple[int, int],
    fps: int,
    crf: int,
    preset: str,
) -> dict[str, Any]:
    probe = _probe_video(path)
    width, height = dimensions
    if probe["codec_name"] != "h264":
        raise ValueError(f"video codec is not H.264: {path}")
    if (int(probe["width"]), int(probe["height"])) != (width, height):
        raise ValueError(f"video resolution differs from source JPEG: {path}")
    if probe["avg_frame_rate"] != f"{fps}/1":
        raise ValueError(f"video frame rate differs from configured {fps} FPS: {path}")
    if int(probe["nb_read_frames"]) != frame_count:
        raise ValueError(f"video frame count differs from source messages: {path}")
    return {
        "codec": "h264",
        "width": width,
        "height": height,
        "fps": fps,
        "frame_count": frame_count,
        "crf": crf,
        "preset": preset,
        "pixel_format": "yuv420p",
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _write_manifest(
    output: Path,
    video_paths: dict[str, Path],
    video_metadata: dict[str, dict[str, Any]],
) -> Path:
    artifacts: list[dict[str, Any]] = [
        {
            "path": output.name,
            "kind": "hdf5",
            "bytes": output.stat().st_size,
            "sha256": _sha256_file(output),
        }
    ]
    for camera_key, path in sorted(video_paths.items()):
        metadata = video_metadata[camera_key]
        artifacts.append(
            {
                "path": path.name,
                "kind": "video",
                **{key: metadata[key] for key in (
                    "codec", "width", "height", "fps", "frame_count",
                    "crf", "preset", "pixel_format", "bytes", "sha256",
                )},
            }
        )
    meta_path = output.parent / "meta.json"
    if meta_path.is_file():
        artifacts.append(
            {
                "path": meta_path.name,
                "kind": "metadata",
                "bytes": meta_path.stat().st_size,
                "sha256": _sha256_file(meta_path),
            }
        )
    manifest = {
        "schema": "galaxis-episode",
        "schema_version": COMPACT_FORMAT_VERSION,
        "storage_profile": COMPACT_PROFILE,
        "state": "complete",
        "episode_id": output.stem,
        "artifacts": artifacts,
    }
    with h5py.File(output, "r") as handle:
        manifest["signal_profile"] = str(handle.attrs.get("signal_profile", FULL_SIGNAL_PROFILE))
        manifest["signal_count"] = len(handle.get("signals", {}))
    manifest_path = output.parent / "manifest.json"
    partial = output.parent / "manifest.json.partial"
    partial.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(partial, manifest_path)
    return manifest_path


def _compact_video_paths(output: Path) -> dict[str, Path]:
    with h5py.File(output, "r") as handle:
        return {
            str(camera_key): output.parent / str(group.attrs["relative_path"])
            for camera_key, group in handle["videos"].items()
            if int(group.attrs.get("frame_count", 0)) > 0
        }


def validate_compact_hdf5(
    input_bag: Path,
    output: Path,
    video_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Validate v3 timeline, exact non-image CDR and every external video."""
    import rosbag2_py

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(input_bag), storage_id="mcap"),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr",
            output_serialization_format="cdr",
        ),
    )
    source_topics = {
        topic.name: (topic.type, getattr(topic, "serialization_format", "cdr"))
        for topic in reader.get_all_topics_and_types()
    }
    metadata_path = input_bag / "metadata.yaml"
    metadata_yaml = metadata_path.read_text(encoding="utf-8") if metadata_path.is_file() else ""
    source_digest = hashlib.sha256()
    non_image_digest = hashlib.sha256()
    source_image_digest = hashlib.sha256()
    image_counts: dict[str, int] = {}
    image_timestamps: dict[str, list[int]] = {}
    image_dimensions: dict[str, tuple[int, int]] = {}
    calibration: dict[str, float] = {}
    expected_signals: dict[str, tuple[list[int], list[float]]] = {}

    with h5py.File(output, "r") as handle:
        if str(handle.attrs.get("format", "")) != FORMAT_NAME:
            raise ValueError("not a Galaxis HDF5 file")
        if int(handle.attrs.get("format_version", 0)) != COMPACT_FORMAT_VERSION:
            raise ValueError("not a compact HDF5 v3 file")
        if str(handle.attrs.get("storage_profile", "")) != COMPACT_PROFILE:
            raise ValueError("unexpected HDF5 storage profile")
        if not bool(handle.attrs.get("complete", False)):
            raise ValueError("HDF5 file is not marked complete")
        signal_profile = str(handle.attrs.get("signal_profile", FULL_SIGNAL_PROFILE))
        selected_topics = {
            name: (selected_type, serialization)
            for name, (message_type, serialization) in source_topics.items()
            if (selected_type := _selected_topic_type(name, message_type, signal_profile)) is not None
        }
        stored_metadata = handle["rosbag_metadata_yaml"].asstr()[()]
        if stored_metadata != metadata_yaml:
            raise ValueError("HDF5 embedded metadata.yaml differs from MCAP")

        topics_by_id: dict[int, dict[str, Any]] = {}
        stored_topics: dict[str, tuple[str, str]] = {}
        for group in handle["topics"].values():
            topic_id = int(group.attrs["id"])
            name = str(group.attrs["name"])
            topics_by_id[topic_id] = {
                "name": name,
                "type": str(group.attrs["type"]),
                "storage_kind": int(group.attrs["storage_kind"]),
                "camera_key": str(group.attrs.get("camera_key", "")),
            }
            stored_topics[name] = (
                str(group.attrs["type"]),
                str(group.attrs["serialization_format"]),
            )
        if stored_topics != selected_topics:
            raise ValueError("HDF5 topic/type/serialization metadata differs from MCAP")

        timeline = handle["timeline"]
        topic_ids = timeline["topic_ids"][:]
        timestamps = timeline["timestamps_ns"][:]
        storage_kinds = timeline["storage_kinds"][:]
        cdr_offsets = timeline["cdr_offsets"][:]
        cdr_lengths = timeline["cdr_lengths"][:]
        video_indices = timeline["video_frame_indices"][:]
        cdr_data = handle["cdr/data"][:]
        index = 0
        while reader.has_next():
            topic_name, serialized_data, timestamp_ns = reader.read_next()
            payload = bytes(serialized_data)
            timestamp_ns = int(timestamp_ns)
            _update_digest(source_digest, topic_name, timestamp_ns, payload)
            if signal_profile == UMI_SIGNAL_PROFILE:
                if topic_name in UMI_OFFSET_TOPICS:
                    _capture_calibration(topic_name, payload, calibration)
                if topic_name not in selected_topics:
                    continue
                payload = _project_umi_payload(topic_name, payload)
            if index >= len(topic_ids):
                raise ValueError("HDF5 timeline has fewer messages than MCAP")
            topic = topics_by_id[int(topic_ids[index])]
            if topic["name"] != topic_name or int(timestamps[index]) != timestamp_ns:
                raise ValueError(f"HDF5 global timeline differs at message {index}")
            if topic["storage_kind"] == STORAGE_EXTERNAL_H264_FRAME:
                if int(storage_kinds[index]) != STORAGE_EXTERNAL_H264_FRAME:
                    raise ValueError(f"invalid video storage kind at message {index}")
                jpeg, image_format = _deserialize_compressed_image(payload)
                if "jpeg" not in image_format.lower() and "jpg" not in image_format.lower():
                    raise ValueError(f"unsupported compressed image format {image_format!r}")
                camera_key = topic["camera_key"]
                expected_frame = image_counts.get(camera_key, 0)
                if int(video_indices[index]) != expected_frame:
                    raise ValueError(f"video frame mapping differs at message {index}")
                dimensions = _jpeg_dimensions(jpeg)
                if camera_key in image_dimensions and image_dimensions[camera_key] != dimensions:
                    raise ValueError(f"camera resolution changes within {camera_key}")
                image_dimensions[camera_key] = dimensions
                image_counts[camera_key] = expected_frame + 1
                image_timestamps.setdefault(camera_key, []).append(timestamp_ns)
                source_image_digest.update(struct.pack("<Q", len(jpeg)))
                source_image_digest.update(jpeg)
            else:
                if int(storage_kinds[index]) != STORAGE_EXACT_CDR:
                    raise ValueError(f"invalid CDR storage kind at message {index}")
                offset = int(cdr_offsets[index])
                length = int(cdr_lengths[index])
                stored_payload = bytes(cdr_data[offset:offset + length])
                if stored_payload != payload:
                    raise ValueError(f"non-image CDR differs at message {index}")
                _update_digest(non_image_digest, topic_name, timestamp_ns, payload)
                if signal_profile == UMI_SIGNAL_PROFILE:
                    for key, value in _numeric_signals(topic_name, topic["type"], payload).items():
                        times, values = expected_signals.setdefault(key, ([], []))
                        times.append(timestamp_ns)
                        values.append(value)
                if int(video_indices[index]) != int(NO_FRAME_INDEX):
                    raise ValueError(f"non-image message has a video frame at {index}")
            index += 1
        if index != len(topic_ids):
            raise ValueError("HDF5 timeline has more messages than MCAP")
        if index != int(handle.attrs["message_count"]):
            raise ValueError("HDF5 message_count attribute is inconsistent")
        if source_digest.hexdigest() != str(handle.attrs["source_content_sha256"]):
            raise ValueError("source message digest differs from HDF5")
        if non_image_digest.hexdigest() != str(handle.attrs["non_image_content_sha256"]):
            raise ValueError("non-image CDR digest differs from HDF5")
        if source_image_digest.hexdigest() != str(handle.attrs["source_image_payload_sha256"]):
            raise ValueError("source JPEG payload digest differs from HDF5")
        if signal_profile == UMI_SIGNAL_PROFILE:
            if dict(handle["calibration"].attrs) != calibration:
                raise ValueError("encoder calibration differs from MCAP")
            stored_signals = {str(g.attrs["key"]): g for g in handle["signals"].values()}
            if len(stored_signals) != len(handle["signals"]) or set(stored_signals) != set(expected_signals):
                raise ValueError("selected numeric signal keys differ from MCAP")
            for key, (times, values) in expected_signals.items():
                signal = stored_signals[key]
                if signal["timestamps_ns"][:].tolist() != times or signal["values"][:].tolist() != values:
                    raise ValueError(f"selected numeric signal values differ from MCAP: {key}")

        resolved_paths = video_paths or _compact_video_paths(output)
        video_metadata: dict[str, dict[str, Any]] = {}
        for camera_key, group in handle["videos"].items():
            expected_count = image_counts.get(camera_key, 0)
            stored_times = group["timestamps_ns"][:].tolist()
            if stored_times != image_timestamps.get(camera_key, []):
                raise ValueError(f"camera timestamps differ for {camera_key}")
            if group["frame_indices"][:].tolist() != list(range(expected_count)):
                raise ValueError(f"camera frame indices differ for {camera_key}")
            if expected_count == 0:
                continue
            path = resolved_paths.get(camera_key)
            if path is None or not path.is_file():
                raise FileNotFoundError(f"missing video for {camera_key}")
            metadata = _video_metadata(
                path,
                frame_count=expected_count,
                dimensions=image_dimensions[camera_key],
                fps=int(group.attrs["fps"]),
                crf=int(group.attrs["crf"]),
                preset=str(group.attrs["preset"]),
            )
            for key in ("codec", "width", "height", "fps", "frame_count", "crf", "preset", "pixel_format", "sha256", "bytes"):
                if str(metadata[key]) != str(group.attrs[key]):
                    raise ValueError(f"video metadata mismatch for {camera_key}: {key}")
            video_metadata[camera_key] = metadata
    return {
        "format_version": COMPACT_FORMAT_VERSION,
        "storage_profile": COMPACT_PROFILE,
        "signal_profile": signal_profile,
        "message_count": index,
        "non_image_content_sha256": non_image_digest.hexdigest(),
        "source_content_sha256": source_digest.hexdigest(),
        "camera_frame_counts": image_counts,
        "video_metadata": video_metadata,
    }


def convert_rosbag_compact(
    input_bag: Path,
    output: Path,
    *,
    compression_level: int = 4,
    video_crf: int = DEFAULT_VIDEO_CRF,
    video_fps: int = DEFAULT_VIDEO_FPS,
    video_preset: str = DEFAULT_VIDEO_PRESET,
    signal_profile: str = FULL_SIGNAL_PROFILE,
) -> dict[str, Any]:
    """Convert MCAP to HDF5 v3 plus timestamp-indexed H.264 camera videos."""
    import rosbag2_py

    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing HDF5 file: {output}")
    partial = output.with_name(output.name + ".partial")
    manifest_partial = output.parent / "manifest.json.partial"
    for stale in (partial, manifest_partial):
        if stale.exists():
            stale.unlink()

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(input_bag), storage_id="mcap"),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr",
            output_serialization_format="cdr",
        ),
    )
    topics = list(reader.get_all_topics_and_types())
    used_camera_keys: set[str] = set()
    cameras: dict[str, str] = {}
    for topic in topics:
        if topic.type == COMPRESSED_IMAGE_TYPE:
            cameras[topic.name] = _safe_camera_key(topic.name, used_camera_keys)

    selected_types = {
        topic.name: selected_type for topic in topics
        if (selected_type := _selected_topic_type(topic.name, topic.type, signal_profile)) is not None
    }
    writer = CompactHdf5Writer(partial, compression_level=compression_level,
                               signal_profile=signal_profile)
    topic_counts: dict[str, int] = {}
    for topic in topics:
        if topic.name not in selected_types:
            continue
        writer.register_topic(
            topic.name,
            selected_types[topic.name],
            serialization_format=getattr(topic, "serialization_format", "cdr"),
            offered_qos_profiles=str(getattr(topic, "offered_qos_profiles", "")),
            camera_key=cameras.get(topic.name),
        )
        topic_counts[topic.name] = 0
    metadata_path = input_bag / "metadata.yaml"
    metadata_yaml = metadata_path.read_text(encoding="utf-8") if metadata_path.is_file() else ""
    writer.set_rosbag_metadata(metadata_yaml)

    encoders: dict[str, H264Encoder] = {}
    partial_videos: dict[str, Path] = {}
    final_videos: dict[str, Path] = {}
    dimensions: dict[str, tuple[int, int]] = {}
    source_digest = hashlib.sha256()
    non_image_digest = hashlib.sha256()
    source_image_digest = hashlib.sha256()
    video_metadata: dict[str, dict[str, Any]] = {}
    try:
        while reader.has_next():
            topic_name, serialized_data, timestamp_ns = reader.read_next()
            payload = bytes(serialized_data)
            timestamp_ns = int(timestamp_ns)
            _update_digest(source_digest, topic_name, timestamp_ns, payload)
            if signal_profile == UMI_SIGNAL_PROFILE:
                if topic_name in UMI_OFFSET_TOPICS:
                    _capture_calibration(topic_name, payload, writer.calibration)
                if topic_name not in selected_types:
                    continue
                payload = _project_umi_payload(topic_name, payload)
            topic_counts[topic_name] = topic_counts.get(topic_name, 0) + 1
            if topic_name not in cameras:
                writer.append_exact(topic_name, timestamp_ns, payload)
                _update_digest(non_image_digest, topic_name, timestamp_ns, payload)
                continue
            jpeg, image_format = _deserialize_compressed_image(payload)
            if "jpeg" not in image_format.lower() and "jpg" not in image_format.lower():
                raise ValueError(
                    f"compact-video requires JPEG CompressedImage, got {image_format!r} on {topic_name}"
                )
            camera_key = cameras[topic_name]
            frame_dimensions = _jpeg_dimensions(jpeg)
            if camera_key in dimensions and dimensions[camera_key] != frame_dimensions:
                raise ValueError(f"camera resolution changes within {topic_name}")
            dimensions[camera_key] = frame_dimensions
            if camera_key not in encoders:
                final_video = output.parent / f"{camera_key}.mp4"
                partial_video = output.parent / f"{camera_key}.mp4.partial"
                if partial_video.exists():
                    partial_video.unlink()
                encoders[camera_key] = H264Encoder(
                    partial_video,
                    crf=video_crf,
                    fps=video_fps,
                    preset=video_preset,
                )
                partial_videos[camera_key] = partial_video
                final_videos[camera_key] = final_video
            frame_index = encoders[camera_key].append(jpeg)
            writer.append_video(topic_name, timestamp_ns, frame_index)
            source_image_digest.update(struct.pack("<Q", len(jpeg)))
            source_image_digest.update(jpeg)

        for encoder in encoders.values():
            encoder.close()
        for camera_key in cameras.values():
            if camera_key not in encoders:
                video_metadata[camera_key] = {
                    "codec": "h264", "width": 0, "height": 0,
                    "fps": video_fps, "frame_count": 0, "crf": video_crf,
                    "preset": video_preset, "pixel_format": "yuv420p",
                    "sha256": "", "bytes": 0,
                }
                continue
            video_metadata[camera_key] = _video_metadata(
                partial_videos[camera_key],
                frame_count=encoders[camera_key].frame_count,
                dimensions=dimensions[camera_key],
                fps=video_fps,
                crf=video_crf,
                preset=video_preset,
            )
        writer.close(
            source_digest=source_digest.hexdigest(),
            non_image_digest=non_image_digest.hexdigest(),
            source_image_digest=source_image_digest.hexdigest(),
            video_metadata=video_metadata,
        )
        result = validate_compact_hdf5(input_bag, partial, partial_videos)
        for camera_key, path in partial_videos.items():
            os.replace(path, final_videos[camera_key])
        os.replace(partial, output)
        manifest = _write_manifest(output, final_videos, video_metadata)
    except BaseException:
        writer.abort()
        for encoder in encoders.values():
            encoder.abort()
        for stale in (partial, manifest_partial, *partial_videos.values()):
            if stale.exists():
                stale.unlink()
        raise

    return {
        **result,
        "topic_counts": topic_counts,
        "hdf5_bytes": output.stat().st_size,
        "video_bytes": sum(path.stat().st_size for path in final_videos.values()),
        "manifest": manifest.name,
    }


def verify_existing_rosbag(input_bag: Path, output: Path) -> dict[str, Any]:
    """Revalidate an existing v2 file or complete v3 bundle against staging."""
    import rosbag2_py

    if not output.is_file():
        raise FileNotFoundError(f"HDF5 file does not exist: {output}")
    with h5py.File(output, "r") as handle:
        format_version = int(handle.attrs.get("format_version", 0))
    if format_version == COMPACT_FORMAT_VERSION:
        result = validate_compact_hdf5(input_bag, output)
        video_paths = _compact_video_paths(output)
        manifest = _write_manifest(
            output, video_paths, result["video_metadata"]
        )
        return {
            **result,
            "hdf5_bytes": output.stat().st_size,
            "video_bytes": sum(path.stat().st_size for path in video_paths.values()),
            "manifest": manifest.name,
        }
    if format_version != FORMAT_VERSION:
        raise ValueError(f"unsupported Galaxis HDF5 format version: {format_version}")
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(input_bag), storage_id="mcap"),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr",
            output_serialization_format="cdr",
        ),
    )
    topic_metadata: dict[str, tuple[str, str, str]] = {}
    for topic in reader.get_all_topics_and_types():
        topic_metadata[topic.name] = (
            topic.type,
            getattr(topic, "serialization_format", "cdr"),
            str(getattr(topic, "offered_qos_profiles", "")),
        )
    metadata_path = input_bag / "metadata.yaml"
    metadata_yaml = (
        metadata_path.read_text(encoding="utf-8")
        if metadata_path.is_file()
        else ""
    )
    digest = hashlib.sha256()
    message_count = 0
    while reader.has_next():
        topic_name, serialized_data, timestamp_ns = reader.read_next()
        _update_digest(digest, topic_name, int(timestamp_ns), bytes(serialized_data))
        message_count += 1
    source_digest = digest.hexdigest()
    validate_hdf5(
        output,
        message_count,
        source_digest,
        expected_topics=topic_metadata,
        expected_metadata_yaml=metadata_yaml,
    )
    return {"message_count": message_count, "content_sha256": source_digest}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--compression", choices=("gzip", "lzf", "none"), default="gzip"
    )
    parser.add_argument(
        "--profile",
        choices=(LOSSLESS_PROFILE, COMPACT_PROFILE),
        default=COMPACT_PROFILE,
    )
    parser.add_argument("--video-crf", type=int, choices=range(0, 52), default=DEFAULT_VIDEO_CRF)
    parser.add_argument("--video-fps", type=int, choices=range(1, 241), default=DEFAULT_VIDEO_FPS)
    parser.add_argument("--video-preset", default=DEFAULT_VIDEO_PRESET)
    parser.add_argument("--signal-profile", choices=(FULL_SIGNAL_PROFILE, UMI_SIGNAL_PROFILE),
                        default=FULL_SIGNAL_PROFILE)
    parser.add_argument(
        "--verify-existing",
        action="store_true",
        help="validate an existing output against the source bag without overwriting it",
    )
    parser.add_argument("--compression-level", type=int, choices=range(1, 10), default=4)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    compression = None if args.compression == "none" else args.compression
    if args.verify_existing:
        result = verify_existing_rosbag(args.input, args.output)
    elif args.profile == COMPACT_PROFILE:
        result = convert_rosbag_compact(
            args.input,
            args.output,
            compression_level=args.compression_level,
            video_crf=args.video_crf,
            video_fps=args.video_fps,
            video_preset=args.video_preset,
            signal_profile=args.signal_profile,
        )
    else:
        result = convert_rosbag_lossless(
            args.input,
            args.output,
            compression=compression,
            compression_level=args.compression_level,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
