"""Extract DAgger correction windows from a raw AgiBot episode.

The output keeps the stock gdk_data_collection layout so its
``scripts/convert_to_lerobot.py`` converter can produce LeRobot v3.0 data.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

REQUIRED_DAGGER_CAMERAS = ("head_color", "hand_right_color")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")


def correction_ranges(markers: list[dict], pre_s: float, post_s: float):
    events = sorted(markers, key=lambda row: int(row["pc_ts_ns"]))
    ranges = []
    takeover = None
    for row in events:
        name = row.get("name")
        if name == "takeover" and takeover is None:
            takeover = int(row["pc_ts_ns"])
        elif name in {"resume", "session_end"} and takeover is not None:
            end = int(row["pc_ts_ns"])
            ranges.append((takeover - int(pre_s * 1e9),
                           end + int(post_s * 1e9), takeover, end))
            takeover = None
    return ranges


def _unique_dir(root: Path, name: str) -> Path:
    candidate = root / name
    suffix = 1
    while candidate.exists():
        candidate = root / f"{name}_{suffix:02d}"
        suffix += 1
    return candidate


def _copy_camera(camera_dir: Path, dst: Path, wanted_ts: set[int]) -> int:
    index_path = camera_dir / f"{camera_dir.name}.txt"
    rows = []
    for line in index_path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and int(parts[0]) in wanted_ts:
            rows.append((int(parts[0]), int(parts[1])))
    frames_out = dst / "camera" / camera_dir.name / "frames"
    frames_out.mkdir(parents=True, exist_ok=True)
    index_out = frames_out.parent / f"{camera_dir.name}.txt"
    index_lines = []
    for new_no, (timestamp, old_no) in enumerate(rows):
        matches = list((camera_dir / "frames").glob(f"{old_no:08d}.*"))
        if not matches:
            continue
        source = matches[0]
        shutil.copy2(source, frames_out / f"{new_no:08d}{source.suffix}")
        index_lines.append(f"{timestamp} {new_no}\n")
    index_out.write_text("".join(index_lines))
    return len(index_lines)


def _valid_camera_rows(camera_dir: Path) -> list[tuple[int, int]]:
    index_path = camera_dir / f"{camera_dir.name}.txt"
    if not index_path.is_file():
        return []
    rows = []
    for line in index_path.read_text().splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        timestamp, frame_number = int(parts[0]), int(parts[1])
        if any((camera_dir / "frames").glob(f"{frame_number:08d}.*")):
            rows.append((timestamp, frame_number))
    return rows


def synchronize_episode_frames(
    episode_dir: str | Path,
    required_cameras: tuple[str, ...] = REQUIRED_DAGGER_CAMERAS,
) -> dict[str, int]:
    """Keep only bundle rows backed by every required exact-timestamp image."""
    episode = Path(episode_dir).expanduser().resolve()
    bundle_path = episode / "record" / "bundles.jsonl"
    bundles = read_jsonl(bundle_path)
    valid_rows = {
        camera: _valid_camera_rows(episode / "camera" / camera)
        for camera in required_cameras
    }
    valid_timestamps = {
        camera: {timestamp for timestamp, _frame in rows}
        for camera, rows in valid_rows.items()
    }
    complete = [
        row
        for row in bundles
        if all(
            isinstance(row.get("cam_ts", {}).get(camera), int)
            and int(row["cam_ts"][camera]) in valid_timestamps[camera]
            for camera in required_cameras
        )
    ]
    if len(complete) < 2:
        counts = ", ".join(
            f"{camera}={len(valid_rows[camera])}" for camera in required_cameras
        )
        raise ValueError(f"双相机同步帧不足：complete={len(complete)}, {counts}")
    for sequence, row in enumerate(complete, 1):
        row["seq"] = sequence
    write_jsonl(bundle_path, complete)

    camera_counts: dict[str, int] = {}
    for camera_dir in sorted((episode / "camera").glob("*")):
        if not camera_dir.is_dir():
            continue
        wanted = {
            int(row["cam_ts"][camera_dir.name])
            for row in complete
            if isinstance(row.get("cam_ts", {}).get(camera_dir.name), int)
        }
        kept = [
            (timestamp, frame_number)
            for timestamp, frame_number in _valid_camera_rows(camera_dir)
            if timestamp in wanted
        ]
        (camera_dir / f"{camera_dir.name}.txt").write_text(
            "".join(f"{timestamp} {frame_number}\n" for timestamp, frame_number in kept)
        )
        camera_counts[camera_dir.name] = len(kept)

    meta_path = episode / "meta_info.json"
    meta = json.loads(meta_path.read_text())
    meta["frames_recorded"] = len(complete)
    meta["first_bundle_ts_ns"] = int(complete[0]["timestamp_ns"])
    meta["last_bundle_ts_ns"] = int(complete[-1]["timestamp_ns"])
    for camera, count in camera_counts.items():
        if camera in meta.get("cameras", {}):
            meta["cameras"][camera]["frames"] = count
    meta["synchronization"] = {
        "required_cameras": list(required_cameras),
        "source_bundle_frames": len(bundles),
        "complete_frames": len(complete),
        "dropped_incomplete_frames": len(bundles) - len(complete),
        "rule": "exact declared camera timestamp with an existing image file",
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return {
        "source_frames": len(bundles),
        "complete_frames": len(complete),
        "dropped_frames": len(bundles) - len(complete),
    }


def extract_window(
    episode_dir: Path,
    output_root: Path,
    index: int,
    start_pc_ns: int,
    end_pc_ns: int,
    takeover_pc_ns: int,
    resume_pc_ns: int,
    pre_s: float,
    post_s: float,
) -> Path:
    bundles = read_jsonl(episode_dir / "record" / "bundles.jsonl")
    selected = [row for row in bundles
                if start_pc_ns <= int(row["pc_recv_ns"]) <= end_pc_ns]
    if len(selected) < 2:
        raise ValueError(f"纠正窗口 {index} 只有 {len(selected)} 帧，无法保存")
    for seq, row in enumerate(selected, 1):
        row["seq"] = seq

    output_root.mkdir(parents=True, exist_ok=True)
    dst = _unique_dir(
        output_root, f"{episode_dir.name}_correction_{index:03d}"
    )
    (dst / "record").mkdir(parents=True)
    write_jsonl(dst / "record" / "bundles.jsonl", selected)

    actions = [row for row in read_jsonl(episode_dir / "record" / "actions.jsonl")
               if start_pc_ns <= int(row["pc_ts_ns"]) <= end_pc_ns]
    expert_action_count = sum(
        row.get("source") == "expert"
        and takeover_pc_ns <= int(row["pc_ts_ns"]) <= resume_pc_ns
        for row in actions
    )
    write_jsonl(dst / "record" / "actions.jsonl", actions)
    if actions:
        (dst / "record" / "actions_meta.json").write_text(json.dumps({
            "clock_mapping": "preserved from source episode",
            "names": actions[0].get("names", []),
            "count": len(actions),
            "sources": sorted({row.get("source", "unknown") for row in actions}),
        }, indent=2))

    markers = [row for row in read_jsonl(
        episode_dir / "record" / "dagger_markers.jsonl"
    ) if start_pc_ns <= int(row["pc_ts_ns"]) <= end_pc_ns]
    write_jsonl(dst / "record" / "dagger_markers.jsonl", markers)

    clocks = read_jsonl(episode_dir / "record" / "clock_offset.jsonl")
    selected_clocks = [row for row in clocks
                       if start_pc_ns <= int(row["pc_ts_ns"]) <= end_pc_ns]
    if not selected_clocks and clocks:
        selected_clocks = [min(
            clocks,
            key=lambda row: abs(int(row["pc_ts_ns"]) - start_pc_ns),
        )]
    write_jsonl(dst / "record" / "clock_offset.jsonl", selected_clocks)

    for name in ("right_only_meta.json", "image_preprocessing.json"):
        source = episode_dir / "record" / name
        if source.exists():
            shutil.copy2(source, dst / "record" / name)

    camera_counts = {}
    for camera_dir in sorted((episode_dir / "camera").glob("*")):
        if not camera_dir.is_dir():
            continue
        wanted = {
            int(row["cam_ts"][camera_dir.name])
            for row in selected
            if row.get("cam_ts", {}).get(camera_dir.name) is not None
        }
        camera_counts[camera_dir.name] = _copy_camera(camera_dir, dst, wanted)

    meta = json.loads((episode_dir / "meta_info.json").read_text())
    meta["episode_id"] = dst.name
    meta["pc_start_ns"] = int(selected[0]["pc_recv_ns"])
    meta["pc_stop_ns"] = int(selected[-1]["pc_recv_ns"])
    meta["first_bundle_ts_ns"] = int(selected[0]["timestamp_ns"])
    meta["last_bundle_ts_ns"] = int(selected[-1]["timestamp_ns"])
    meta["frames_recorded"] = len(selected)
    for camera, count in camera_counts.items():
        if camera in meta.get("cameras", {}):
            meta["cameras"][camera]["frames"] = count
    meta["dagger"] = {
        "source_episode": str(episode_dir),
        "correction_index": index,
        "pre_seconds": pre_s,
        "post_seconds": post_s,
        "takeover_pc_ts_ns": takeover_pc_ns,
        "resume_pc_ts_ns": resume_pc_ns,
        "window_start_pc_ts_ns": start_pc_ns,
        "window_end_pc_ts_ns": end_pc_ns,
        "expert_action_count": expert_action_count,
        "action_sources": sorted({row.get("source", "unknown") for row in actions}),
    }
    (dst / "meta_info.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2)
    )
    return dst


def save_correction_windows(
    episode_dir: str | Path,
    output_root: str | Path,
    pre_s: float,
    post_s: float,
) -> list[Path]:
    episode = Path(episode_dir).expanduser().resolve()
    markers = read_jsonl(episode / "record" / "dagger_markers.jsonl")
    ranges = correction_ranges(markers, pre_s, post_s)
    if not ranges:
        raise ValueError("本次记录没有完整的接管/继续区间")
    actions = read_jsonl(episode / "record" / "actions.jsonl")
    missing_expert = [
        index
        for index, (_, _, takeover, resume) in enumerate(ranges, 1)
        if not any(
            row.get("source") == "expert"
            and takeover <= int(row["pc_ts_ns"]) <= resume
            for row in actions
        )
    ]
    if missing_expert:
        numbers = "、".join(str(index) for index in missing_expert)
        raise ValueError(
            f"接管区间 {numbers} 没有记录到 expert 动作，未保存任何纠正片段；"
            "请确认 XR 数据流正常，并在遥操作时持续按住右手 GRIP"
        )
    return [
        extract_window(
            episode, Path(output_root).expanduser(), index,
            start, end, takeover, resume, pre_s, post_s,
        )
        for index, (start, end, takeover, resume) in enumerate(ranges, 1)
    ]


def save_full_episode(episode_dir: str | Path, output_root: str | Path) -> Path:
    """Move the finalized raw DAgger episode into the visible import root."""
    episode = Path(episode_dir).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = _unique_dir(output, f"{episode.name}_dagger_full")
    shutil.move(str(episode), str(destination))
    meta_path = destination / "meta_info.json"
    meta = json.loads(meta_path.read_text())
    meta["episode_id"] = destination.name
    meta["dagger"] = {
        **(meta.get("dagger") if isinstance(meta.get("dagger"), dict) else {}),
        "save_mode": "full",
        "source_episode": str(episode),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return destination
