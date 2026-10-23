"""Unit tests for RecordManager's upload-notification behaviour on stop_recording.

All ROS2 dependencies are mocked via ``conftest.mock_rclpy``.
"""

import json
import os
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper: build a RecordManager without hitting __init__ (blocks on services)
# ---------------------------------------------------------------------------


@pytest.fixture()
def record_manager(mock_rclpy, tmp_record_dir):
    """Create a ``RecordManager`` instance bypassing ``__init__``."""
    from galaxis_core.record_manager import RecordManager

    # Bypass __init__ which blocks on service discovery
    node = RecordManager.__new__(RecordManager)

    # Manually wire up FakeNode behaviour
    from conftest import FakeNode

    fake = FakeNode("record_manager")
    node._name = fake._name
    node._params = fake._params
    node._logger = fake._logger

    # Satisfy declare_parameter / get_parameter via FakeNode methods
    node.declare_parameter = fake.declare_parameter
    node.get_parameter = fake.get_parameter
    node.get_logger = fake.get_logger
    node.create_publisher = fake.create_publisher
    node.create_timer = fake.create_timer
    node.create_service = fake.create_service
    node.create_client = fake.create_client
    node.destroy_node = fake.destroy_node

    # Instance attributes that __init__ would set
    node.rosbag_process = None
    node.is_recording = False
    node._recording_started_monotonic = None
    node._recording_elapsed_sec = 0.0
    node.recording_elapsed_pub = MagicMock()
    node._recording_start_lock = threading.Lock()
    node.recent_record_dir = None
    node.task_name = None
    node.reent_cg = MagicMock()

    node.record_base_dir = str(tmp_record_dir)
    node.robot_id = "test_robot"
    node.site = "test_lab"
    node.location = ""
    node.record_topics = ["/tf"]
    node.rosbag_params = ["--storage", "mcap"]
    node.record_format = "rosbag"
    node.qos_overrides_file = ""
    node.use_sim_time = False
    node._sim_time_verified = False
    node._hdf5_process_lock = threading.Lock()
    node._hdf5_process = None
    node._hdf5_shutdown = threading.Event()
    node.required_free_space = 0
    node.record_owner_uid = 1000
    node.record_owner_gid = 1000

    # Publishers
    node.rosbag_manager_mode_pub = MagicMock()
    node.storage_free_space_pub = MagicMock()
    node.storage_used_space_pub = MagicMock()
    node.recording_completed_pub = MagicMock()
    node.recording_saved_pub = MagicMock()

    # Service clients
    node.init_meta_client = MagicMock()
    node.add_file_client = MagicMock()
    node.extend_entities_client = MagicMock()
    node.extend_components_client = MagicMock()
    node.get_verified_metadata_client = MagicMock()

    return node


# ===================================================================
# TestStopRecordingPublish
# ===================================================================


