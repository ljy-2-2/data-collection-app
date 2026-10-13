"""Offline A/B -> real JPEG Episode -> mocked platform contract, no hardware."""

import io
import json
import math
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleop_control/src"))
from teleop_control import collection_uploader, tianji_collection
from teleop_control.collection_uploader import (
    CollectionUploader,
    READY_MARKER,
    UPLOADED_MARKER,
)
from teleop_control.tianji_collection import TianjiCollection, CAMERA_MAPPING
from FusionDataCollector.collector import readiness
from fusion_camera import StreamEvent


def feedback(sequence, selected=("left", "right"), a=0, b=0):
    stamp = time.monotonic_ns()
    return {
        "mode": "live",
        "teleop_state": "running",
        "session": "sdk-test",
        "vr_session": 1,
        "sequence": sequence,
        "pc_monotonic_ns": stamp,
        "pc_unix_ns": time.time_ns(),
        "vr_connected": True,
        "buttons": {"A": False, "B": False},
        "button_press_counts": {"A": a, "B": b},
        "joints_deg": {s: [0] * 7 for s in ("left", "right")},
        "joint_feedback": {
            s: {"frame": sequence, "unchanged_s": 0} for s in ("left", "right")
        },
        "joint_command": {
            "sequence": sequence,
            "pc_monotonic_ns": stamp,
            "joints_deg": {s: [10] * 7 for s in selected},
        },
        "grippers": {
            "fault": "",
            "motors": {
                s: {
                    "state": "ACTIVE",
                    "target_rad": 0.8,
                    "display_endpoints_rad": {"open_rad": 1.0, "closed_rad": 0.0},
                    "feedback": {
                        "state": 1,
                        "sequence": sequence,
                        "received": stamp / 1e9,
                        "position": 0.5,
                    },
                }
                for s in selected
            },
        },
    }


@pytest.fixture
def account(monkeypatch):
    monkeypatch.setattr(
        tianji_collection, "current_platform_username", lambda: "collector-test"
    )
    monkeypatch.setattr(
        collection_uploader, "current_platform_username", lambda: "collector-test"
    )


def platform_client(seen, fail_upload=False):
    def handler(request):
        seen.append(request)
        if request.url.host == "storage.test":
            assert "Authorization" not in request.headers
            assert int(request.headers["Content-Length"]) == len(request.content)
            return httpx.Response(503 if fail_upload else 200)
        assert "Authorization" in request.headers
        if request.url.path.startswith("/episodes/import-batches/"):
            return httpx.Response(200, json={})
        if request.url.path == "/uploads/presign":
            data = json.loads(request.content)
            name = data["filenames"][0]
            return httpx.Response(
                200,
                json={
                    "urls": [
                        {
                            "filename": name,
                            "put_url": "https://storage.test/" + name,
                            "key": "raw/" + data["episode_id"] + "/" + name,
                        }
                    ]
                },
            )
        if request.url.path == "/episodes":
            return httpx.Response(201, json={"id": json.loads(request.content)["id"]})
        raise AssertionError(str(request.url))

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        auth=httpx.BasicAuth("test", "synthetic"),
    )


