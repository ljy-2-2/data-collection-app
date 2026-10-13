"""Episode directory writer.

Layout (stock-compatible where it matters — <cam>.txt index format is the
same "<capture_ts_ns> <frame#>" used by camera_dlb, so stock tooling that
keys on it still works):

  <output_root>/<episode_id>/
    camera/<cam>/<cam>.txt            # "<capture_ts_ns> <frame#>" per stored frame
    camera/<cam>/frames/%08d.jpg      # JPEG payload bytes verbatim (no re-encode)
    camera/<cam>/frames/%08d.png      # 16-bit PNG for Z16/GRAY16 depth
    record/bundles.jsonl              # per accepted frame: ts, tensors, deltas, cam hits
    record/imu.jsonl                  # per frame: nearest sample per configured IMU
    record/lidar/<name>/%08d.npz      # packed point cloud + ts
    record/ultrasonic.jsonl
    record/clock_offset.jsonl         # {robot_ts_ns, pc_ts_ns} pairs @~1 Hz
    record/actions.jsonl              # NOT written here — drop your teleop command log
                                      # in this path and the converter picks it up
    meta_info.json                    # config, counters, schema, verdict fields

All writes are append-streamed so a crash loses at most the buffered tail.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import agibot_gdk as gdk
import cv2
import numpy as np

from .bundle_codec import to_jsonable

SCHEMA_VERSION = 1


class CameraStreamWriter:
    """One camera: numbered frame files + stock-format .txt timestamp index."""

    def __init__(self, ep_dir: Path, cam: str, jpeg_passthrough: bool,
                 depth_png: bool, live_preview_dir: Path | None = None):
        self.cam = cam
        self._dir = ep_dir / "camera" / cam
        self._frames = self._dir / "frames"
        self._frames.mkdir(parents=True, exist_ok=True)
        self._index = open(self._dir / f"{cam}.txt", "w", buffering=1)
        self._n = 0
        self._jpeg_passthrough = jpeg_passthrough
        self._depth_png = depth_png
        self._live_preview_dir = live_preview_dir
        self.encoding_seen: str | None = None

    def write(self, img) -> None:
        data = np.asarray(img.data, dtype=np.uint8)
        if img.encoding == gdk.Encoding.JPEG and self._jpeg_passthrough:
            path = self._frames / f"{self._n:08d}.jpg"
            jpeg = data.tobytes()
            path.write_bytes(jpeg)
            if self._live_preview_dir is not None:
                preview = self._live_preview_dir / f"{self.cam}.jpg"
                temporary = preview.with_suffix(".jpg.tmp")
                temporary.write_bytes(jpeg)
                os.replace(temporary, preview)
            enc = "jpeg"
        elif img.encoding == gdk.Encoding.PNG:
            path = self._frames / f"{self._n:08d}.png"
            path.write_bytes(data.tobytes())
            enc = "png"
        elif img.encoding == gdk.Encoding.UNCOMPRESSED and img.color_format in (
                gdk.ColorFormat.RS2_FORMAT_Z16, gdk.ColorFormat.GRAY16) and self._depth_png:
            depth = data.view(np.uint16).reshape(img.height, img.width)
            path = self._frames / f"{self._n:08d}.png"
            cv2.imwrite(str(path), depth)  # 16-bit PNG, lossless
            enc = "z16_png"
        else:
            # Fallback: raw payload + shape note; converter can deal with it.
            path = self._frames / f"{self._n:08d}.raw"
            path.write_bytes(data.tobytes())
            enc = f"raw:{img.encoding}:{img.color_format}:{img.width}x{img.height}"
        if self.encoding_seen is None:
            self.encoding_seen = enc
        self._index.write(f"{img.timestamp_ns} {self._n}\n")
        self._n += 1

    @property
    def frames_written(self) -> int:
        return self._n

    def close(self):
        self._index.close()


class _Jsonl:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "w")
        self._n = 0

    def write(self, obj: dict):
        self._f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self._n += 1
        if self._n % 30 == 0:
            self._f.flush()

    def close(self):
        self._f.close()


class EpisodeWriter:
    def __init__(self, output_root: Path, cameras: list, jpeg_passthrough: bool,
                 depth_png: bool, task: str, config_dict: dict,
                 live_preview_dir: Path | None = None):
        self.episode_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        self.dir = output_root / self.episode_id
        self.dir.mkdir(parents=True)
        self.task = task
        self._config_dict = config_dict
        self._t_start_pc_ns = time.time_ns()

        if live_preview_dir is not None:
            live_preview_dir.mkdir(parents=True, exist_ok=True)
        self.cams = {c.name: CameraStreamWriter(
                         self.dir, c.name, jpeg_passthrough, depth_png,
                         live_preview_dir
                     )
                     for c in cameras}
        rec = self.dir / "record"
        self.bundles = _Jsonl(rec / "bundles.jsonl")
        self.imu = _Jsonl(rec / "imu.jsonl")
        self.ultrasonic = _Jsonl(rec / "ultrasonic.jsonl")
        self.clock_offset = _Jsonl(rec / "clock_offset.jsonl")
        self._lidar_dir = rec / "lidar"
        self._lidar_n: dict[str, int] = {}
        self.first_bundle_ts: int | None = None
        self.last_bundle_ts: int | None = None
        self.frames_recorded = 0

    def write_bundle_row(self, bundle_ts_ns: int, seq: int, tensors: dict,
                         cam_declared_ts: dict, cam_delta_ms: dict, pc_ts_ns: int):
        # Camera membership is NOT recorded here: each stored frame's exact
        # capture ts lands in <cam>.txt, and the converter joins on
        # cam_declared_ts == indexed ts (stock FilterMessagesWithImages rule).
        if self.first_bundle_ts is None:
            self.first_bundle_ts = bundle_ts_ns
        self.last_bundle_ts = bundle_ts_ns
        self.frames_recorded += 1
        self.bundles.write({
            "seq": seq,
            "timestamp_ns": bundle_ts_ns,
            "pc_recv_ns": pc_ts_ns,
            "cam_ts": {k: int(v) for k, v in cam_declared_ts.items()},
            "cam_delta_ms": cam_delta_ms,
            "tensors": {k: to_jsonable(v) for k, v in tensors.items()},
        })

    def write_lidar(self, name: str, pc) -> None:
        n = self._lidar_n.get(name, 0)
        d = self._lidar_dir / name
        d.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            d / f"{n:08d}.npz",
            timestamp_ns=np.int64(pc.timestamp_ns),
            data=np.asarray(pc.data, dtype=np.uint8),
            width=pc.width, height=pc.height, point_step=pc.point_step,
            row_step=pc.row_step,
            fields=json.dumps([{"name": f.name, "offset": f.offset,
                                "datatype": int(f.datatype), "count": f.count}
                               for f in pc.fields]),
        )
        self._lidar_n[name] = n + 1

    def finalize(self, counters: dict, camera_cfgs: list, notes: dict | None = None):
        for w in self.cams.values():
            w.close()
        for j in (self.bundles, self.imu, self.ultrasonic, self.clock_offset):
            j.close()
        meta = {
            "schema_version": SCHEMA_VERSION,
            "episode_id": self.episode_id,
            "task": self.task,
            "robot_type": self._config_dict.get("robot_type", "agibot_g2"),
            "collector": "gdk_data_collection",
            "pc_start_ns": self._t_start_pc_ns,
            "pc_stop_ns": time.time_ns(),
            "first_bundle_ts_ns": self.first_bundle_ts,
            "last_bundle_ts_ns": self.last_bundle_ts,
            "frames_recorded": self.frames_recorded,
            "cameras": {
                name: {
                    "frames": w.frames_written,
                    "encoding": w.encoding_seen,
                    "essential": next((c.essential for c in camera_cfgs if c.name == name), False),
                }
                for name, w in self.cams.items()
            },
            "counters": counters,
            "config": self._config_dict,
            "action_backfilled": [],   # filled by the converter if it backfills
            "integrity": None,          # room for validator verdicts (stock field names)
            "data_validate": None,
        }
        if notes:
            meta.update(notes)
        with open(self.dir / "meta_info.json", "w") as f:
            json.dump(meta, f, indent=2)
        return meta