class TestStopRecordingPublish:
    """Verify that ``stop_recording`` publishes to ``recording_completed``."""

    def test_mp4_failure_preserves_original_and_never_reports_saved(self, record_manager, tmp_record_dir):
        folder = tmp_record_dir / "mp4_failed"
        folder.mkdir()
        (folder / "source.mcap").write_bytes(b"source")
        record_manager.is_recording = True
        record_manager.video_mp4_enabled = True
        record_manager.recent_record_dir = str(folder)
        record_manager.task_name = "mp4_failed"
        record_manager.rosbag_process = MagicMock()
        record_manager.rosbag_process.wait.return_value = 0
        future = MagicMock()
        future.done.return_value = True
        future.result.return_value = MagicMock(success=True, message='{"task":"test"}')
        record_manager.get_verified_metadata_client.call_async.return_value = future
        with patch.object(record_manager, "_queue_mp4_recording", side_effect=RuntimeError("encoder failed")):
            success, message = record_manager.stop_recording()
        assert not success and "encoder failed" in message
        assert (folder / "source.mcap").read_bytes() == b"source"
        assert not (folder / "meta.json").exists()
        record_manager.recording_completed_pub.publish.assert_not_called()
        record_manager.recording_saved_pub.publish.assert_not_called()

    @pytest.mark.parametrize("exit_code,metadata_ok", [(1, True), (0, False)])
    def test_failed_save_preserves_bag_and_reason_without_upload_marker(
        self, record_manager, tmp_record_dir, exit_code, metadata_ok
    ):
        folder = tmp_record_dir / "failed_round"
        folder.mkdir()
        bag = folder / "record.mcap"
        bag.write_bytes(b"recoverable-data")
        record_manager.is_recording = True
        record_manager.recent_record_dir = str(folder)
        record_manager.rosbag_process = MagicMock()
        record_manager.rosbag_process.wait.return_value = exit_code
        future = MagicMock()
        future.done.return_value = True
        future.result.return_value = MagicMock(success=metadata_ok, message="episode.end_time missing")
        record_manager.get_verified_metadata_client.call_async.return_value = future
        success, message = record_manager.stop_recording()
        assert not success
        assert bag.read_bytes() == b"recoverable-data"
        failure = json.loads((folder / "recording-failure.json").read_text())
        assert failure["reason"]
        if not metadata_ok:
            assert "episode.end_time missing" in message
        assert not (folder / "meta.json").exists()
        record_manager.recording_completed_pub.publish.assert_not_called()
        record_manager.recording_saved_pub.publish.assert_not_called()

    def test_publishes_on_successful_stop(self, record_manager, tmp_record_dir):
        # Prepare: simulate an active recording
        rec_dir = tmp_record_dir / "task_001"
        rec_dir.mkdir()
        record_manager.is_recording = True
        record_manager.recent_record_dir = str(rec_dir)
        record_manager.task_name = "task_001"
        record_manager.rosbag_process = MagicMock()

        # Mock metadata verification to succeed
        future = MagicMock()
        result = MagicMock()
        result.success = True
        result.message = json.dumps({"task": "test"})
        future.result.return_value = result
        future.done.return_value = True
        record_manager.get_verified_metadata_client.call_async.return_value = future

        success, msg = record_manager.stop_recording()

        assert success is True
        record_manager.recording_saved_pub.publish.assert_called_once()
        saved_arg = record_manager.recording_saved_pub.publish.call_args[0][0]
        assert saved_arg.data == str(rec_dir)
        record_manager.recording_completed_pub.publish.assert_called_once()
        call_arg = record_manager.recording_completed_pub.publish.call_args[0][0]
        assert call_arg.data == str(rec_dir)

    def test_no_publish_on_metadata_failure(self, record_manager, tmp_record_dir):
        rec_dir = tmp_record_dir / "task_002"
        rec_dir.mkdir()
        record_manager.is_recording = True
        record_manager.recent_record_dir = str(rec_dir)
        record_manager.task_name = "task_002"
        record_manager.rosbag_process = MagicMock()

        # Mock metadata verification to fail
        future = MagicMock()
        result = MagicMock()
        result.success = False
        result.message = ""
        future.result.return_value = result
        future.done.return_value = True
        record_manager.get_verified_metadata_client.call_async.return_value = future

        # Patch delete_last_recording to avoid filesystem side effects
        with patch.object(record_manager, "delete_last_recording", return_value=True):
            success, msg = record_manager.stop_recording()

        assert success is False
        record_manager.recording_saved_pub.publish.assert_not_called()
        record_manager.recording_completed_pub.publish.assert_not_called()


class TestRecordingOwnership:
    def test_normalize_recording_owner_recurses_without_following_symlinks(
        self, record_manager, tmp_record_dir
    ):
        rec_dir = tmp_record_dir / "task_003"
        nested = rec_dir / "nested"
        nested.mkdir(parents=True)
        payload = nested / "record.mcap"
        payload.write_text("mcap")
        link = rec_dir / "outside-link"
        link.symlink_to(payload)

        with (
            patch("galaxis_core.ownership.os.chown") as chown,
            patch("galaxis_core.ownership.os.chmod") as chmod,
        ):
            assert record_manager.normalize_recording_owner(str(rec_dir)) is True

        changed = {os.fspath(call.args[0]) for call in chown.call_args_list}
        assert str(rec_dir) in changed
        assert str(nested) in changed
        assert str(payload) in changed
        assert str(link) not in changed
        chmod_calls = {
            (os.fspath(call.args[0]), call.args[1])
            for call in chmod.call_args_list
        }
        assert (str(rec_dir), 0o777) in chmod_calls
        assert (str(nested), 0o777) in chmod_calls
        assert (str(payload), 0o777) in chmod_calls
        assert not any(call[0] == str(link) for call in chmod_calls)

    def test_no_publish_when_not_recording(self, record_manager):
        record_manager.is_recording = False
        record_manager.rosbag_process = None

        success, msg = record_manager.stop_recording()

        assert success is False
        record_manager.recording_saved_pub.publish.assert_not_called()
        record_manager.recording_completed_pub.publish.assert_not_called()


