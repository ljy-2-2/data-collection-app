#!/usr/bin/env python3
"""Repair proven mixed-orientation center videos in LeRobot v2.1 trees."""

from __future__ import annotations

import argparse
from pathlib import Path

from aggregate_lerobot_v30 import (
    concatenate_center_video,
    detect_mixed_center_orientations,
)


def repair_tree(root: Path) -> tuple[int, int]:
    checked = 0
    repaired = 0
    for video in sorted(
        root.glob("*/videos/chunk-*/observation.image.center/episode_*.mp4")
    ):
        checked += 1
        if detect_mixed_center_orientations([video]) is None:
            continue
        temporary = video.with_name(f"{video.stem}.repairing.mp4")
        temporary.unlink(missing_ok=True)
        try:
            concatenate_center_video([video], temporary)
            if detect_mixed_center_orientations([temporary]) is not None:
                raise ValueError(f"orientation repair validation failed: {video}")
            temporary.chmod(0o777)
            temporary.replace(video)
            repaired += 1
        finally:
            temporary.unlink(missing_ok=True)
    return checked, repaired


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, type=Path)
    args = parser.parse_args()
    checked, repaired = repair_tree(args.src)
    print(f"center v2.1 videos: checked={checked}, repaired={repaired}")


if __name__ == "__main__":
    main()
