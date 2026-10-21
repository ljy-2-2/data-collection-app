#!/usr/bin/env python3
"""Build profile-filtered runtime config from the unified galaxis_config.yaml.

The unified config carries four sections (robot_config, galaxis_devices,
recording_gate, upload_targets) as complete, unpruned source material. This
script writes the profile-filtered runtime files to config/_runtime/single/,
applying the setup profile (/etc/galaxis/collection_profile.yaml) pruning and
saved camera modes on top.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

import yaml


MERGED_FILES = (
    "robot_config.yaml",
    "recording_gate.yaml",
    "upload_targets.yaml",
    "galaxis_devices.yaml",
)
PROFILE_DEFAULT = {
    "collection_profile": "dual",
    "active_cameras": ["left", "right", "center"],
    "active_arms": ["left", "right"],
    "camera_sources": {"center": "usb"},
}
CAMERA_MODE_KEYS = ("image_width", "image_height", "framerate", "pixel_format")
SUPPORTED_CAMERA_PIXEL_FORMATS = {"yuyv2rgb", "mjpeg2rgb"}


def load_yaml(path: Path) -> Any:
    with path.open("r") as f:
        loaded = yaml.safe_load(f)
    return loaded if loaded is not None else {}


def load_collection_profile(path: Path | None) -> dict[str, Any]:
    """Load and validate the setup profile; absent files preserve dual-arm."""
    profile = dict(PROFILE_DEFAULT)
    if path is not None and path.exists():
        loaded = load_yaml(path)
        if not isinstance(loaded, dict):
            raise ValueError(f"collection profile must be a mapping: {path}")
        profile.update(loaded)
    name = profile.get("collection_profile")
    cameras = profile.get("active_cameras")
    arms = profile.get("active_arms")
    expected = {
        "dual": (["left", "right", "center"], ["left", "right"]),
        "single-left": (["left", "center"], ["left"]),
        "single-right": (["right", "center"], ["right"]),
    }
    if name not in expected or (cameras, arms) != expected[name]:
        raise ValueError(
            f"invalid collection profile {name!r}: cameras={cameras!r}, arms={arms!r}"
        )
    sources = profile.get("camera_sources", {"center": "usb"})
    if not isinstance(sources, dict):
        raise ValueError("collection profile camera_sources must be a mapping")
    center_source = sources.get("center", "usb")
    if center_source not in {"usb", "g2_head_color"}:
        raise ValueError(f"unsupported center camera source: {center_source!r}")
    profile["camera_sources"] = {"center": center_source}
    quality = profile.get('camera_quality')
    if quality and quality.get('version', 1) < 2:
        profile['camera_quality'] = dict(version=2, min_rate_hz=27.0, rate_window_sec=5.0,
            warn_frame_gap_sec=0.5, stop_min_rate_hz=20.0, stop_low_rate_sec=10.0,
            max_frame_gap_sec=3.0, warmup_sec=1.0)
    return profile


def load_camera_overrides(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load and validate per-role camera modes saved by Device Setup."""
    if path is None or not path.exists():
        return {}
    loaded = load_yaml(path)
    cameras = loaded.get("cameras", {}) if isinstance(loaded, dict) else {}
    if cameras is None:
        return {}
    if not isinstance(cameras, dict):
        raise ValueError("collection profile cameras must be a mapping")
    result: dict[str, dict[str, Any]] = {}
    for side, settings in cameras.items():
        if side not in {"left", "right", "center"} or not isinstance(settings, dict):
            raise ValueError(f"invalid camera mode entry: {side!r}")
        missing = set(CAMERA_MODE_KEYS) - set(settings)
        if missing:
            raise ValueError(f"camera {side} mode missing: {sorted(missing)}")
        width = settings["image_width"]
        height = settings["image_height"]
        fps = settings["framerate"]
        pixel_format = settings["pixel_format"]
        if (
            not isinstance(width, int)
            or isinstance(width, bool)
            or width <= 0
            or not isinstance(height, int)
            or isinstance(height, bool)
            or height <= 0
        ):
            raise ValueError(f"camera {side} has invalid resolution {width}x{height}")
        try:
            fps = float(fps)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"camera {side} has invalid framerate {fps!r}") from exc
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError(f"camera {side} has invalid framerate {fps!r}")
        if pixel_format not in SUPPORTED_CAMERA_PIXEL_FORMATS:
            raise ValueError(
                f"camera {side} has unsupported pixel_format {pixel_format!r}"
            )
        result[side] = {
            "image_width": width,
            "image_height": height,
            "framerate": fps,
            "pixel_format": pixel_format,
        }
    return result


