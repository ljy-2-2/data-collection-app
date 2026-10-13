"""Offline integration: real ROS CDR + MCAP + three real H.264 videos.

Run in the existing galaxis-core image with --network none and no devices.
No ROS node, hardware collection, platform upload, or user data is touched.
"""

import json
import shutil
import subprocess

import pytest


def test_real_ros_bag_to_selected_bundle(tmp_path):
    rosbag = pytest.importorskip("rosbag2_py")
    h5py = pytest.importorskip("h5py")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("requires ffmpeg and ffprobe")
    from rclpy.serialization import serialize_message, deserialize_message
    from galaxis_pico_msgs.msg import PicoController, PicoHmd
    from geometry_msgs.msg import PoseStamped
    from sensor_msgs.msg import JointState, Joy, CompressedImage
    from std_msgs.msg import Float64
    from galaxis_core import hdf5_recorder as recorder

    jpeg = subprocess.check_output([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32",
        "-frames:v", "1", "-c:v", "mjpeg", "-threads", "1", "-f", "image2pipe", "pipe:1",
    ])
    bag = tmp_path / "source-bag"
    writer = rosbag.SequentialWriter()
    writer.open(rosbag.StorageOptions(uri=str(bag), storage_id="mcap"),
                rosbag.ConverterOptions("cdr", "cdr"))
    topics = dict(recorder.UMI_POSE_TOPICS)
    topics.update({topic: "sensor_msgs/msg/JointState" for topic in recorder.UMI_GRIPPER_TOPICS})
    topics.update({topic: "std_msgs/msg/Float64" for topic in recorder.UMI_OFFSET_TOPICS})
    topics.update({f"/pico/controller/{side}/joy": "sensor_msgs/msg/Joy" for side in ("left", "right")})
    topics["/joint_states"] = "sensor_msgs/msg/JointState"
    for camera in ("left", "right", "center"):
        topics[f"/{camera}_camera/image_raw/compressed"] = recorder.COMPRESSED_IMAGE_TYPE
    for index, (name, kind) in enumerate(topics.items()):
        writer.create_topic(rosbag.TopicMetadata(id=index, name=name, type=kind, serialization_format="cdr"))
    for topic in recorder.UMI_OFFSET_TOPICS:
        writer.write(topic, serialize_message(Float64(data=1.25)), 900_000_000)
    for frame in range(3):
        stamp = 1_000_000_000 + frame * 33_333_333
        for topic, kind in topics.items():
            if topic in recorder.UMI_OFFSET_TOPICS:
                continue
            if topic in recorder.UMI_POSE_TOPICS:
                msg = PicoHmd() if "/hmd/" in topic else PicoController()
                msg.header.frame_id = "pico_origin"
                msg.header.stamp.sec = 1
                msg.header.stamp.nanosec = frame * 33_333_333
                msg.device_time_ns = stamp
                msg.pc_monotonic_ns = stamp + 100
                msg.seq = frame
                msg.pico_id = "offline-integration-test"
                msg.position.x = float(frame)
                msg.rotation.w = 1.0
                if isinstance(msg, PicoController):
                    msg.tracked = True
                    msg.valid = True
            elif kind == "sensor_msgs/msg/JointState":
                msg = JointState()
                names = ([recorder.UMI_GRIPPER_TOPICS[topic]] if topic in recorder.UMI_GRIPPER_TOPICS
                         else list(recorder.UMI_GRIPPER_TOPICS.values()))
                msg.name = names
                msg.position = [0.25 + frame] * len(names)
            elif kind == "sensor_msgs/msg/Joy":
                msg = Joy()
                msg.axes = [0.0, 1.0, 0.5, 0.5]
                msg.buttons = [1] * 11
            else:
                msg = CompressedImage()
                msg.format = "jpeg"
                msg.data = jpeg
            writer.write(topic, serialize_message(msg), stamp)
    writer.close()

    episode_dir = tmp_path / "hdf5" / "test-task" / "test-round"
    episode_dir.mkdir(parents=True)
    (episode_dir / "meta.json").write_text(json.dumps({
        "task_name": "offline-profile-test", "episode": {"start_time": 1, "end_time": 1.066666666},
    }))
    output = episode_dir / "test-round.h5"
    result = recorder.convert_rosbag_compact(bag, output, signal_profile=recorder.UMI_SIGNAL_PROFILE)
    assert result["camera_frame_counts"] == {f"{camera}_camera": 3 for camera in ("left", "right", "center")}
    with h5py.File(output, "r") as handle:
        assert len(handle["signals"]) == 23
        topics_by_id = {int(g.attrs["id"]): (str(g.attrs["name"]), str(g.attrs["type"]))
                        for g in handle["topics"].values()}
        timeline = handle["timeline"]
        cdr = bytes(handle["cdr/data"][:])
        pose_count = joint_count = 0
        for i, topic_id in enumerate(timeline["topic_ids"][:]):
            name, kind = topics_by_id[int(topic_id)]
            start = int(timeline["cdr_offsets"][i])
            size = int(timeline["cdr_lengths"][i])
            if kind == recorder.POSE_STAMPED_TYPE:
                pose = deserialize_message(cdr[start:start + size], PoseStamped)
                assert pose.header.frame_id == "pico_origin"
                assert pose.header.stamp.sec == 1
                assert pose.pose.position.x in (0, 1, 2)
                assert pose.pose.orientation.w == 1.0
                pose_count += 1
            elif kind == "sensor_msgs/msg/JointState":
                joint = deserialize_message(cdr[start:start + size], JointState)
                assert list(joint.name) == [recorder.UMI_GRIPPER_TOPICS[name]]
                assert len(joint.position) == 1
                assert len(joint.velocity) == len(joint.effort) == 0
                joint_count += 1
        assert pose_count == 9
        assert joint_count == 6
    manifest = json.loads((episode_dir / "manifest.json").read_text())
    assert manifest["signal_count"] == 23
    assert len(manifest["artifacts"]) == 5  # HDF5 + 3 videos + meta
    recorder.verify_existing_rosbag(bag, output)
