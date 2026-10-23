"""Exercise actual H.264 encoding and timestamp-preserving MCAP reconstruction."""
import io
import json
import struct
import sys
from pathlib import Path

import pytest
import yaml
from PIL import Image
from mcap.reader import make_reader
from mcap.writer import Writer, CompressionType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "umi_collection/vendor/galaxis/galaxis-core/galaxis-core"))
from galaxis_core import video_bundle as bundle


def image_message(timestamp, color):
    payload = bytearray(b"\x00\x01\x00\x00" + struct.pack("<iI", 100+timestamp//1_000_000_000, timestamp%1_000_000_000))
    for value in (b"camera\x00", b"jpeg\x00"):
        payload.extend(b"\x00" * ((4 - (len(payload) - 4) % 4) % 4))
        payload.extend(struct.pack("<I", len(value)) + value)
    payload.extend(b"\x00" * ((4 - (len(payload) - 4) % 4) % 4))
    output = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(output, "JPEG")
    return bytes(payload) + struct.pack("<I", len(output.getvalue())) + output.getvalue()


def recording(root, sides=("left", "right", "center"), offsets=(0, 33000000, 68000000, 99000000, 134000000), log_offsets=None):
    root.mkdir()
    source = root / "source.mcap"
    with source.open("wb") as stream:
        writer = Writer(stream, compression=CompressionType.NONE)
        writer.start(profile="ros2")
        schema = writer.register_schema("sensor_msgs/msg/CompressedImage", "ros2msg", b"test image schema")
        cameras = {side: writer.register_channel(f"/{side}_camera/image_raw/compressed", "cdr", schema) for side in sides}
        signals = writer.register_channel("/joint_states", "cdr", writer.register_schema("sensor_msgs/msg/JointState", "ros2msg", b"test joint schema"))
        for i, offset in enumerate(offsets):
            timestamp = 100000000000 + (offset if log_offsets is None else log_offsets[i])
            writer.add_message(signals, timestamp, b"exact-joint-data" + bytes([i]), timestamp - 5, i)
            for side, channel in cameras.items():
                writer.add_message(channel, timestamp, image_message(offset, {"left":"red", "right":"green", "center":"blue"}[side]), timestamp - 9, i)
        writer.finish()
    (root / "metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
        "relative_file_paths": [source.name], "topics_with_message_count": [],
        "starting_time": {"nanoseconds_since_epoch": 100000000000}, "duration": {"nanoseconds": 134000000},
    }}))
    return source


def contract(root, sides=('left','right','center'), width=64, height=48):
    value=dict(cameras={side:dict(image_width=width,image_height=height,framerate=30.0,pixel_format='mjpeg2rgb') for side in sides},
               limits=dict(min_rate_hz=29.0,max_frame_gap_sec=1.0,rate_window_sec=3.0,stop_min_rate_hz=27.0,stop_low_rate_sec=2.0,warn_frame_gap_sec=.2))
    (root/'camera-contract.json').write_text(json.dumps(value))
    return value


def test_recording_contract_validates_actual_frames_and_keeps_audit_in_manifest(tmp_path):
    root=tmp_path/'round';source=recording(root)
    selected=contract(root)
    manifest=bundle.convert_bundle(root)
    assert manifest['crf']==14
    assert manifest['encoder'] == dict(codec='libx264', preset='veryfast', pixel_format='yuv420p')
    assert manifest['camera_contract']==selected
    for camera in manifest['cameras']:
        assert camera['size']==[64,48] and camera['frame_count']==5
        assert abs((camera['frame_count']-1)/camera['fps']-.134)<1e-8
    assert all(v['passed'] and v['actual_fps']>=29.7 for v in manifest['quality_validation'].values())
    assert source.exists()


def test_hd_finalization_budget_scales_with_recorded_duration(tmp_path):
    assert bundle.conversion_timeout(tmp_path)==300
    path=tmp_path/'metadata.yaml'
    for seconds,expected in [(30,300),(120,840),(180,1200),(1800,3600)]:
        path.write_text(yaml.safe_dump({'rosbag2_bagfile_information':{'duration':{'nanoseconds':seconds*10**9}}}))
        assert bundle.conversion_timeout(tmp_path)==expected


def test_duplicate_samples_never_become_successful_video(tmp_path):
    offsets = (0,33333333,33333333)
    root=tmp_path/'round';source=recording(root,offsets=offsets)
    original=bundle.file_hash(source);contract(root)
    with pytest.raises(ValueError,match='质量不合格'):bundle.convert_bundle(root)
    assert not (root/'media').exists() and bundle.file_hash(source)==original


@pytest.mark.parametrize('offsets', [(0,1100000000,1200000000), tuple(i*50000000 for i in range(160))])
def test_low_fps_and_stalls_save_video_with_quality_warnings(tmp_path, offsets):
    root=tmp_path/'round';source=recording(root,offsets=offsets)
    original=bundle.file_hash(source);contract(root)
    manifest=bundle.convert_bundle(root)
    assert manifest['state'] == 'complete'
    assert all(not v['passed'] and v['warnings'] and v['timing_policy'] == 'diagnostic_only'
               for v in manifest['quality_validation'].values())
    assert bundle.file_hash(source) == original