def apply_camera_overrides(
    data: Any, overrides: dict[str, dict[str, Any]]
) -> Any:
    """Apply Setup modes only to camera nodes present after profile pruning."""
    if not isinstance(data, dict):
        return data
    for side, settings in overrides.items():
        node = data.get(f"{side}_camera/usb_cam")
        if not isinstance(node, dict):
            continue
        params = node.get("ros__parameters")
        if not isinstance(params, dict):
            raise ValueError(f"camera node {side} has no ros__parameters mapping")
        params.update(settings)
    return data


def _references_side(value: Any, side: str) -> bool:
    if not isinstance(value, str):
        return False
    markers = (
        f"/{side}_camera/",
        f"/galaxis/gripper/{side}/",
        f"/pico/controller/{side}/",
        f"{side}_hand_",
        f"{side}_left_finger_",
        f"{side}_right_finger_",
    )
    return any(marker in value for marker in markers)


def apply_collection_profile(filename: str, data: Any, profile: dict[str, Any]) -> Any:
    """Prune merged portable configuration to the selected hardware contract."""
    if not isinstance(data, dict):
        return data
    inactive_arms = {"left", "right"} - set(profile["active_arms"])
    inactive_cameras = {"left", "right", "center"} - set(profile["active_cameras"])

    if filename == "galaxis_devices.yaml":
        result = dict(data)
        for side in inactive_cameras:
            result.pop(f"{side}_camera/usb_cam", None)
            result.pop(f"{side}_camera_brightness_controller", None)
        if "center" in inactive_cameras:
            result.pop("center_camera/g2_bridge", None)
            result.pop("center_camera_flip_node", None)
        elif profile["camera_sources"]["center"] == "g2_head_color":
            result.pop("center_camera/usb_cam", None)
            result.pop("center_camera_brightness_controller", None)
        else:
            result.pop("center_camera/g2_bridge", None)
        flip = result.get("center_camera_flip_node", {}).get("ros__parameters")
        if isinstance(flip, dict):
            flip['input_transport'] = ('raw' if profile['camera_sources']['center'] == 'g2_head_color' else 'compressed')
        encoder = result.get("encoder_node", {}).get("ros__parameters", {})
        encoder["active_sides"] = list(profile["active_arms"])
        encoder["collection_profile"] = profile["collection_profile"]
        # The actual 100 Hz serial stream must not be replayed as fresh data
        # for half a second after it stops (platform gap limit is 100 ms).
        encoder["data_timeout_sec"] = 0.05
        return result

    if filename == "robot_config.yaml":
        params = data.get("/**", {}).get("ros__parameters", {})
        topics = params.get("record_topics")
        if isinstance(topics, list):
            inactive = inactive_arms | inactive_cameras
            params["record_topics"] = [
                topic
                for topic in topics
                if not any(_references_side(topic, side) for side in inactive)
            ]
            if "/collection_profile" not in params["record_topics"]:
                params["record_topics"].append("/collection_profile")
        return data

    if filename == "recording_gate.yaml":
        # Older installed unified configs contain a 90 s warning / 120 s
        # hard stop. Remove that legacy policy on every generation as well,
        # so upgrades retaining saved settings do not restore the time cap.
        data.setdefault("groups", {}).pop("duration_limits", None)
        # Platform UMI screening rejects source timestamp gaps > 100 ms.
        # Warn at 200 ms; end only after 1 s without delivery. Offline validation
        # isolates real gaps > 100 ms before preparing training data.
        health = data['groups'].setdefault('health', {}).setdefault('conditions', {})
        for side in profile['active_arms']:
            for suffix, timeout, level in [('warn', 0.2, 1), ('stop', 1.0, 2)]:
                rule = dict(topic=f'/galaxis/gripper/{side}/joint_state',
                    expected_type='sensor_msgs/msg/JointState', qos_depth=100,
                    timeout_sec=timeout, escalation=level, debounce_sec=0.0)
                if suffix == 'stop':
                    rule['source_max_gap_sec'] = 0.2
                    rule['source_gap_escalation'] = 1
                health[f'{side}_gripper_joint_state_timeout_{suffix}'] = rule
        data.setdefault("groups", {})["pico_timing"] = dict(
            default_type="topic_condition", default_escalation=1,
            settle_sec=0.0, recovery_sec=0.5,
            conditions={f"{side}_source_clock": dict(
                topic=f"/pico/controller/{side}/state",
                expected_type="galaxis_pico_msgs/msg/PicoController",
                best_effort=True, qos_depth=100, pico_timing=True, timeout_sec=1.0, escalation=1,
                debounce_sec=0.0) for side in profile["active_arms"]})
        data['groups']['sample_continuity'] = dict(
            default_type='topic_condition', recovery_sec=0.5,
            conditions={f'{side}_source_samples_{kind}': dict(
                topic=f'/pico/{"hmd" if side == "hmd" else "controller/"+side}/state',
                expected_type='galaxis_pico_msgs/msg/'+('PicoHmd' if side == 'hmd' else 'PicoController'),
                best_effort=True, qos_depth=100, timeout_sec=timeout,
                source_max_gap_sec=0.2, source_gap_escalation=1,
                require_tracked_pose=side != "hmd", escalation=level, debounce_sec=0.0)
                for side in ['hmd', *profile['active_arms']]
                for kind, timeout, level in [('warn', 0.2, 1), ('stop', 1.0, 2)]})
        quality = profile.get('camera_quality')
        if quality and quality.get('version', 1) < 2:
            quality = dict(version=2, min_rate_hz=27.0, rate_window_sec=5.0,
                                    warn_frame_gap_sec=0.5, stop_min_rate_hz=20.0,
                                    stop_low_rate_sec=10.0, max_frame_gap_sec=3.0, warmup_sec=1.0)
        if quality:
            conditions = {}
            for side in profile['active_cameras']:
                mode = profile['cameras'][side]
                conditions[f'{side}_image_30fps'] = dict(
                    topic=f'/{side}_camera/image_raw/compressed',
                    expected_type='sensor_msgs/msg/CompressedImage',
                    best_effort=True, qos_depth=30,
                    timeout_sec=0.2, escalation=1,
                    min_rate_hz=quality['min_rate_hz'], rate_escalation=0,
                    rate_window_sec=quality['rate_window_sec'],
                    max_frame_gap_sec=quality['warn_frame_gap_sec'], debounce_sec=0.0)
                conditions[f'{side}_image_sustained_failure'] = dict(
                    topic=f'/{side}_camera/image_raw/compressed',
                    expected_type='sensor_msgs/msg/CompressedImage', best_effort=True, qos_depth=30,
                    timeout_sec=1.0, source_max_gap_sec=0.2, source_gap_escalation=1, escalation=2,
                    min_rate_hz=quality['stop_min_rate_hz'], rate_escalation=2,
                    rate_window_sec=quality['rate_window_sec'],
                    low_rate_hold_sec=quality['stop_low_rate_sec'],
                    max_frame_gap_sec=quality['max_frame_gap_sec'], debounce_sec=0.0)
                conditions[f'{side}_image_size'] = dict(
                    topic=f'/{side}_camera/camera_info', timeout_sec=quality['max_frame_gap_sec'], escalation=2,
                    camera_selection_role=side,
                    condition=f"msg.width == {mode['image_width']} and msg.height == {mode['image_height']}",
                    reason=f'{side} 相机实际分辨率与所选配置不一致', debounce_sec=0.0)
            data.setdefault('groups', {})['camera_quality'] = dict(
                default_type='topic_condition', recovery_sec=quality['warmup_sec'], conditions=conditions)
        for group_name, group in data.get("groups", {}).items():
            conditions = group.get("conditions") if isinstance(group, dict) else None
            if not isinstance(conditions, dict):
                continue
            for name, condition in list(conditions.items()):
                if not isinstance(condition, dict):
                    continue
                topic = condition.get("topic")
                if quality and group_name != "camera_quality" and isinstance(topic, str) and any(
                        topic.startswith(f"/{side}_camera/") for side in ("left", "center", "right")):
                    # The camera policy owns freshness/rate; legacy camera_info
                    # warnings must not independently block a healthy stream.
                    del conditions[name]
                    continue
                if any(_references_side(topic, side) for side in inactive_arms | inactive_cameras):
                    del conditions[name]
                    continue
                frames = condition.get("frames")
                if isinstance(frames, list):
                    condition["frames"] = [
                        frame
                        for frame in frames
                        if not any(
                            _references_side(frame.get("target"), side)
                            for side in inactive_arms
                            if isinstance(frame, dict)
                        )
                    ]
                    if not condition["frames"]:
                        del conditions[name]
        return data

    return data


