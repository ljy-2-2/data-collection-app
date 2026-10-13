"""Collection configuration: which sensors to pull, budgets, output layout.

Everything the collector touches on the robot goes through agibot_gdk; this
config only selects among GDK-reachable sources:
  - bundle tensors (proprio state + any teleop__action__* keys), ~30 Hz
  - cameras by CameraType (fetched at each bundle's declared timestamp)
  - IMUs, lidars, ultrasonic radar (nearest-sample per bundle frame)
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import yaml


@dataclasses.dataclass
class CameraCfg:
    name: str                       # snake_case, e.g. "head_color" -> CameraType.kHeadColor
    essential: bool = False         # essential + missing => frame rejected (not just marked)
    staleness_budget_ms: float = 50.0
    retries: int = 2                # extra get_nearest_image attempts for exact ts match
    retry_sleep_ms: float = 4.0


@dataclasses.dataclass
class BundleCfg:
    record_all: bool = True         # record every bundle_info tensor
    keys: list[str] = dataclasses.field(default_factory=list)  # used when record_all=False
    essential_keys: list[str] = dataclasses.field(
        default_factory=lambda: ["state__joint__position"]
    )                               # missing any of these => frame rejected


@dataclasses.dataclass
class ImuCfg:
    name: str                       # front | back | chassis
    staleness_budget_ms: float = 20.0


@dataclasses.dataclass
class LidarCfg:
    name: str                       # front | back
    every_n_frames: int = 1         # subsample: fetch every Nth bundle frame


@dataclasses.dataclass
class CollectorCfg:
    robot_type: str = "agibot_g2"
    output_root: str = "~/agibot_episodes"
    poll_hz: float = 100.0          # bundle poll rate; pace by arrival, never sleep(1/30)
    fetch_workers: int = 2          # camera fetch threads; cameras are sharded across them
    queue_depth: int = 8            # per-worker job queue; full queue => drop + count
    clock_offset_hz: float = 1.0    # robot-ts vs PC-ts pair logging rate
    jpeg_passthrough: bool = True   # write JPEG payload bytes verbatim (no re-encode)
    depth_png: bool = True          # write Z16/GRAY16 depth as 16-bit PNG
    bundle: BundleCfg = dataclasses.field(default_factory=BundleCfg)
    cameras: list[CameraCfg] = dataclasses.field(default_factory=list)
    imus: list[ImuCfg] = dataclasses.field(default_factory=list)
    lidars: list[LidarCfg] = dataclasses.field(default_factory=list)
    ultrasonic: bool = False

    @property
    def output_root_path(self) -> Path:
        return Path(self.output_root).expanduser()


_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


def load_config(path: str | Path) -> CollectorCfg:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    cfg = CollectorCfg()
    for key in ("robot_type", "output_root", "poll_hz", "fetch_workers", "queue_depth",
                "clock_offset_hz", "jpeg_passthrough", "depth_png", "ultrasonic"):
        if key in raw:
            setattr(cfg, key, raw[key])

    b = raw.get("bundle", {}) or {}
    cfg.bundle = BundleCfg(
        record_all=b.get("record_all", True),
        keys=list(b.get("keys", [])),
        essential_keys=list(b.get("essential_keys", ["state__joint__position"])),
    )

    cams = []
    for c in raw.get("cameras", []) or []:
        if isinstance(c, str):
            c = {"name": c}
        cam = CameraCfg(name=c["name"])
        for k in ("essential", "staleness_budget_ms", "retries", "retry_sleep_ms"):
            if k in c:
                setattr(cam, k, c[k])
        if not _SNAKE.match(cam.name):
            raise ValueError(f"camera name must be snake_case (e.g. head_color): {cam.name!r}")
        cams.append(cam)
    cfg.cameras = cams

    cfg.imus = [ImuCfg(**i) if isinstance(i, dict) else ImuCfg(name=i)
                for i in (raw.get("imus", []) or [])]
    cfg.lidars = [LidarCfg(**l) if isinstance(l, dict) else LidarCfg(name=l)
                  for l in (raw.get("lidars", []) or [])]
    return cfg


def config_as_dict(cfg: CollectorCfg) -> dict:
    return dataclasses.asdict(cfg)