@pytest.mark.parametrize(
    "mode,raw_format,upload_mode", [
        ("dual", "mjpeg", None), ("left", "mjpeg", None), ("right", "yuyv", None),
        ("dual", "mjpeg", "left"), ("dual", "mjpeg", "right"),
    ]
)
def test_ab_episode_and_platform_round_trip(tmp_path, account, mode, raw_format, upload_mode):
    selected = ("left", "right") if mode == "dual" else (mode,)
    exported = selected if upload_mode is None else (upload_mode,)
    buf = io.BytesIO()
    Image.new("RGB", (1024, 768), (40, 90, 130)).save(buf, format="JPEG")
    pixels = (
        buf.getvalue()
        if raw_format == "mjpeg"
        else bytes([90, 128, 90, 128]) * (640 * 480 // 2)
    )
    manager = SimpleNamespace(
        pause_cameras=Mock(), start_cameras=Mock(), collection_frame=Mock(),
        resume_cameras_after_episode=Mock()
    )
    collection = TianjiCollection(manager, tmp_path)
    offset = 3_000_000_000

    class Camera:
        stopped = False

        def validate_stream(self):
            pass

        def status(self):
            return {
                "idle": True,
                "observation_modes": ["teleop_observation"],
                "graceful_stop": True,
            }

        def list_cameras(self):
            return [{"id": key, "available": True} for key in CAMERA_MAPPING]

        def clock_sync(self, samples):
            return {
                "boot_id": "synthetic",
                "pc_midpoint_ns": time.monotonic_ns(),
                "offset_ns": offset,
                "uncertainty_ns": 100,
            }

        def request_stop(self):
            self.stopped = True

        def close(self):
            pass

        def stream(self, **options):
            manager.pause_cameras.assert_not_called()
            assert options["mode"] == "teleop_observation"
            size = [1024, 768] if raw_format == "mjpeg" else [640, 480]
            yield StreamEvent(
                {
                    "type": "start",
                    "boot_id": "synthetic",
                    "format": raw_format,
                    "native_size": size,
                    "observation_mode": "teleop_observation",
                    "graceful_stop": True,
                    "adopted_preview_stream": True,
                    "episode_count_baseline": {
                        "frames": dict(zip(CAMERA_MAPPING, [601, 590, 583])),
                        "joint_samples": 2002,
                        "source": "received_before_subscription",
                    },
                }
            )
            counts, samples = [0, 0, 0], 0
            for i in range(75):
                row = feedback(i + 3, selected, a=1, b=int(i >= 65))
                collection.accept(row)
                stamp = row["pc_monotonic_ns"] + offset
                yield StreamEvent(
                    {
                        "type": "joints",
                        "timestamp_ns": stamp,
                        "position_deg": [0.0] * 7 + [30.0] * 7,
                        "velocity_deg_s": [0.0] * 14,
                        "torque_nm": [1.0] * 14,
                        "joint_names": [
                            s + "_joint_" + str(j)
                            for s in ("left", "right")
                            for j in range(1, 8)
                        ],
                        "sequence": i,
                        "tick": i,
                        "frame_serial": [i, i],
                        "pc_recv_ns": time.time_ns(),
                    }
                )
                samples += 1
                if self.stopped:
                    break
                if i % 3 == 0:
                    for k, camera in enumerate(CAMERA_MAPPING):
                        yield StreamEvent(
                            {
                                "type": "image",
                                "camera_id": camera,
                                "format": raw_format,
                                "width": size[0],
                                "height": size[1],
                                "timestamp_ns": stamp + k * 100_000,
                                "sequence": counts[k],
                            },
                            pixels,
                        )
                        counts[k] += 1
                time.sleep(0.008)
            yield StreamEvent(
                {"type": "end", "ok": True,
                 "frames": [n+b for n, b in zip(counts, [601, 590, 583])],
                 "joint_samples": samples+2002}
            )

    manager.adopt_camera_stream = Camera
    resume_before_finalizing = []
    manager.resume_cameras_after_episode.side_effect = lambda: resume_before_finalizing.append(
        not (collection.episode.directory / 'record/aligned_joints.jsonl').exists()
    )
    collection.arm({"task": "SYNTHETIC TEST", "mode": mode, "upload_mode": upload_mode, "parameters": {"scale": 1}})
    assert collection.controlled_sides == selected
    collection.accept(feedback(1, selected))
    collection.accept(feedback(2, selected, a=1))
    collection.job.join(timeout=15)
    assert not collection.active()
    assert collection.snapshot()["completed"] == 1, collection.error
    assert collection.armed  # B ends the Episode without stopping teleoperation.
    directory = collection.episode.directory
    metadata = json.loads((directory / "meta_info.json").read_text())
    assert metadata["complete"] and metadata["actions_recorded"]
    assert metadata["gripper_action_policy"] == "initial_feedback_then_last_target"
    comparison = metadata['integrity']['count_comparison']
    assert comparison['scope'] == 'episode_after_subscription'
    assert comparison['matches']
    assert comparison['excluded_preview']['joint_samples'] == 2002
    assert metadata["robot_type"] == "tianji"
    assert metadata["controlled_sides"] == list(selected)
    assert metadata["upload_sides"] == list(exported)
    assert metadata["camera_mapping"] == CAMERA_MAPPING
    assert metadata["stop_reason"] == "B/Y"
    assert not list(directory.rglob("native"))
    assert set(p.name for p in (directory / "camera").iterdir()) == set(
        CAMERA_MAPPING.values()
    )
    for path in (directory / "camera").glob("*/frames/*.jpg"):
        with Image.open(path) as image:
            assert image.size == (640, 480)
            image.load()
    bundles = [
        json.loads(row)
        for row in (directory / "record/bundles.jsonl").read_text().splitlines()
    ]
    assert all(len(row["grippers"]) == len(selected) for row in bundles)
    assert all(row['tensors']['action__'+side+'_effector__position'] == [0.8]
               and row['gripper_target_sources'][side]['source'] == 'last_controller_target'
               for row in bundles for side in selected)
    action = next(row for row in bundles if "action__joint__position" in row["tensors"])
    assert action["tensors"]["action__joint__position"][0] == pytest.approx(
        math.radians(10)
    )
    assert action["tensors"]["state__joint__position"][0] == 0
    assert (directory / READY_MARKER).is_file()
    manager.collection_frame.assert_not_called()
    assert manager.resume_cameras_after_episode.called
    assert resume_before_finalizing[0] is True
    seen = []
    original_files = {p.relative_to(directory).as_posix(): p.read_bytes()
                      for p in directory.rglob('*') if p.is_file()}
    with platform_client(seen) as client:
        uploader = CollectionUploader(tmp_path, "https://platform.test", client=client)
        uploader.scan_once()
        assert uploader.status()["uploaded"] == 1
        count = len(seen)
        uploader.scan_once()
        assert len(seen) == count  # Receipt makes repeat scans idempotent.
    registered = json.loads(next(r for r in seen if r.url.path == "/episodes").content)
    assert registered["robot_id"] == "tianji"
    assert registered["extra"]["fps"] == 30
    assert registered['extra']['controlled_sides'] == list(selected)
    assert registered['extra']['upload_sides'] == list(exported)
    features = registered['extra']['source_features']
    expected_names = [s + '_joint_' + str(j) for s in exported for j in range(1, 8)]
    assert features['observation.state']['names'] == expected_names
    assert features['observation.state']['shape'] == [7 * len(exported)]
    assert features['action']['names'] == expected_names
    assert set(registered['extra']['gripper_features']) == set(exported)
    assert all(not feature['missing_command_is_null']
               and feature['target_policy'] == 'initial_feedback_then_last_target'
               for feature in registered['extra']['gripper_features'].values())
    assert registered["extra"]["timing"]["bundle_timestamp_field"] == "timestamp_ns"
    assert registered["author"] == "collector-test"
    assert registered["extra"]["missing_extra_cameras"] == []
    assert {c["key"] for c in registered["extra"]["cameras"]} == {
        "observation.images." + n for n in CAMERA_MAPPING.values()
    }
    archive_request = next(r for r in seen if r.url.host == "storage.test")
    with tarfile.open(fileobj=io.BytesIO(archive_request.content)) as archive:
        assert not any("/native/" in n for n in archive.getnames())
        assert any(n.endswith("/record/actions.jsonl") for n in archive.getnames())
        def archived_rows(relative):
            return [json.loads(line) for line in archive.extractfile(directory.name + '/' + relative)]
        uploaded_bundles = archived_rows('record/bundles.jsonl')
        assert len(uploaded_bundles) == len(bundles)
        for original, uploaded in zip(bundles, uploaded_bundles):
            for key in ('timestamp_ns', 'pc_recv_ns', 'camera_frame_index', 'cam_ts'):
                assert uploaded[key] == original[key]
            tensors = uploaded['tensors']
            assert tensors['state__joint__name'] == expected_names
            assert tensors['action__joint__name'] == expected_names
            assert len(tensors['state__joint__position']) == 7 * len(exported)
            expected_positions = [math.radians(0 if s == 'left' else 30) for s in exported for _ in range(7)]
            assert tensors['state__joint__position'] == pytest.approx(expected_positions)
            assert set(uploaded['grippers']) == set(exported)
            for side in set(('left', 'right')) - set(exported):
                assert not any('__' + side + '_' in key for key in tensors)
                assert directory.name + '/record/' + side + '_gripper.jsonl' not in archive.getnames()
        for relative in ('record/joints.jsonl', 'record/actions.jsonl'):
            assert all(row['joint_names'] == expected_names for row in archived_rows(relative))
        assert all(len(row['position_rad']) == 8 * len(exported)
                   for row in archived_rows('record/aligned_joints.jsonl'))
        assert all(set(row['joints_deg']) == set(exported)
                   for row in archived_rows('record/teleop_feedback.jsonl'))
        for relative, original in original_files.items():
            assert (directory / relative).read_bytes() == original
            if relative.startswith('camera/') or len(exported) == 2 and relative != READY_MARKER:
                assert archive.extractfile(directory.name + '/' + relative).read() == original
    assert (directory / UPLOADED_MARKER).is_file()
    collection.close()


def test_single_arm_readiness_does_not_fabricate_other_gripper():
    row = feedback(1, ("left",))
    assert readiness(row, required_sides=("left",)) is None
    assert readiness(row, required_sides=("left", "right")) is not None


def test_fault_evidence_survives_later_feedback_and_control_stop(tmp_path):
    collection = TianjiCollection(SimpleNamespace(), tmp_path)
    row = feedback(1)
    row["grippers"]["fault"] = "left: motor feedback timeout"
    row["grippers"]["control_timing"] = {"max_cycle_ms": 170}
    collection.accept(row)
    saved = collection.snapshot()["last_gripper_fault"]
    row["grippers"]["control_timing"]["max_cycle_ms"] = 999
    collection.accept(row)
    collection.accept({"teleop_state": "idle", "grippers": None})
    assert collection.snapshot()["last_gripper_fault"] == saved
    assert saved["grippers"]["control_timing"]["max_cycle_ms"] == 170
    saved["grippers"]["fault"] = "mutated"
    assert collection.snapshot()["last_gripper_fault"]["grippers"]["fault"] == "left: motor feedback timeout"


@pytest.mark.parametrize("fault", [
    "left: motor feedback timeout",
    "right: motor fault 0x8",
    "停止请求或机械臂故障；重启前排除故障",
])
def test_gripper_fault_blocks_recording_and_preserves_cause(tmp_path, fault):
    collection = TianjiCollection(SimpleNamespace(), tmp_path)
    collection.armed = True
    collection.latest = feedback(1)
    collection.latest["grippers"]["fault"] = fault
    with pytest.raises(ValueError) as error:
        collection.start()
    assert str(error.value) == "夹爪反馈报告故障：" + fault
    assert collection.job is None
    assert collection.episode is None


def test_camera_failure_never_marks_ready_or_stops_teleop(tmp_path, account):
    manager = SimpleNamespace(
        pause_cameras=Mock(side_effect=RuntimeError("capture_paused")),
        start_cameras=Mock(),
        collection_frame=Mock(),
        resume_cameras_after_episode=Mock(),
        new_camera_client=Mock(),
        adopt_camera_stream=Mock(side_effect=RuntimeError("capture_paused")),
    )
    collection = TianjiCollection(manager, tmp_path)
    collection.arm({"task": "test", "mode": "dual"})
    collection.accept(feedback(1))
    collection.accept(feedback(2, a=1))
    collection.job.join(timeout=5)
    assert "capture_paused" in collection.error
    assert collection.armed
    assert not list(tmp_path.rglob(READY_MARKER))
    manager.new_camera_client.assert_not_called()
    collection.close()


def make_ready(tmp_path, complete=True):
    root = tmp_path / "episode"
    root.mkdir()
    cameras = {}
    for name in CAMERA_MAPPING.values():
        frames = root / "camera" / name / "frames"
        frames.mkdir(parents=True)
        (frames / "00000000.jpg").write_bytes(b"synthetic")
        (frames.parent / (name + ".txt")).write_text("100 0\n")
        cameras[name] = {"frames": 1, "width": 640, "height": 480}
    (root / "meta_info.json").write_text(
        json.dumps(
            {
                "robot_type": "tianji",
                "complete": complete,
                "actions_recorded": True,
                "cameras": cameras,
                "controlled_sides": ["left"],
            }
        )
    )
    (root / "record").mkdir()
    for name in ("actions.jsonl", "bundles.jsonl"):
        (root / "record" / name).write_text('{"synthetic":true}\n')
    (root / READY_MARKER).write_text(
        json.dumps(
            {
                "author": "collector-test",
                "requested_cameras": list(CAMERA_MAPPING.values()),
            }
        )
    )
    return root


def test_upload_failure_retries_and_never_marks_success_early(tmp_path, account):
    root = make_ready(tmp_path)
    with platform_client([], fail_upload=True) as client:
        uploader = CollectionUploader(tmp_path, "https://platform.test", client=client)
        uploader.scan_once()
        assert uploader.status()["failed"] == 1
        assert not (root / UPLOADED_MARKER).exists()
    with platform_client([]) as client:
        CollectionUploader(tmp_path, "https://platform.test", client=client).scan_once()
    assert (root / UPLOADED_MARKER).exists()


def test_incomplete_and_native_data_refused_and_account_bound(
    tmp_path, account, monkeypatch
):
    root = make_ready(tmp_path, complete=False)
    seen = []
    with platform_client(seen) as client:
        uploader = CollectionUploader(tmp_path, "https://platform.test", client=client)
        uploader.scan_once()
        assert not any(r.url.path == "/uploads/presign" for r in seen)
        assert not (root / UPLOADED_MARKER).exists()
        meta = json.loads((root / "meta_info.json").read_text())
        meta["complete"] = True
        (root / "meta_info.json").write_text(json.dumps(meta))
        native = root / "camera/head_color/native/frame.jpg"
        native.parent.mkdir()
        native.write_bytes(b"must-not-upload")
        uploader.scan_once()
        assert not any(r.url.path == "/uploads/presign" for r in seen)
        monkeypatch.setattr(
            collection_uploader, "current_platform_username", lambda: "other-operator"
        )
        count = len(seen)
        uploader.scan_once()
        assert len(seen) == count
        assert uploader.status()["pending"] == 1


def test_tianji_preview_rate_excludes_disk_finalize_time():
    from teleop_control.collection_uploader import _tianji_playback_timing
    metadata = dict(requested_image_hz=30, frames_recorded=234,
                    first_bundle_ts_ns=1_000_000_000, last_bundle_ts_ns=8_810_181_027,
                    pc_start_ns=10_000_000_000, pc_stop_ns=18_362_744_852)
    rate, span = _tianji_playback_timing(metadata)
    assert rate == 30
    assert span == pytest.approx(7.810181027)
    metadata['pc_stop_ns'] += 120_000_000_000  # Slow disk or audit cannot slow the video.
    assert _tianji_playback_timing(metadata) == (rate, span)


@pytest.mark.parametrize('rate', [0, -1, float('nan'), float('inf'), True])
def test_tianji_rejects_invalid_preview_rate(rate):
    from teleop_control.collection_uploader import _tianji_playback_timing
    with pytest.raises(ValueError, match='帧率'):
        _tianji_playback_timing(dict(requested_image_hz=rate))


def waiting_feedback(sequence, selected=('left', 'right')):
    row = feedback(sequence, selected)
    for motor in row['grippers']['motors'].values():
        motor.update(state='READY', target_rad=None)
        motor['feedback']['state'] = 0
    return row


@pytest.mark.parametrize('selected', [('left', 'right'), ('left',), ('right',)])
def test_gripper_recording_reference_initializes_once_then_holds_real_target(selected):
    import copy
    collection = TianjiCollection(SimpleNamespace())
    collection.controlled_sides = selected
    first = waiting_feedback(1, selected)
    original = copy.deepcopy(first)
    initial = collection._gripper_action_reference(first)
    assert set(initial) == set(selected)
    assert all(r['position_rad'] == .5 and r['source'] == 'initial_feedback'
               and not r['controller_target_available'] for r in initial.values())
    assert first == original
    drift = waiting_feedback(2, selected)
    for m in drift['grippers']['motors'].values(): m['feedback']['position'] = .3
    assert collection._gripper_action_reference(drift) == initial
    actual = collection._gripper_action_reference(feedback(3, selected))
    assert all(r['position_rad'] == .8 and r['source'] == 'last_controller_target'
               and r['controller_target_available'] for r in actual.values())
    waiting = collection._gripper_action_reference(waiting_feedback(4, selected))
    assert all(r['position_rad'] == .8 and not r['controller_target_available']
               for r in waiting.values())
    assert all(r['position_rad'] == .5 for r in initial.values())


@pytest.mark.parametrize('invalid', ['stale', 'future', 'nan', 'fault', 'simulation', 'motor_fault'])
def test_gripper_recording_reference_cannot_seed_from_invalid_feedback(invalid):
    collection = TianjiCollection(SimpleNamespace())
    row = waiting_feedback(1)
    motor = row['grippers']['motors']['left']
    if invalid == 'stale': motor['feedback']['received'] -= 1
    if invalid == 'future': motor['feedback']['received'] += 1
    if invalid == 'nan': motor['feedback']['position'] = float('nan')
    if invalid == 'fault': row['grippers']['fault'] = 'left: timeout'
    if invalid == 'simulation': row['mode'] = 'simulation'
    if invalid == 'motor_fault': motor['feedback']['state'] = 8
    reference = collection._gripper_action_reference(row)['left']
    assert reference['position_rad'] is None and reference['source'] == 'unavailable'
    good = collection._gripper_action_reference(waiting_feedback(2))['left']
    assert good['position_rad'] == .5


def test_gripper_reference_resets_for_new_sdk_session():
    collection = TianjiCollection(SimpleNamespace())
    collection._gripper_action_reference(feedback(1))
    row = waiting_feedback(2)
    row['session'] = 'new-sdk-session'
    assert collection._gripper_action_reference(row)['left']['position_rad'] == .5


def test_gripper_reference_survives_episode_boundary_but_resets_on_rearm(tmp_path, account):
    collection = TianjiCollection(SimpleNamespace(), tmp_path)
    try:
        collection.arm({'task': 'test', 'mode': 'dual'})
        collection.accept(feedback(1))
        collection.accept(waiting_feedback(2))
        assert collection.latest['gripper_action_reference']['left']['position_rad'] == .8
        collection.phase = 'saved'
        collection.accept(waiting_feedback(3))
        assert collection.latest['gripper_action_reference']['left']['position_rad'] == .8
        collection.disarm()
        collection.arm({'task': 'test', 'mode': 'left'})
        collection.accept(waiting_feedback(4, ('left',)))
        assert collection.latest['gripper_action_reference']['left']['position_rad'] == .5
        assert set(collection.latest['gripper_action_reference']) == {'left'}
    finally:
        collection.close()
