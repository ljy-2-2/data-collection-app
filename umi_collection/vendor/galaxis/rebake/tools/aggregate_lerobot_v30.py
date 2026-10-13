#!/usr/bin/env python3
"""Aggregate schema-compatible Rebake episodes into one LeRobot v3 dataset."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from fractions import Fraction
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from lerobot_profile_contract import PROFILE_VIDEO_KEYS, validate_contract
from video_orientation import choose_next_vertical_orientation


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_episodes(src: Path) -> list[tuple[Path, dict]]:
    episodes = []
    for directory in sorted(src.iterdir()):
        info_path = directory / "meta/info.json"
        if not directory.is_dir() or directory.name.endswith("_old") or not info_path.exists():
            continue
        info = read_json(info_path)
        if info.get("codebase_version") == "v3.0" and (directory.parent / f"{directory.name}_old").exists():
            directory = directory.parent / f"{directory.name}_old"
            info = read_json(directory / "meta/info.json")
        if info.get("codebase_version") == "v2.1":
            episodes.append((directory, info))
    if not episodes:
        raise ValueError("no v2.1 episode datasets found")
    return episodes


def video_duration(path: Path) -> float:
    payload = json.loads(
        subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=nb_frames,avg_frame_rate:format=duration",
                "-of", "json", str(path),
            ],
            text=True,
        )
    )
    stream = payload["streams"][0]
    frame_count = stream.get("nb_frames")
    frame_rate = stream.get("avg_frame_rate")
    if frame_count not in (None, "N/A") and frame_rate not in (None, "0/0"):
        duration = int(frame_count) / float(Fraction(frame_rate))
    else:
        duration = float(payload["format"]["duration"])
    if duration <= 0:
        raise ValueError(f"video has non-positive duration: {path}")
    return duration


@contextmanager
def concat_manifest(paths: list[Path]):
    with tempfile.NamedTemporaryFile("w", delete=False) as manifest:
        for path in paths:
            manifest.write("file '" + str(path).replace("'", "'\\''") + "'\n")
        manifest_path = Path(manifest.name)
    try:
        yield manifest_path
    finally:
        manifest_path.unlink(missing_ok=True)


def concatenate_videos(paths: list[Path], destination: Path) -> list[float]:
    durations = [video_duration(path) for path in paths]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with concat_manifest(paths) as manifest_path:
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", str(manifest_path), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-crf", "18", "-preset", "fast", str(destination),
            ],
            check=True,
        )
    return durations


def _read_frame(stream, frame_size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < frame_size:
        chunk = stream.read(frame_size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    if not chunks:
        return None
    if len(chunks) != frame_size:
        raise ValueError(
            f"ffmpeg returned a partial raw frame: bytes={len(chunks)}, frame_size={frame_size}"
        )
    return bytes(chunks)


def detect_mixed_center_orientations(paths: list[Path]) -> list[bool] | None:
    """Return corrective per-frame flips, or None for an already-clean video.

    Legacy contaminated recordings start with the direct usb_cam transport;
    the intended software-corrected transport arrives shortly afterwards.
    Once at least one high-confidence mirrored transition proves that both
    publishers are present, invert the first-frame-relative solution so the
    result uses the configured corrected (upright) orientation.
    """
    width, height = 160, 120
    with concat_manifest(paths) as manifest_path:
        decoder = subprocess.Popen(
            [
                "ffmpeg", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", str(manifest_path), "-an", "-vf",
                f"scale={width}:{height},format=gray", "-fps_mode", "passthrough",
                "-f", "rawvideo", "-",
            ],
            stdout=subprocess.PIPE,
        )
        assert decoder.stdout is not None
        frame_size = width * height
        first = _read_frame(decoder.stdout, frame_size)
        if first is None:
            decoder.stdout.close()
            decoder.wait()
            raise ValueError("center video contains no frames")
        previous = np.frombuffer(first, dtype=np.uint8).reshape(
            height, width
        ).astype(np.int16)
        relative_states = [False]
        switches = 0
        while True:
            raw = _read_frame(decoder.stdout, frame_size)
            if raw is None:
                break
            current = np.frombuffer(raw, dtype=np.uint8).reshape(height, width)
            prior_state = relative_states[-1]
            state = choose_next_vertical_orientation(
                previous, current, prior_state
            )
            switches += state != prior_state
            relative_states.append(state)
            previous = (
                current[::-1].astype(np.int16)
                if state
                else current.astype(np.int16)
            )
        decoder.stdout.close()
        decoder_code = decoder.wait()
        if decoder_code:
            raise subprocess.CalledProcessError(decoder_code, "center orientation analysis")
    if switches == 0:
        return None
    return [not state for state in relative_states]


def _video_stream_contract(path: Path) -> tuple[int, int, str]:
    payload = json.loads(
        subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,avg_frame_rate",
                "-of", "json", str(path),
            ],
            text=True,
        )
    )
    stream = payload["streams"][0]
    return int(stream["width"]), int(stream["height"]), stream["avg_frame_rate"]


def _numeric_stats(frame: pd.DataFrame, features: dict) -> dict[str, dict[str, list[float]]]:
    """Compute LeRobot-compatible stats for every non-video feature.

    Rebake already materializes the complete numeric frame table.  Computing
    stats here keeps v3 outputs self-contained, so LeRobot's normalizers do not
    depend on a separate post-processing step.  Video stats are intentionally
    omitted; visual policies use their configured image normalization.
    """
    stats: dict[str, dict[str, list[float]]] = {}
    for key, spec in features.items():
        if spec.get("dtype") in {"video", "image", "string", "language"}:
            continue
        if key not in frame.columns:
            raise ValueError(f"feature {key!r} is missing from the aggregated parquet")
        values: list[np.ndarray] = []
        shape = tuple(spec.get("shape") or ())
        width = int(np.prod(shape)) if shape else 1
        for value in frame[key].tolist():
            array = np.asarray(value)
            if array.dtype.kind not in "biuf":
                raise ValueError(f"feature {key!r} contains non-numeric values")
            flattened = array.astype(np.float64, copy=False).reshape(-1)
            if flattened.size != width:
                raise ValueError(
                    f"feature {key!r} has {flattened.size} values, expected {width}"
                )
            values.append(flattened)
        if not values:
            continue
        flattened = np.stack(values, axis=0)
        stats[key] = {
            "min": np.min(flattened, axis=0).reshape(-1).tolist(),
            "max": np.max(flattened, axis=0).reshape(-1).tolist(),
            "mean": np.mean(flattened, axis=0).reshape(-1).tolist(),
            "std": np.std(flattened, axis=0).reshape(-1).tolist(),
        }
    return stats


def concatenate_center_video(paths: list[Path], destination: Path) -> list[float]:
    """Concatenate center episodes and repair proven legacy orientation mixing."""
    durations = [video_duration(path) for path in paths]
    destination.parent.mkdir(parents=True, exist_ok=True)
    corrective_flips = detect_mixed_center_orientations(paths)
    if corrective_flips is None:
        concatenate_videos(paths, destination)
        return durations
    print(
        "center-camera orientation repair: "
        f"correcting {sum(corrective_flips)}/{len(corrective_flips)} frames"
    )

    width, height, frame_rate = _video_stream_contract(paths[0])
    frame_size = width * height * 3
    with concat_manifest(paths) as manifest_path:
        decoder = subprocess.Popen(
            [
                "ffmpeg", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", str(manifest_path), "-an", "-pix_fmt", "rgb24",
                "-fps_mode", "passthrough", "-f", "rawvideo", "-",
            ],
            stdout=subprocess.PIPE,
        )
        encoder = subprocess.Popen(
            [
                "ffmpeg", "-y", "-v", "error", "-f", "rawvideo",
                "-pix_fmt", "rgb24", "-video_size", f"{width}x{height}",
                "-framerate", frame_rate, "-i", "-", "-an", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "fast",
                "-fps_mode", "cfr", "-video_track_timescale", "90000",
                "-f", "mp4", str(destination),
            ],
            stdin=subprocess.PIPE,
        )
        assert decoder.stdout is not None and encoder.stdin is not None
        written = 0
        try:
            for should_flip in corrective_flips:
                raw = _read_frame(decoder.stdout, frame_size)
                if raw is None:
                    raise ValueError(
                        f"center video ended at frame {written}; expected {len(corrective_flips)}"
                    )
                if should_flip:
                    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(
                        height, width, 3
                    )
                    encoder.stdin.write(pixels[::-1].tobytes())
                else:
                    encoder.stdin.write(raw)
                written += 1
            if decoder.stdout.read(1):
                raise ValueError("center orientation analysis/frame decode count mismatch")
        finally:
            decoder.stdout.close()
            encoder.stdin.close()
        decoder_code = decoder.wait()
        encoder_code = encoder.wait()
        if decoder_code or encoder_code:
            raise subprocess.CalledProcessError(
                decoder_code or encoder_code, "center-camera orientation repair"
            )
    return durations


def aggregate(src: Path, out: Path, profile: str) -> Path:
    episodes_in = collect_episodes(src)
    video_keys = validate_contract(episodes_in, profile)
    episode_tasks = []
    all_task_names = set()
    for directory, _info in episodes_in:
        task_rows = [
            json.loads(line)
            for line in (directory / "meta/tasks.jsonl").read_text().splitlines()
            if line.strip()
        ]
        local_tasks = {int(row["task_index"]): row["task"] for row in task_rows}
        if not local_tasks:
            raise ValueError(f"no task metadata in {directory.name}")
        episode_tasks.append(local_tasks)
        all_task_names.update(local_tasks.values())
    unique_tasks = sorted(all_task_names)
    global_task_indices = {task: index for index, task in enumerate(unique_tasks)}

    building = out.with_name(f"{out.name}.building")
    if building.exists():
        shutil.rmtree(building)
    (building / "data/chunk-000").mkdir(parents=True)
    (building / "meta/episodes/chunk-000").mkdir(parents=True)

    frames = []
    parquet_contract = None
    episode_rows = []
    offset = 0
    for index, (directory, _info) in enumerate(episodes_in):
        data_files = list((directory / "data").glob("chunk-*/*.parquet"))
        if len(data_files) != 1:
            raise ValueError(f"expected one parquet shard in {directory.name}, got {len(data_files)}")
        frame = pq.read_table(data_files[0]).to_pandas()
        current_contract = (tuple(frame.columns), tuple(map(str, frame.dtypes)))
        if parquet_contract is None:
            parquet_contract = current_contract
        elif current_contract != parquet_contract:
            raise ValueError(f"Parquet column/dtype mismatch in {directory.name}")
        episode = json.loads((directory / "meta/episodes.jsonl").read_text().splitlines()[0])
        length = int(episode["length"])
        if len(frame) != length:
            raise ValueError(f"frame count mismatch in {directory.name}: parquet={len(frame)}, meta={length}")
        required_indices = {"episode_index", "frame_index", "index", "task_index"}
        missing_indices = required_indices - set(frame.columns)
        if missing_indices:
            raise ValueError(
                f"missing index columns in {directory.name}: {sorted(missing_indices)}"
            )
        # Each standalone v2.1 dataset starts its counters at zero. Rebase all
        # dataset-global indices while retaining frame_index as per-episode.
        frame["episode_index"] = index
        frame["frame_index"] = range(length)
        frame["index"] = range(offset, offset + length)
        local_tasks = episode_tasks[index]
        try:
            frame["task_index"] = frame["task_index"].map(
                lambda local_index: global_task_indices[local_tasks[int(local_index)]]
            )
        except KeyError as exc:
            raise ValueError(
                f"unknown local task_index in {directory.name}: {exc.args[0]}"
            ) from exc
        frames.append(frame)
        episode_rows.append(
            {
                "episode_index": index,
                "data/chunk_index": 0,
                "data/file_index": 0,
                "dataset_from_index": offset,
                "dataset_to_index": offset + length,
                "tasks": episode.get("tasks", []),
            }
        )
        offset += length
    aggregated_frame = pd.concat(frames, ignore_index=True)
    aggregated_frame.to_parquet(
        building / "data/chunk-000/file-000.parquet", index=False
    )

    for key in video_keys:
        paths = []
        for directory, _info in episodes_in:
            matches = list((directory / "videos").glob(f"chunk-*/{key}/episode_*.mp4"))
            if len(matches) != 1:
                raise ValueError(f"expected one {key} video in {directory.name}, got {len(matches)}")
            paths.append(matches[0])
        destination = building / f"videos/{key}/chunk-000/file-000.mp4"
        if key == "observation.image.center":
            durations = concatenate_center_video(paths, destination)
        else:
            durations = concatenate_videos(paths, destination)
        timestamp = 0.0
        for row, duration in zip(episode_rows, durations):
            row[f"videos/{key}/chunk_index"] = 0
            row[f"videos/{key}/file_index"] = 0
            row[f"videos/{key}/from_timestamp"] = timestamp
            row[f"videos/{key}/to_timestamp"] = timestamp + duration
            timestamp += duration

    pd.DataFrame(episode_rows).to_parquet(
        building / "meta/episodes/chunk-000/file-000.parquet", index=False
    )
    pd.DataFrame({"task_index": range(len(unique_tasks)), "task": unique_tasks}).to_parquet(
        building / "meta/tasks.parquet", index=False
    )

    info = dict(episodes_in[0][1])
    info.update(
        codebase_version="v3.0",
        total_episodes=len(episode_rows),
        total_frames=offset,
        total_chunks=1,
        total_videos=len(video_keys),
        data_path="data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        video_path="videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        # The source v2.1 info may contain a stale `0:1` split.  The v3
        # aggregate contains every converted episode in one training split.
        splits={"train": f"0:{len(episode_rows)}"},
        collection_profile=profile,
    )
    (building / "meta/info.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (building / "meta/stats.json").write_text(
        json.dumps(_numeric_stats(aggregated_frame, info["features"]), indent=2) + "\n",
        encoding="utf-8",
    )

    previous = out.with_name(f"{out.name}.previous")
    if previous.exists():
        shutil.rmtree(previous)
    if out.exists():
        out.rename(previous)
    building.rename(out)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--profile", required=True, choices=sorted(PROFILE_VIDEO_KEYS))
    args = parser.parse_args()
    print(aggregate(args.src, args.out, args.profile))


if __name__ == "__main__":
    main()
