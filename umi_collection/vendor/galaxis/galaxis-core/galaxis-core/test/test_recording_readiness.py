"""Use real MCAP bytes, including partial writes and compressed chunks."""
from io import BytesIO
from unittest.mock import MagicMock, patch
import signal
import subprocess

import pytest
from mcap.writer import CompressionType, Writer

from galaxis_core.recording_readiness import (
    McapReadiness, required_recording_topics, wait_for_recording_ready,
)
from test_record_manager_upload_notify import record_manager  # noqa: F401


TOPICS = ["/left_camera/image_raw/compressed", "/pico/hmd/state",
          "/pico/controller/left/state", "/galaxis/gripper/left/joint_state"]


def recording(topics, compression=CompressionType.NONE, chunked=True, messages=True):
    stream = BytesIO()
    writer = Writer(stream, compression=compression, chunk_size=64, use_chunking=chunked)
    writer.start()
    for i, topic in enumerate(topics):
        channel = writer.register_channel(topic, "cdr", 0)
        if messages:
            writer.add_message(channel, 1000 + i, b"\x00" * 40, 1000 + i)
    writer.finish()
    return stream.getvalue()


@pytest.mark.parametrize("compression", list(CompressionType))
@pytest.mark.parametrize("chunked", [True, False])
def test_waits_for_messages_in_every_enabled_channel(tmp_path, compression, chunked):
    probe = McapReadiness(tmp_path, TOPICS, TOPICS)
    payload = recording(TOPICS, compression, chunked)
    path = tmp_path / "round.mcap"
    # Exercise every partial header, record and compressed chunk boundary.
    for size in range(1, len(payload), 17):
        path.write_bytes(payload[:size])
        probe.scan()
    path.write_bytes(payload)
    probe.scan()
    assert probe.ready
    assert set(TOPICS) <= probe.seen


def test_file_and_channel_declarations_are_not_ready(tmp_path):
    probe = McapReadiness(tmp_path, TOPICS, TOPICS)
    (tmp_path / "round.mcap").write_bytes(recording(TOPICS, messages=False))
    probe.scan()
    assert not probe.ready
    assert probe.missing == sorted(TOPICS)


def test_missing_topic_and_split_files_with_reused_channel_ids(tmp_path):
    probe = McapReadiness(tmp_path, TOPICS, TOPICS)
    (tmp_path / "round_0.mcap").write_bytes(recording(TOPICS[:-1]))
    probe.scan()
    assert not probe.ready
    assert probe.missing == TOPICS[-1:]
    (tmp_path / "round_1.mcap").write_bytes(recording(TOPICS[-1:]))
    probe.scan()
    assert probe.ready


def test_profile_only_requires_enabled_continuous_streams():
    assert required_recording_topics(TOPICS + ["/tf_static", "/device_ids", "/rosout"]) == set(TOPICS)
    assert "/pico/controller/right/state" not in required_recording_topics(TOPICS)


def test_no_umi_profile_still_requires_an_actual_requested_message(tmp_path):
    probe = McapReadiness(tmp_path, set(), ["/tf"])
    (tmp_path / "round.mcap").write_bytes(recording(["/rosout"]))
    probe.scan()
    assert not probe.ready
    (tmp_path / "next.mcap").write_bytes(recording(["/tf"]))
    probe.scan()
    assert probe.ready


def test_wait_does_not_treat_a_running_process_as_ready(tmp_path):
    probe = McapReadiness(tmp_path, TOPICS, TOPICS)
    process = MagicMock()
    process.poll.return_value = None
    now = [0.0]
    def tick(seconds):
        now[0] += seconds
    with pytest.raises(RuntimeError, match="未录到必要通道.*left_camera"):
        wait_for_recording_ready(process, probe, timeout=.1, clock=lambda: now[0], sleep=tick)
    assert now[0] == pytest.approx(.1)


def test_wait_accepts_only_after_data_is_written(tmp_path):
    probe = McapReadiness(tmp_path, TOPICS, TOPICS)
    process = MagicMock()
    process.poll.return_value = None
    def write_data(_):
        (tmp_path / "round.mcap").write_bytes(recording(TOPICS))
    wait_for_recording_ready(process, probe, sleep=write_data)
    assert probe.ready


