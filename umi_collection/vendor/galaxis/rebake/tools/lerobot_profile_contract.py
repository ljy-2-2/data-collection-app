"""Pure schema validation shared by the Galaxis LeRobot v3 aggregator."""
from __future__ import annotations

from pathlib import Path


PROFILE_VIDEO_KEYS = {
    "dual": {
        "observation.image.left",
        "observation.image.right",
        "observation.image.center",
    },
    "single-left": {"observation.image.left", "observation.image.center"},
    "single-right": {"observation.image.right", "observation.image.center"},
}
PROFILE_JOINT_NAMES = {
    "dual": ["left_left_finger_joint", "right_right_finger_joint"],
    "single-left": ["left_left_finger_joint"],
    "single-right": ["right_right_finger_joint"],
}


def validate_contract(episodes: list[tuple[Path, dict]], profile: str) -> list[str]:
    expected_features = episodes[0][1].get("features")
    if not isinstance(expected_features, dict):
        raise ValueError("first episode has no feature contract")
    for directory, info in episodes[1:]:
        if info.get("features") != expected_features:
            raise ValueError(f"feature schema mismatch in {directory.name}")
        if info.get("fps") != episodes[0][1].get("fps"):
            raise ValueError(f"fps mismatch in {directory.name}")
    video_keys = [
        key for key, feature in expected_features.items() if feature.get("dtype") == "video"
    ]
    if set(video_keys) != PROFILE_VIDEO_KEYS[profile]:
        raise ValueError(
            f"{profile} video contract mismatch: expected {sorted(PROFILE_VIDEO_KEYS[profile])}, "
            f"got {sorted(video_keys)}"
        )
    for key in ("observation.joint_states", "action.joint_states"):
        feature = expected_features.get(key, {})
        # Older recordings used the right finger as the left hand's active
        # joint. Preserve their truthful labels while accepting the new model.
        expected_names = PROFILE_JOINT_NAMES[profile]
        legacy_names = [name.replace("left_left_finger_joint", "left_right_finger_joint")
                        for name in expected_names]
        if feature.get("names") not in (expected_names, legacy_names):
            raise ValueError(
                f"{key} names mismatch: expected {PROFILE_JOINT_NAMES[profile]}, "
                f"got {feature.get('names')}"
            )
    return video_keys
