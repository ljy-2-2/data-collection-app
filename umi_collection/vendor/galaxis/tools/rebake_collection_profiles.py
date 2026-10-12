#!/usr/bin/env python3
"""Profile detection and Rebake config generation for Galaxis rosbags."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

import yaml


PROFILES = ("dual", "single-left", "single-right")
CAMERA_TOPICS = {
    "left": "/left_camera/image_raw/compressed",
    "right": "/right_camera/image_raw/compressed",
    "center": "/center_camera/image_raw/compressed",
}
PROFILE_CAMERAS = {
    "dual": ("left", "right", "center"),
    "single-left": ("left", "center"),
    "single-right": ("right", "center"),
}
PROFILE_ARMS = {
    "dual": ("left", "right"),
    "single-left": ("left",),
    "single-right": ("right",),
}


def topics_from_rosbag_metadata(path: Path) -> set[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    info = data.get("rosbag2_bagfile_information", data)
    topics = set()
    for item in info.get("topics_with_message_count", []):
        metadata = item.get("topic_metadata", {}) if isinstance(item, dict) else {}
        name = metadata.get("name")
        count = item.get("message_count", 0) if isinstance(item, dict) else 0
        if isinstance(name, str) and isinstance(count, int) and count > 0:
            topics.add(name)
    return topics


def detect_profile(topics: Iterable[str]) -> str:
    """Infer a strict collection contract from recorded image topics."""
    topic_set = set(topics)
    present = tuple(side for side, topic in CAMERA_TOPICS.items() if topic in topic_set)
    for profile, cameras in PROFILE_CAMERAS.items():
        if set(present) == set(cameras):
            if "/joint_states" not in topic_set:
                raise ValueError(f"{profile} bag is missing required /joint_states")
            return profile
    raise ValueError(
        "unsupported camera topic set: "
        + ", ".join(CAMERA_TOPICS[side] for side in present)
    )


def discover_bags(root: Path) -> dict[str, list[Path]]:
    grouped = {profile: [] for profile in PROFILES}
    errors = []
    for metadata in sorted(root.glob("*/metadata.yaml")):
        try:
            grouped[detect_profile(topics_from_rosbag_metadata(metadata))].append(metadata.parent)
        except ValueError as exc:
            errors.append(f"{metadata.parent.name}: {exc}")
    if errors:
        raise ValueError("cannot classify rosbags:\n  " + "\n  ".join(errors))
    return grouped


def _mentions_side(value: Any, side: str) -> bool:
    if not isinstance(value, str):
        return False
    return any(
        marker in value
        for marker in (
            f"/{side}_camera/",
            f".{side}_",
            f"/{side}_",
            f"pico_{side}_",
            f"{side}_hand_",
            f"{side}_left_finger_",
            f"{side}_right_finger_",
        )
    )


def build_robot_model(base_model: list[dict[str, Any]], profile: str) -> list[dict[str, Any]]:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    inactive = {"left", "right"} - set(PROFILE_ARMS[profile])
    active_joint_names = [f"{side}_right_finger_joint" for side in PROFILE_ARMS[profile]]
    result = []
    for original in base_model:
        entry = deepcopy(original)
        if any(
            _mentions_side(entry.get(key), side)
            for side in inactive
            for key in ("topic", "field", "feature")
        ):
            continue
        if entry.get("topic") in ("/joint_states", "/joint_states/action"):
            entry["names"] = list(active_joint_names)
        result.append(entry)
    expected_video_features = {
        f"observation.image.{side}" for side in PROFILE_CAMERAS[profile]
    }
    actual_video_features = {
        entry["feature"] for entry in result if entry.get("type") == "Video"
    }
    if actual_video_features != expected_video_features:
        raise ValueError(
            f"generated {profile} video contract mismatch: {sorted(actual_video_features)}"
        )
    return result


def build_pipeline(
    base_pipeline: dict[str, Any], profile: str, model_path: str, outdir: str
) -> dict[str, Any]:
    result = deepcopy(base_pipeline)
    inactive = {"left", "right"} - set(PROFILE_ARMS[profile])
    for stage in result.get("stage_configs", []):
        tf_config = stage.get("TfChainEnricherConfig")
        if isinstance(tf_config, dict):
            tf_config["frame_pairs"] = [
                pair
                for pair in tf_config.get("frame_pairs", [])
                if not any(
                    _mentions_side(pair.get("source"), side)
                    or _mentions_side(pair.get("target"), side)
                    for side in inactive
                )
            ]
        transformer = stage.get("LeRobotV21TransformerConfig")
        if isinstance(transformer, dict):
            transformer["robot_model"] = model_path
            transformer["outdir"] = outdir
    return result


def write_profile_configs(
    base_model_path: Path,
    base_pipeline_path: Path,
    output_root: Path,
    profile: str,
    container_output_root: str,
    container_work_root: str = "/data/rebake_work_user",
    container_config_root: str = "/data/rebake_profile_configs",
) -> tuple[Path, Path]:
    model = yaml.safe_load(base_model_path.read_text(encoding="utf-8"))
    pipeline = yaml.safe_load(base_pipeline_path.read_text(encoding="utf-8"))
    profile_dir = output_root / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    model_path = profile_dir / "robot_model.yaml"
    pipeline_path = profile_dir / "pipeline.yaml"
    model_path.write_text(
        yaml.safe_dump(build_robot_model(model, profile), sort_keys=False),
        encoding="utf-8",
    )
    generated_pipeline = build_pipeline(
        pipeline,
        profile,
        f"{container_config_root}/{profile}/robot_model.yaml",
        f"{container_output_root}/{profile}",
    )
    # Use a new operator-owned work tree instead of colliding with temporary
    # directories made by older root/nobody container runs.
    generated_pipeline["work_dir"] = f"{container_work_root}/{profile}"
    pipeline_path.write_text(
        yaml.safe_dump(generated_pipeline, sort_keys=False),
        encoding="utf-8",
    )
    return model_path, pipeline_path