class TestRecordingFormat:
    def test_rosbag_payload_and_command_remain_compatible(self, record_manager):
        payload_type, filename = record_manager._recording_payload("task/round_001")
        command = record_manager._recording_command("/data/round_001", filename)

        assert (payload_type, filename) == ("mcap", "round_001_0.mcap")
        assert command == [
            "ros2", "bag", "record", "-o", "/data/round_001",
            "--storage", "mcap", "/tf",
        ]

    def test_hdf5_payload_and_command(self, record_manager):
        record_manager.record_format = "hdf5"
        payload_type, filename = record_manager._recording_payload("task/round_001")
        command = record_manager._recording_command("/data/round_001", filename)

        assert (payload_type, filename) == ("hdf5", "round_001.h5")
        assert command == [
            "ros2", "bag", "record", "-o",
            "/data/round_001/.rosbag_staging",
            "--storage", "mcap", "/tf",
        ]

    def test_hdf5_stop_pauses_immediately_and_queues_finalization(
        self, record_manager, tmp_path
    ):
        record_manager.record_format = "hdf5"
        record_manager.is_recording = True
        record_manager.task_name = "Galaxis/Galaxis_001"
        record_manager.recent_record_dir = str(tmp_path / "Galaxis_001")
        (tmp_path / "Galaxis_001").mkdir()
        record_manager.rosbag_process = MagicMock()
        record_manager.rosbag_process.wait.return_value = 0
        future = MagicMock()
        result = MagicMock(success=True, message=json.dumps({"task": "Galaxis"}))
        future.done.return_value = True
        future.result.return_value = result
        record_manager.get_verified_metadata_client.call_async.return_value = future

        with patch.object(record_manager, "_queue_hdf5_finalization") as queue_job:
            success, message = record_manager.stop_recording()

        assert success is True
        assert "queued" in message
        assert record_manager.is_recording is False
        record_manager.rosbag_manager_mode_pub.publish.assert_called()
        record_manager.recording_saved_pub.publish.assert_called_once()
        saved_arg = record_manager.recording_saved_pub.publish.call_args[0][0]
        assert saved_arg.data == str(tmp_path / "Galaxis_001")
        assert json.loads(
            (tmp_path / "Galaxis_001" / "meta.json").read_text()
        ) == {"task": "Galaxis"}
        # Upload readiness remains tied to successful background conversion.
        record_manager.recording_completed_pub.publish.assert_not_called()
        queue_job.assert_called_once_with(
            str(tmp_path / "Galaxis_001"),
            "Galaxis/Galaxis_001",
            {"task": "Galaxis"},
        )

    def test_hdf5_finalize_keeps_staging_until_round_metadata_is_confirmed(
        self, record_manager, tmp_path
    ):
        record_manager.record_format = "hdf5"
        record_manager.task_name = "round_001"
        record_manager.recent_record_dir = str(tmp_path / "round_001")
        staging = tmp_path / "round_001" / ".rosbag_staging"
        staging.mkdir(parents=True)
        (staging / "data.mcap").write_bytes(b"mcap")

        process = MagicMock(returncode=0)

        def communicate():
            (tmp_path / "round_001" / "round_001.h5").write_bytes(b"hdf5")
            return '{"message_count": 1}', None

        process.communicate.side_effect = communicate

        with patch(
            "galaxis_core.record_manager.subprocess.Popen", return_value=process
        ) as popen:
            success, _message = record_manager._finalize_hdf5_recording(
                str(tmp_path / "round_001"), "round_001"
            )

        assert success is True
        assert staging.exists()
        command = popen.call_args.args[0]
        assert command[-10:] == [
            "--profile", "compact-video",
            "--signal-profile", "umi-pose-gripper-v1",
            "--video-crf", "14", "--video-fps", "30",
            "--video-preset", "medium",
        ]
        assert popen.call_args.kwargs["start_new_session"] is True

    def test_hdf5_complete_removes_staging_after_payload_and_metadata(
        self, record_manager, tmp_path
    ):
        record_manager.record_format = "hdf5"
        record_dir = tmp_path / "round_001"
        staging = record_dir / ".rosbag_staging"
        staging.mkdir(parents=True)
        (staging / "metadata.yaml").write_text("metadata")
        (record_dir / "round_001.h5").write_bytes(b"hdf5")

        with patch.object(
            record_manager,
            "_finalize_hdf5_recording",
            return_value=(True, "Success"),
        ):
            record_manager._complete_hdf5_recording(
                str(record_dir), "round_001", {"task": "Galaxis"}
            )

        assert not staging.exists()
        assert json.loads((record_dir / "meta.json").read_text()) == {
            "task": "Galaxis"
        }
        record_manager.recording_completed_pub.publish.assert_called_once()

    def test_startup_requeues_all_staging_rounds(self, record_manager, tmp_path):
        record_manager.record_format = "hdf5"
        record_manager.record_base_dir = str(tmp_path)
        rounds = []
        for name in ("round_001", "round_002"):
            record_dir = tmp_path / "Galaxis" / name
            staging = record_dir / ".rosbag_staging"
            staging.mkdir(parents=True)
            (staging / "metadata.yaml").write_text("metadata")
            (record_dir / "meta.json").write_text('{"task": "Galaxis"}')
            rounds.append(record_dir)

        with patch.object(record_manager, "_queue_hdf5_finalization") as queue:
            record_manager._recover_hdf5_finalizations()

        assert queue.call_count == 2
        assert {call.args[0] for call in queue.call_args_list} == {
            str(path) for path in rounds
        }
        assert all(call.args[2] == {"task": "Galaxis"} for call in queue.call_args_list)

    def test_startup_does_not_requeue_legacy_final_without_meta(
        self, record_manager, tmp_path
    ):
        record_manager.record_format = "hdf5"
        record_manager.record_base_dir = str(tmp_path)
        record_dir = tmp_path / "Galaxis" / "round_legacy"
        staging = record_dir / ".rosbag_staging"
        staging.mkdir(parents=True)
        (staging / "metadata.yaml").write_text("metadata")
        (record_dir / "round_legacy.h5").write_bytes(b"validated hdf5")

        with patch.object(record_manager, "_queue_hdf5_finalization") as queue:
            record_manager._recover_hdf5_finalizations()

        queue.assert_not_called()
        assert staging.exists()

    def test_hdf5_finalize_preserves_staging_on_failure(
        self, record_manager, tmp_path
    ):
        record_manager.record_format = "hdf5"
        record_manager.task_name = "round_002"
        record_manager.recent_record_dir = str(tmp_path / "round_002")
        staging = tmp_path / "round_002" / ".rosbag_staging"
        staging.mkdir(parents=True)
        (staging / "data.mcap").write_bytes(b"mcap")

        process = MagicMock(returncode=1)
        process.communicate.return_value = "checksum mismatch", None
        with patch(
            "galaxis_core.record_manager.subprocess.Popen", return_value=process
        ):
            success, _message = record_manager._finalize_hdf5_recording(
                str(tmp_path / "round_002"), "round_002"
            )

        assert success is False
        assert staging.exists()

    def test_shutdown_terminates_converter_instead_of_waiting_indefinitely(
        self, record_manager
    ):
        record_manager.is_recording = False
        record_manager._hdf5_shutdown = threading.Event()
        record_manager._hdf5_jobs_lock = threading.Lock()
        record_manager._hdf5_jobs = {MagicMock()}
        record_manager._hdf5_process_lock = threading.Lock()
        process = MagicMock()
        process.poll.return_value = None
        record_manager._hdf5_process = process
        record_manager._hdf5_executor = MagicMock()

        with patch("galaxis_core.record_manager.os.killpg") as killpg:
            record_manager.shutdown()

        assert record_manager._hdf5_shutdown.is_set()
        killpg.assert_called_once_with(process.pid, 15)
        process.terminate.assert_not_called()
        process.wait.assert_called_once_with(timeout=3.0)
        record_manager._hdf5_executor.shutdown.assert_called_once_with(
            wait=True, cancel_futures=True
        )