def test_child_exit_wins_even_with_complete_data(tmp_path):
    probe = McapReadiness(tmp_path, TOPICS, TOPICS)
    (tmp_path / "round.mcap").write_bytes(recording(TOPICS))
    process = MagicMock()
    process.poll.return_value = 1
    with pytest.raises(RuntimeError, match="退出码 1"):
        wait_for_recording_ready(process, probe)


def test_shutdown_interrupts_wait(tmp_path):
    with pytest.raises(RuntimeError, match="关闭流程"):
        wait_for_recording_ready(MagicMock(), McapReadiness(tmp_path, TOPICS, TOPICS), keep_running=lambda: False)


def prepare_start(node):
    for name in ("init_meta_client", "add_file_client", "set_robot_client",
                 "set_environment_client", "extend_programs_client", "set_devices_client"):
        client = MagicMock()
        client.call_async.return_value.done.return_value = True
        client.call_async.return_value.result.return_value.success = True
        setattr(node, name, client)
    for key in ("robot_type", "environment_type", "teleop_interface", "teleop_interface_url",
                "teleop_interface_hash", "teleop_interface_branch"):
        node.declare_parameter(key, "test")
    node.declare_parameter("devices", "[]")
    node.record_url = node.record_hash = node.record_branch = "test"
    node.record_format = "hdf5"
    node.record_topics = TOPICS
    process = MagicMock()
    process.poll.return_value = None
    return process


def test_manager_publishes_success_only_after_probe(record_manager):
    process = prepare_start(record_manager)
    def ready(*args, **kwargs):
        assert not record_manager.is_recording
        record_manager.rosbag_manager_mode_pub.publish.assert_not_called()
        assert record_manager.recent_record_dir is None
    with patch("galaxis_core.record_manager.subprocess.Popen", return_value=process), patch(
        "galaxis_core.record_manager.wait_for_recording_ready", side_effect=ready
    ):
        ok, _ = record_manager.start_recording("round")
    assert ok
    assert record_manager.is_recording
    assert record_manager.recent_record_dir.endswith("round")
    assert record_manager.rosbag_manager_mode_pub.publish.call_args.args[0].data is True


def test_failed_start_does_not_publish_saved_or_replace_previous_round(record_manager):
    process = prepare_start(record_manager)
    record_manager.recent_record_dir = "/previous/saved/round"
    process.wait.side_effect = lambda **kw: setattr(process.poll, "return_value", 0)
    with patch("galaxis_core.record_manager.subprocess.Popen", return_value=process), patch(
        "galaxis_core.record_manager.wait_for_recording_ready", side_effect=RuntimeError("缺少左相机")
    ):
        ok, message = record_manager.start_recording("failed-round")
    assert not ok
    assert message == "缺少左相机"
    assert not record_manager.is_recording
    assert record_manager.rosbag_process is None
    assert record_manager.recent_record_dir == "/previous/saved/round"
    record_manager.recording_saved_pub.publish.assert_not_called()
    record_manager.recording_completed_pub.publish.assert_not_called()
    process.send_signal.assert_called_once_with(signal.SIGINT)


def test_abort_escalates_and_reaps_child(record_manager):
    process = MagicMock()
    process.poll.side_effect = [None, None, None, -9]
    process.wait.side_effect = [subprocess.TimeoutExpired("recorder", 2),
                                subprocess.TimeoutExpired("recorder", 1), -9]
    record_manager.rosbag_process = process
    record_manager._abort_recording_start()
    assert record_manager.rosbag_process is None
    assert [c.args[0] for c in process.send_signal.call_args_list] == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]


def test_duplicate_start_is_rejected_while_starting(record_manager):
    record_manager._recording_start_lock.acquire()
    try:
        ok, message = record_manager.start_recording("duplicate")
    finally:
        record_manager._recording_start_lock.release()
    assert not ok
    assert "正在启动" in message
