"""UMI MP4 bundle, with exact non-image messages and per-frame ROS timestamps.

This module is also shipped in the platform worker. Only Python, mcap and
FFmpeg are required; importing ROS or deserializing robot messages is avoided.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import statistics
import struct
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from pathlib import Path

from mcap.reader import make_reader
from mcap.writer import Writer, CompressionType

FORMAT = "umi-mp4-v1"
MANIFEST = "video-manifest.json"
CAMERAS = {f"/{side}_camera/image_raw/compressed": side for side in ("left", "right", "center")}


def cleanup_interrupted_encodes(record_dir: Path) -> None:
    """Called by the exclusive finalizer, before retrying an interrupted job.

    Only explicitly marked temporary encodes are disposable. Originals and
    published media remain untouched, including when a machine lost power.
    """
    for stage in Path(record_dir).glob('.mp4-*'):
        if stage.is_symlink() or not stage.is_dir() or not re.fullmatch(r'\.mp4-[a-z0-9_]{8}', stage.name):
            continue
        marker = stage / '.encoding-stage'
        if marker.is_symlink() or not marker.is_file():
            continue
        if marker.read_text() == FORMAT:
            shutil.rmtree(stage)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("Invalid MP4 bundle artifact name")
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing MP4 bundle artifact: {name}")
    return path


def split_image(payload: bytes) -> tuple[bytes, bytes]:
    """Keep the original CDR header/format; extract only the byte-array field."""
    if payload[:2] not in (b"\x00\x01", b"\x00\x00"):
        raise ValueError("Unsupported CompressedImage CDR encoding")
    order = "<" if payload[1] else ">"
    offset = 12  # encapsulation + Header.stamp
    for _ in range(2):  # Header.frame_id, format
        offset = 4 + ((offset - 4 + 3) // 4) * 4
        size = struct.unpack_from(order + "I", payload, offset)[0]
        if size < 1 or offset + 4 + size > len(payload):
            raise ValueError("Invalid CompressedImage string")
        offset += 4 + size
    offset = 4 + ((offset - 4 + 3) // 4) * 4
    size = struct.unpack_from(order + "I", payload, offset)[0]
    jpeg = payload[offset + 4:offset + 4 + size]
    if len(jpeg) != size or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
        raise ValueError("MP4 recording requires complete JPEG camera frames")
    return payload[:offset], jpeg


def join_image(prefix: bytes, jpeg: bytes) -> bytes:
    return prefix + struct.pack(("<" if prefix[1] else ">") + "I", len(jpeg)) + jpeg


def _copy_message(writer, schemas, channels, schema, channel, message):
    if channel.id not in channels:
        schema_id = 0
        if schema:
            if schema.id not in schemas:
                schemas[schema.id] = writer.register_schema(schema.name, schema.encoding, schema.data)
            schema_id = schemas[schema.id]
        channels[channel.id] = writer.register_channel(
            channel.topic, channel.message_encoding, schema_id, channel.metadata)
    writer.add_message(channels[channel.id], message.log_time, message.data,
                       message.publish_time, message.sequence)


def _signal_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        for schema, channel, message in make_reader(stream, validate_crcs=True).iter_messages(log_time_order=False):
            if channel.topic in CAMERAS:
                continue
            for value in (channel.topic.encode(), channel.message_encoding.encode(),
                          schema.name.encode() if schema else b"", schema.data if schema else b"",
                          json.dumps(channel.metadata, sort_keys=True).encode(),
                          struct.pack("<QQI", message.log_time, message.publish_time, message.sequence), message.data):
                digest.update(struct.pack("<Q", len(value)))
                digest.update(value)
            count += 1
    return digest.hexdigest(), count


def operation_timeout(seconds: float) -> float | None:
    # The queue supervisor owns an active-time deadline. Wall-clock subprocess
    # deadlines would incorrectly expire while the whole encoder group is paused
    # for a recording. Standalone conversion retains its local timeouts.
    return None if os.environ.get('GALAXIS_MANAGED_FINALIZER') == '1' else seconds


def probe_video(path: Path, *, timeout: float = 180) -> dict:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-threads", "2", "-err_detect", "explode",
        "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,nb_read_frames", "-of", "json", str(path),
    ], check=True, capture_output=True, text=True, timeout=operation_timeout(timeout))
    # ffprobe may return zero even when decoding reports errors. Reject them,
    # rather than decoding every frame a second time with ffmpeg -f null.
    if result.stderr.strip():
        raise ValueError(f"Cannot decode MP4: {path.name}: {result.stderr[-1000:]}")
    return json.loads(result.stdout)["streams"][0]


def validate_bundle(root: Path, *, decode: bool = True) -> dict:
    manifest = json.loads(artifact(root, MANIFEST).read_text())
    if manifest.get("format") != FORMAT or manifest.get("state") != "complete":
        raise ValueError("Incomplete or unsupported MP4 bundle")
    cameras = manifest.get("cameras", [])
    topics = [c["topic"] for c in cameras]
    if (len(topics) != len(set(topics)) or not set(topics) <= CAMERAS.keys()
            or "/center_camera/image_raw/compressed" not in topics or len(topics) < 2):
        raise ValueError("MP4 bundle requires center and at least one wrist camera")
    files = [manifest["signals"], *cameras]
    names = [entry["file"] for entry in files]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate MP4 bundle filenames")
    for entry in files:
        path = artifact(root, entry["file"])
        if file_hash(path) != entry["sha256"]:
            raise ValueError(f"MP4 bundle checksum mismatch: {path.name}")
    for camera in cameras:
        frames = camera["frames"]
        if not frames or len(frames) != camera["frame_count"]:
            raise ValueError("MP4 frame timestamps are incomplete")
        previous = -1
        for frame in frames:
            if (not isinstance(frame["log_time"], int) or frame["log_time"] < previous
                    or not isinstance(frame["publish_time"], int) or frame["publish_time"] < 0):
                raise ValueError("Invalid MP4 frame timestamp order")
            previous = frame["log_time"]
            prefix = bytes.fromhex(frame["cdr_prefix"])
            if len(prefix) < 20 or prefix[:2] not in (b"\x00\x01", b"\x00\x00"):
                raise ValueError("Invalid saved camera message header")
    if decode:
        # Independent cameras can be decoded concurrently without changing any
        # frames. Keep full decode, count, size and checksum checks. The finalizer
        # owns this entire process group and pauses it when recording starts.
        def verify_camera(camera):
            path = root / camera["file"]
            probe = probe_video(path, timeout=max(180, len(camera["frames"]) / 10))
            if (probe["codec_name"] != "h264" or int(probe["nb_read_frames"]) != len(camera["frames"])
                    or [probe["width"], probe["height"]] != camera["size"]):
                raise ValueError(f"MP4 frame count/size mismatch: {path.name}")
        with ThreadPoolExecutor(max_workers=min(3, len(cameras))) as pool:
            list(pool.map(verify_camera, cameras))
    return manifest


def conversion_timeout(record_dir: Path) -> float:
    """Give longer HD clips enough time to encode and fully decode for checks."""
    import yaml
    try:
        metadata = yaml.safe_load((record_dir / 'metadata.yaml').read_text())
        duration = metadata['rosbag2_bagfile_information']['duration']['nanoseconds'] / 1e9
        return max(300, min(3600, 6 * duration + 120))
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        return 300


def convert_bundle(record_dir: Path, *, crf: int = 14) -> dict:
    """Publish media/ atomically. Never delete or modify the source bag here."""
    import yaml
    record_dir = Path(record_dir)
    final = record_dir / "media"
    if final.exists():
        return validate_bundle(final)
    metadata = yaml.safe_load((record_dir / "metadata.yaml").read_text())
    info = metadata["rosbag2_bagfile_information"]
    names = info["relative_file_paths"]
    if len(names) != 1:
        raise ValueError("MP4 conversion requires one complete source MCAP")
    source = artifact(record_dir, names[0])
    timestamps = {}
    source_times = {}
    contract_file = record_dir / 'camera-contract.json'
    contract = json.loads(contract_file.read_text()) if contract_file.exists() else None
    with source.open("rb") as stream:
        for _, channel, message in make_reader(stream, validate_crcs=True).iter_messages(topics=list(CAMERAS), log_time_order=False):
            timestamps.setdefault(channel.topic, []).append(message.log_time)
            prefix, _ = split_image(message.data)
            sec, ns = struct.unpack_from(('<' if prefix[1] else '>')+'iI', prefix, 4)
            source_times.setdefault(channel.topic, []).append(sec*1_000_000_000+ns)
    if "/center_camera/image_raw/compressed" not in timestamps or len(timestamps) < 2:
        raise ValueError("MP4 recording requires center and at least one wrist camera")
    quality = {}
    if contract:
        from .camera_quality import recording_timing_result
        expected = {f'/{side}_camera/image_raw/compressed' for side in contract['cameras']}
        if set(timestamps) != expected:
            raise ValueError('相机数据不完整：录制的话题与配置不一致，原始数据保留')
        limits = contract['limits']
        for topic, values in timestamps.items():
            failure, warnings = recording_timing_result(values, source_times[topic], limits)
            if failure:
                raise ValueError(f'{topic} 质量不合格：{failure}；原始数据保留')
            relative = [(t-source_times[topic][0])/1e9 for t in source_times[topic]]
            quality[CAMERAS[topic]] = dict(actual_fps=(len(relative)-1)/relative[-1],
                max_frame_gap_ms=max(b-a for a,b in zip(relative,relative[1:]))*1000,
                frames=len(relative), passed=True, warnings=warnings)
    # Temporary files stay within the episode, so rename is on the same filesystem.
    with tempfile.TemporaryDirectory(prefix=".mp4-", dir=record_dir) as temp:
        stage = Path(temp)
        (stage / '.encoding-stage').write_text(FORMAT)
        cameras, processes, errors = {}, {}, {}
        counts = Counter()
        signals = stage / "signals.mcap"
        try:
            with signals.open("wb") as dest, source.open("rb") as stream:
                writer = Writer(dest, compression=CompressionType.ZSTD)
                writer.start(profile="ros2")
                schemas, channels = {}, {}
                # Physical order preserves the exact ordering of non-image messages.
                for schema, channel, message in make_reader(stream, validate_crcs=True).iter_messages(log_time_order=False):
                    if channel.topic not in CAMERAS:
                        _copy_message(writer, schemas, channels, schema, channel, message)
                        counts[channel.topic] += 1
                        continue
                    if schema is None or schema.name != "sensor_msgs/msg/CompressedImage":
                        raise ValueError("Unsupported camera schema")
                    prefix, jpeg = split_image(message.data)
                    if contract:
                        from PIL import Image
                        import io
                        size = list(Image.open(io.BytesIO(jpeg)).size)
                        wanted = contract['cameras'][CAMERAS[channel.topic]]
                        if size != [wanted['image_width'], wanted['image_height']]:
                            raise ValueError(f'{channel.topic} 实际图像尺寸 {size} 与所选分辨率不一致')
                    if channel.topic not in cameras:
                        from PIL import Image
                        import io
                        size = list(Image.open(io.BytesIO(jpeg)).size)
                        times = source_times[channel.topic]
                        deltas = [b - a for a, b in zip(times, times[1:]) if b > a]
                        # Arrival intervals may alternate short/long because of
                        # scheduling. Their median can shorten a minute-long HD
                        # video by seconds. Match the complete sensor time span;
                        # retain every original timestamp in the frame manifest.
                        if contract:
                            fps = round(quality[CAMERAS[channel.topic]]['actual_fps'], 9)
                        else:
                            if len(times) < 2 or len(deltas) != len(times) - 1:
                                raise ValueError('相机时间戳不足、重复或倒退，保留原始数据')
                            fps = round((len(times)-1)*1e9/(times[-1]-times[0]), 9)
                        name = CAMERAS[channel.topic] + ".mp4"
                        cameras[channel.topic] = {"topic": channel.topic, "file": name, "fps": fps,
                            "size": size, "schema_name": schema.name, "schema_encoding": schema.encoding,
                            "schema_data": schema.data.hex(), "message_encoding": channel.message_encoding,
                            "channel_metadata": channel.metadata, "frames": []}
                        errors[channel.topic] = tempfile.TemporaryFile()
                        processes[channel.topic] = subprocess.Popen([
                            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-filter_threads", "1", "-threads", "1",
                            "-f", "image2pipe", "-framerate", str(fps), "-vcodec", "mjpeg", "-i", "pipe:0",
                            "-an", "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", str(crf),
                            "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", "-movflags", "+faststart", str(stage / name),
                        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors[channel.topic])
                    camera = cameras[channel.topic]
                    frames = camera["frames"]
                    if frames and message.log_time < frames[-1]["log_time"]:
                        raise ValueError("Camera timestamps moved backwards")
                    processes[channel.topic].stdin.write(jpeg)
                    frames.append({"log_time": message.log_time, "publish_time": message.publish_time,
                                   "sequence": message.sequence, "cdr_prefix": prefix.hex()})
                writer.finish()
            for topic, process in processes.items():
                process.stdin.close()
                if process.wait(timeout=operation_timeout(180)) != 0:
                    errors[topic].seek(0)
                    raise RuntimeError(errors[topic].read().decode(errors="replace")[-2000:])
            if _signal_digest(source) != _signal_digest(signals):
                raise ValueError("Non-image messages changed during MP4 conversion")
            for camera in cameras.values():
                camera["sha256"] = file_hash(stage / camera["file"])
                camera["frame_count"] = len(camera["frames"])
            manifest = {"format": FORMAT, "state": "complete", "crf": crf,
                        "encoder": {"codec": "libx264", "preset": "veryfast", "pixel_format": "yuv420p"},
                        "source_file": source.name, "source_sha256": file_hash(source),
                        "signals": {"file": signals.name, "sha256": file_hash(signals)},
                        "cameras": list(cameras.values())}
            if contract:
                manifest['camera_contract'] = contract
                manifest['quality_validation'] = quality
            (stage / MANIFEST).write_text(json.dumps(manifest, separators=(",", ":")))
            # Metadata describes the small, non-image MCAP, not the deleted source.
            info["relative_file_paths"] = [signals.name]
            info["message_count"] = sum(counts.values())
            info["topics_with_message_count"] = [dict(row, message_count=counts[row["topic_metadata"]["name"]])
                for row in info.get("topics_with_message_count", []) if row["topic_metadata"]["name"] in counts]
            info["files"] = [{"path": signals.name, "starting_time": info.get("starting_time"),
                              "duration": info.get("duration"), "message_count": sum(counts.values())}]
            (stage / "metadata.yaml").write_text(yaml.safe_dump(metadata, sort_keys=False))
            validate_bundle(stage)
            (stage / '.encoding-stage').unlink()
            # Flush all completed artifacts before exposing the manifest.
            for path in stage.iterdir():
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            os.replace(stage, final)
            return manifest
        finally:
            for process in processes.values():
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
            for handle in errors.values():
                handle.close()


def cleanup_source(record_dir: Path, manifest: dict) -> None:
    """Only call after complete video validation and publishing episode metadata."""
    record_dir = Path(record_dir)
    if not (record_dir / "meta.json").is_file():
        raise ValueError("Cannot clean source before episode metadata is saved")
    source = record_dir / manifest["source_file"]
    if source.exists():
        if artifact(record_dir, manifest["source_file"]) != source or file_hash(source) != manifest["source_sha256"]:
            raise ValueError("Source changed since MP4 conversion; keeping original")
        source.unlink()
    (record_dir / "metadata.yaml").unlink(missing_ok=True)


def restore_for_ingest(root: Path, output: Path) -> dict:
    """Reconstruct a temporary image MCAP for the existing rebake pipeline.

    All ROS timestamps and non-image payloads are exact; camera pixels are
    decoded from the lossy H.264 source. This temporary file is never uploaded.
    """
    manifest = validate_bundle(root)
    with output.open("wb") as dest, artifact(root, manifest["signals"]["file"]).open("rb") as src:
        writer = Writer(dest, compression=CompressionType.ZSTD)
        writer.start(profile="ros2")
        schemas, channels = {}, {}
        for schema, channel, message in make_reader(src, validate_crcs=True).iter_messages(log_time_order=False):
            if channel.topic in CAMERAS:
                raise ValueError("Signals MCAP unexpectedly contains camera images")
            _copy_message(writer, schemas, channels, schema, channel, message)
        for camera in manifest["cameras"]:
            schema_id = writer.register_schema(camera["schema_name"], camera["schema_encoding"], bytes.fromhex(camera["schema_data"]))
            channel_id = writer.register_channel(camera["topic"], camera["message_encoding"], schema_id, camera["channel_metadata"])
            # A file avoids buffering a complete decoded camera stream in RAM.
            with tempfile.TemporaryFile() as jpeg_stream:
                subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(root / camera["file"]),
                                "-threads", "1", "-fps_mode", "passthrough", "-c:v", "mjpeg", "-q:v", "2",
                                "-f", "image2pipe", "-"], stdout=jpeg_stream, stderr=subprocess.PIPE,
                               check=True, timeout=300)
                jpeg_stream.seek(0)
                buffer = b""
                frames = iter(camera["frames"])
                count = 0
                while chunk := jpeg_stream.read(1024 * 1024):
                    buffer += chunk
                    while (end := buffer.find(b"\xff\xd9")) >= 0:
                        jpeg, buffer = buffer[:end + 2], buffer[end + 2:]
                        frame = next(frames, None)
                        if frame is None or not jpeg.startswith(b"\xff\xd8"):
                            raise ValueError("Decoded MP4 frame count mismatch")
                        payload = join_image(bytes.fromhex(frame["cdr_prefix"]), jpeg)
                        writer.add_message(channel_id, frame["log_time"], payload, frame["publish_time"], frame["sequence"])
                        count += 1
                if buffer or count != camera["frame_count"]:
                    raise ValueError("Decoded MP4 is missing frames")
        writer.finish()
    return manifest


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_dir", type=Path)
    args = parser.parse_args()
    result = convert_bundle(args.record_dir)
    print(json.dumps({"format": result["format"], "cameras": len(result["cameras"])}))