@pytest.mark.parametrize('value', [None, {}, {'available': False, 'checked_at': 0},
                                   {'available': True, 'checked_at': 0}])
def test_desktop_offline_gate_prevents_ros_recorder_start(record_manager, tmp_path, value):
    gate = tmp_path / 'platform.json'
    if value is not None:
        gate.write_text(json.dumps(value))
    record_manager.collection_gate_file = str(gate)
    with patch('galaxis_core.record_manager.subprocess.Popen') as launch:
        assert record_manager.start_recording('task/round') == (False, '')
    launch.assert_not_called()
    assert not record_manager.is_recording


def test_completed_metadata_atomically_carries_collection_owner(record_manager, tmp_path):
    record_manager.collection_author = 'operator-a'
    record_manager.collection_destination = {'kind': 'local', 'api_url': 'http://127.0.0.1:8000', 'web_url': 'http://127.0.0.1:5173'}
    record_manager.save_meta_file({'episode': {'start_time': 10}}, str(tmp_path))
    assert json.loads((tmp_path / 'meta.json').read_text())['author'] == 'operator-a'
    assert json.loads((tmp_path / 'meta.json').read_text())['upload_destination'] == record_manager.collection_destination
    assert not (tmp_path / 'meta.json.tmp').exists()


@pytest.mark.parametrize('compressed_raw', [False, True])
def test_mp4_stop_is_durable_queue_only_and_next_round_can_start(record_manager, tmp_record_dir, compressed_raw):
    folder = tmp_record_dir / 'task' / 'first'; folder.mkdir(parents=True)
    source = folder / 'source.mcap'; source.write_bytes(b'original camera and joint data')
    record_manager.is_recording = True
    record_manager.video_mp4_enabled = True
    record_manager.compressed_mcap_enabled = compressed_raw
    if compressed_raw:
        (folder / '.compressed-raw.json').write_text(json.dumps({
            'format': 'umi-mcap-h264-v1', 'state': 'complete', 'encoder': 'test', 'sha256': 'test-digest'}))
    record_manager.recent_record_dir = str(folder)
    record_manager.collection_author = 'first-author'
    record_manager.collection_destination = {'kind': 'local', 'api_url': 'http://127.0.0.1:8000', 'web_url': 'http://127.0.0.1:5173'}
    record_manager.rosbag_process = MagicMock()
    record_manager.rosbag_process.wait.return_value = 0
    future = MagicMock()
    future.done.return_value = True
    future.result.return_value = MagicMock(success=True, message='{"episode":{"id":"first"}}')
    record_manager.get_verified_metadata_client.call_async.return_value = future
    from galaxis_core.recording_priority import CapturePriority
    record_manager._capture_priority = CapturePriority(tmp_record_dir)
    record_manager._capture_priority.acquire()
    with patch('subprocess.Popen', side_effect=AssertionError('Stop must not start encoder')):
        success, _ = record_manager.stop_recording()
    assert success and not record_manager.is_recording
    if compressed_raw:
        assert not (folder / '.mp4-pending.json').exists()
        saved = json.loads((folder / 'meta.json').read_text())
        assert saved['recording_format'] == 'umi-mcap-h264-v1' and saved['raw_sha256'] == 'test-digest'
        assert source.exists()
        record_manager.recording_completed_pub.publish.assert_called_once()
        return
    pending = json.loads((folder / '.mp4-pending.json').read_text())
    assert pending['author'] == 'first-author' and pending['_video_crf'] == 18
    assert pending['upload_destination'] == record_manager.collection_destination
    assert (folder / 'meta.json').exists() and source.exists()
    record_manager.recording_saved_pub.publish.assert_called_once()
    record_manager.recording_completed_pub.publish.assert_not_called()
    assert record_manager._capture_priority.intent is None
    with patch.object(record_manager, '_start_recording', return_value=(True, 'next')) as start:
        assert record_manager.start_recording('second')[0]
    start.assert_called_once_with('second')
    assert json.loads((folder / '.mp4-pending.json').read_text()) == pending