def test_driver_resolution_fallback_cannot_pass_final_validation(tmp_path):
    root=tmp_path/'round';source=recording(root)
    contract(root,width=1920,height=1080)
    with pytest.raises(ValueError,match='分辨率不一致'):bundle.convert_bundle(root)
    assert source.exists() and not (root/'media').exists()


@pytest.mark.parametrize("sides", [("left", "right", "center"), ("left", "center"), ("right", "center")])
def test_mp4_round_trip_preserves_all_non_image_bytes_and_frame_timestamps(tmp_path, sides):
    root = tmp_path / "round"
    source = recording(root, sides)
    original_hash = bundle.file_hash(source)
    manifest = bundle.convert_bundle(root)
    assert len(manifest["cameras"]) == len(sides)
    restored = tmp_path / "restored.mcap"
    bundle.restore_for_ingest(root / "media", restored)
    def messages(path):
        with path.open("rb") as stream:
            return [(c.topic, m.log_time, m.publish_time, m.sequence,
                     bundle.split_image(m.data)[0] if c.topic in bundle.CAMERAS else m.data)
                    for _, c, m in make_reader(stream).iter_messages()]
    assert sorted(messages(source)) == sorted(messages(restored))
    assert bundle.file_hash(source) == original_hash
    assert bundle.convert_bundle(root) == manifest  # crash/restart retry is idempotent
    with pytest.raises(ValueError, match="metadata"):
        bundle.cleanup_source(root, manifest)
    (root / "meta.json").write_text("{}")
    bundle.cleanup_source(root, manifest)
    assert not source.exists()
    assert not (root / "metadata.yaml").exists()
    assert bundle.validate_bundle(root / "media")["state"] == "complete"


def test_corrupted_video_is_rejected_and_source_survives(tmp_path):
    root = tmp_path / "round"
    source = recording(root)
    bundle.convert_bundle(root)
    (root / "media/left.mp4").write_bytes(b"broken")
    with pytest.raises(ValueError, match="checksum"):
        bundle.restore_for_ingest(root / "media", tmp_path / "restored.mcap")
    assert source.is_file()


def test_decode_errors_are_rejected_even_with_matching_hash(tmp_path):
    root = tmp_path / 'round'
    source = recording(root)
    manifest = bundle.convert_bundle(root)
    path = root / 'media/left.mp4'
    payload = bytearray(path.read_bytes())
    start = payload.index(b'mdat') + 4
    # Keep the MP4 index intact, corrupt the actual compressed frame packets.
    payload[start:start + 24] = b'\x00' * 24
    path.write_bytes(payload)
    camera = next(c for c in manifest['cameras'] if c['file'] == 'left.mp4')
    camera['sha256'] = bundle.file_hash(path)
    (root / 'media/video-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises((ValueError, bundle.subprocess.CalledProcessError)):
        bundle.validate_bundle(root / 'media')
    assert source.exists()


def test_only_supervised_conversion_uses_pause_aware_timeout(monkeypatch):
    monkeypatch.delenv('GALAXIS_MANAGED_FINALIZER', raising=False)
    assert bundle.operation_timeout(180) == 180
    monkeypatch.setenv('GALAXIS_MANAGED_FINALIZER', '1')
    assert bundle.operation_timeout(180) is None


def test_encoder_failure_never_publishes_bundle_or_deletes_source(tmp_path, monkeypatch):
    root = tmp_path / "round"
    source = recording(root)
    original = bundle.file_hash(source)
    def failed(*args, **kwargs):
        raise FileNotFoundError("ffmpeg missing")
    monkeypatch.setattr(bundle.subprocess, "Popen", failed)
    with pytest.raises(FileNotFoundError):
        bundle.convert_bundle(root)
    assert not (root / "media").exists()
    assert bundle.file_hash(source) == original


def test_legacy_bundle_fps_uses_sensor_timestamps_not_bursty_arrival(tmp_path):
    root=tmp_path/'legacy'
    recording(root, offsets=tuple(i*33333333 for i in range(5)), log_offsets=(0,1000000,2000000,140000000,150000000))
    manifest=bundle.convert_bundle(root)
    assert all(abs(camera['fps']-30)<1e-5 for camera in manifest['cameras'])


def test_parallel_validation_checks_every_camera_and_propagates_failure(tmp_path, monkeypatch):
    import threading
    root = tmp_path / 'round'
    recording(root)
    manifest = bundle.convert_bundle(root)
    barrier = threading.Barrier(3, timeout=5)
    checked = []
    def probe(path, **kwargs):
        checked.append(path.name)
        barrier.wait()
        if path.name == 'right.mp4':
            raise ValueError('right camera decode error')
        return dict(codec_name='h264', nb_read_frames=5, width=64, height=48)
    monkeypatch.setattr(bundle, 'probe_video', probe)
    with pytest.raises(ValueError, match='right camera decode error'):
        bundle.validate_bundle(root / 'media')
    assert set(checked) == {c['file'] for c in manifest['cameras']}