def build_from_single(
    single_path: Path,
    out_dir: Path,
    profile_file: Path | None = None,
) -> list[Path]:
    """从唯一配置文件(含 robot_config/galaxis_devices/recording_gate/upload_targets
    四个段落)直接生成运行时文件,仍会叠加设备设置 profile 的裁剪与相机模式覆盖。"""
    doc = load_yaml(single_path)
    if not isinstance(doc, dict):
        raise ValueError(f"single config must be a mapping: {single_path}")

    profile = load_collection_profile(profile_file)
    camera_overrides = load_camera_overrides(profile_file)
    written: list[Path] = []
    for filename in MERGED_FILES:
        section = filename[: -len(".yaml")] if filename.endswith(".yaml") else filename
        data = doc.get(section)
        if data is None:
            raise ValueError(
                f"single config missing section {section!r}: {single_path}"
            )
        path = out_dir / filename
        out_dir.mkdir(parents=True, exist_ok=True)
        with path.open("w") as output:
            yaml.safe_dump(data, output, sort_keys=False, default_flow_style=False)
        transformed = apply_collection_profile(filename, data, profile)
        with path.open("w") as output:
            yaml.safe_dump(transformed, output, sort_keys=False, default_flow_style=False)
        if filename == "galaxis_devices.yaml":
            transformed = apply_camera_overrides(transformed, camera_overrides)
            with path.open("w") as output:
                yaml.safe_dump(
                    transformed, output, sort_keys=False, default_flow_style=False
                )
        written.append(path)
    profile_path = out_dir / "collection_profile.yaml"
    with profile_path.open("w") as output:
        yaml.safe_dump(profile, output, sort_keys=False, default_flow_style=False)
    written.append(profile_path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--single-config",
        type=Path,
        required=True,
        help="Path to the single unified config file (4 sections).",
    )
    parser.add_argument(
        "--profile-file",
        type=Path,
        default=Path("/etc/galaxis/collection_profile.yaml"),
        help="Setup profile YAML (missing file defaults to dual-arm portable).",
    )
    parser.add_argument(
        "--config-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "config",
        help="Path to galaxis_bringup/config (default: relative to this script).",
    )
    args = parser.parse_args(argv)

    written = build_from_single(
        single_path=args.single_config,
        out_dir=args.config_root / "_runtime" / "single",
        profile_file=args.profile_file,
    )
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