def test_cancel_cannot_race_stop_or_new_capture(record_manager):
    record_manager._recording_start_lock.acquire()
    try:
        assert record_manager.cancel_recording()[0] is False
        assert record_manager.stop_recording()[0] is False
        assert record_manager.start_recording('second')[0] is False
    finally:
        record_manager._recording_start_lock.release()


def test_recording_elapsed_freezes_after_stop_and_resets_for_next_round(record_manager):
    node = record_manager
    node.is_recording = True
    node._recording_started_monotonic = 10.0
    with patch('galaxis_core.record_manager.time.monotonic', return_value=12.75):
        node.publish_recording_state()
    assert node.recording_elapsed_pub.publish.call_args.args[0].data == 2.75
    node.is_recording = False
    with patch('galaxis_core.record_manager.time.monotonic', return_value=20.0):
        node.publish_recording_state()
    assert node.recording_elapsed_pub.publish.call_args.args[0].data == 10.0
    with patch('galaxis_core.record_manager.time.monotonic', return_value=200.0):
        node.publish_recording_state()
    assert node.recording_elapsed_pub.publish.call_args.args[0].data == 10.0
    node.is_recording = True
    node._recording_started_monotonic = 200.0
    node._recording_elapsed_sec = 0.0
    with patch('galaxis_core.record_manager.time.monotonic', return_value=201.25):
        node.publish_recording_state()
    assert node.recording_elapsed_pub.publish.call_args.args[0].data == 1.25


