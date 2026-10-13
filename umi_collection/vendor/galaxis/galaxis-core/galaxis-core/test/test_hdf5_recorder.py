"""HDF5 storage schema tests (skipped when the host lacks h5py)."""

import hashlib
import importlib
import struct

import pytest

h5py = pytest.importorskip("h5py")


def test_writer_preserves_topic_type_timestamps_and_serialized_bytes(tmp_path):
    module = importlib.import_module("galaxis_core.hdf5_recorder")
    output = tmp_path / "round.h5"

    writer = module.Hdf5Writer(output, compression="gzip", compression_level=1)
    writer.register_topic("/camera/image", "sensor_msgs/msg/CompressedImage")
    writer.register_topic("/joint_states", "sensor_msgs/msg/JointState")
    messages = [
        ("/camera/image", 101, b"\x00\x01abc"),
        ("/joint_states", 150, b"joint"),
        ("/camera/image", 202, b"xyz"),
    ]
    digest = hashlib.sha256()
    for topic_name, timestamp_ns, payload in messages:
        writer.append(topic_name, timestamp_ns, payload)
        module._update_digest(digest, topic_name, timestamp_ns, payload)
    expected_digest = digest.hexdigest()
    writer.close(source_digest=expected_digest)
    module.validate_hdf5(output, 3, expected_digest)

    with h5py.File(output, "r") as handle:
        assert handle.attrs["format"] == "galaxis-hdf5"
        assert bool(handle.attrs["complete"]) is True
        topic = handle["topics"]["0000"]
        assert topic.attrs["name"] == "/camera/image"
        assert topic.attrs["type"] == "sensor_msgs/msg/CompressedImage"
        recorded = handle["messages"]
        assert recorded["topic_ids"][:].tolist() == [0, 1, 0]
        assert recorded["timestamps_ns"][:].tolist() == [101, 150, 202]
        assert recorded["offsets"][:].tolist() == [0, 5, 10]
        assert recorded["lengths"][:].tolist() == [5, 5, 3]
        assert bytes(recorded["data"][:]) == b"\x00\x01abcjointxyz"
        assert handle.attrs["content_sha256"] == expected_digest

    with h5py.File(output, "r+") as handle:
        handle["messages"]["data"][0] = 255
    with pytest.raises(ValueError, match="checksum mismatch"):
        module.validate_hdf5(output, 3, expected_digest)


def test_compact_writer_keeps_exact_non_image_cdr_and_video_timeline(tmp_path):
    module = importlib.import_module("galaxis_core.hdf5_recorder")
    output = tmp_path / "round.h5.partial"
    writer = module.CompactHdf5Writer(output, compression_level=1)
    writer.register_topic(
        "/center_camera/image_raw/compressed",
        "sensor_msgs/msg/CompressedImage",
        serialization_format="cdr",
        offered_qos_profiles="qos",
        camera_key="center_camera",
    )
    writer.register_topic(
        "/joint_states",
        "sensor_msgs/msg/JointState",
        serialization_format="cdr",
        offered_qos_profiles="qos",
    )
    writer.set_rosbag_metadata("metadata")
    writer.append_video("/center_camera/image_raw/compressed", 101, 0)
    writer.append_exact("/joint_states", 150, b"exact-joint-cdr")
    writer.close(
        source_digest="source",
        non_image_digest="non-image",
        source_image_digest="jpeg",
        video_metadata={
            "center_camera": {
                "codec": "h264",
                "width": 640,
                "height": 480,
                "fps": 30,
                "frame_count": 1,
                "crf": 14,
                "preset": "medium",
                "pixel_format": "yuv420p",
                "sha256": "video",
                "bytes": 123,
            }
        },
    )

    with h5py.File(output, "r") as handle:
        assert handle.attrs["format_version"] == 3
        assert handle.attrs["storage_profile"] == "compact-video"
        assert bool(handle.attrs["complete"]) is True
        assert bytes(handle["cdr/data"][:]) == b"exact-joint-cdr"
        timeline = handle["timeline"]
        assert timeline["topic_ids"][:].tolist() == [0, 1]
        assert timeline["timestamps_ns"][:].tolist() == [101, 150]
        assert timeline["storage_kinds"][:].tolist() == [1, 0]
        assert timeline["cdr_lengths"][:].tolist() == [0, 15]
        assert timeline["video_frame_indices"][:].tolist() == [
            0,
            int(module.NO_FRAME_INDEX),
        ]
        camera = handle["videos/center_camera"]
        assert camera["timestamps_ns"][:].tolist() == [101]
        assert camera["frame_indices"][:].tolist() == [0]
        assert camera.attrs["relative_path"] == "center_camera.mp4"
        assert camera.attrs["crf"] == 14


def test_compact_writer_adds_direct_numeric_signal_tables(tmp_path):
    module = importlib.import_module("galaxis_core.hdf5_recorder")
    output = tmp_path / "round.h5.partial"
    writer = module.CompactHdf5Writer(output, compression_level=1)
    writer.register_topic(
        "/galaxis/gripper/left/encoder_offset",
        "std_msgs/msg/Float64",
        serialization_format="cdr",
        offered_qos_profiles="qos",
    )
    writer.set_rosbag_metadata("metadata")
    writer.append_exact(
        "/galaxis/gripper/left/encoder_offset",
        123,
        b"\x00\x01\x00\x00" + struct.pack("<d", 1.25),
    )
    writer.close(
        source_digest="source",
        non_image_digest="non-image",
        source_image_digest="images",
        video_metadata={},
    )

    with h5py.File(output, "r") as handle:
        signal = handle["signals/0000"]
        assert signal.attrs["key"] == "galaxis.gripper.left.encoder_offset"
        assert signal["timestamps_ns"][:].tolist() == [123]
        assert signal["values"][:].tolist() == [1.25]