def interrupted_manager(node, tmp_path):
    from galaxis_core.capture_recovery import begin_capture
    folder = tmp_path/'task'/'interrupted'
    folder.mkdir(parents=True)
    node._capture_journal = begin_capture(tmp_path, folder, {'author':'original'}, ['/camera'])
    node.is_recording = True
    node.recent_record_dir = str(folder)
    node.rosbag_process = MagicMock()
    node.rosbag_process.poll.return_value = None
    node.rosbag_process.wait.return_value = 0
    node.recording_cancelled_pub = MagicMock()
    return folder, node._capture_journal


def test_interrupted_stop_queues_recovery_instead_of_deleting(record_manager, tmp_path):
    folder, journal = interrupted_manager(record_manager, tmp_path)
    (folder/'source.mcap').write_bytes(b'original')
    process=record_manager.rosbag_process
    ok, _ = record_manager.interrupt_recording('device_lost')
    assert ok and not record_manager.is_recording
    process.send_signal.assert_called_once()
    assert json.loads(journal.read_text())['state']=='interrupted'
    assert (folder/'source.mcap').read_bytes()==b'original'
    record_manager.recording_saved_pub.publish.assert_called_once()
    record_manager.recording_cancelled_pub.publish.assert_not_called()


def test_explicit_discard_is_durable_before_writer_is_stopped(record_manager, tmp_path):
    folder, journal = interrupted_manager(record_manager, tmp_path)
    def check(_):
        assert json.loads(journal.read_text())['state']=='discarded'
        assert (folder/'.discarded').exists()
    record_manager.rosbag_process.send_signal.side_effect=check
    ok,_=record_manager.cancel_recording()
    assert ok
    assert json.loads(journal.read_text())['state']=='discarded'
    record_manager.recording_saved_pub.publish.assert_not_called()


def test_failed_metadata_on_exit_preserves_durable_capture(record_manager, tmp_path):
    folder,journal=interrupted_manager(record_manager,tmp_path)
    future=MagicMock();future.done.return_value=True
    future.result.return_value=MagicMock(success=False,message='no end_time')
    record_manager.get_verified_metadata_client.call_async.return_value=future
    assert record_manager.stop_recording()[0]
    assert json.loads(journal.read_text())['state']=='interrupted'
    assert not (folder/'recording-failure.json').exists()
