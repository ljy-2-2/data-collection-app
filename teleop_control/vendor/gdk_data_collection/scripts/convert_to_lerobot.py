#!/usr/bin/env python3
"""Convert raw GDK-collected episodes into a LeRobot v3.0 dataset.

Run inside the *lerobot* environment (NOT the GDK env — no agibot_gdk needed):

    ~/miniconda3/envs/lerobot/bin/python scripts/convert_to_lerobot.py \
        --episodes ~/agibot_episodes \
        --repo-id <user>/agibot_g2_task \
        --root ~/lerobot_datasets/agibot_g2_task

Frame join rule (stock FilterMessagesWithImages, offline recheck): a bundle
row becomes a dataset frame only if EVERY selected camera stored a frame
whose capture ts exactly equals the bundle's declared timestamp for it.

Action sources, in --action-source auto order:
  1. record/actions.jsonl   — our own teleop command log, rows
                              {"ts_ns": <robot-clock ns>, "action": [floats]}
                              resampled to each frame by nearest |dt|
  2. teleop__action__* keys — stock teleop intent embedded in the bundle
  3. state_backfill         — action[t] = state[t+1] (last frame repeated),
                              recorded explicitly in meta as action_backfilled
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np

DEFAULT_STATE_KEYS = [
    "state__joint__position",
    "state__left_effector__position",
    "state__right_effector__position",
    "state__head__position",
    "state__waist__position",
]


def load_episode_raw(ep_dir: Path) -> dict:
    meta = json.loads((ep_dir / "meta_info.json").read_text())
    rows = []
    with open(ep_dir / "record" / "bundles.jsonl") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    cam_index: dict[str, dict[int, int]] = {}
    for cam_dir in sorted((ep_dir / "camera").glob("*")) if (ep_dir / "camera").exists() else []:
        idx_file = cam_dir / f"{cam_dir.name}.txt"
        if not idx_file.exists():
            continue
        idx = {}
        for line in idx_file.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                idx[int(parts[0])] = int(parts[1])
        cam_index[cam_dir.name] = idx
    actions_log = []
    af = ep_dir / "record" / "actions.jsonl"
    if af.exists():
        with open(af) as f:
            for line in f:
                line = line.strip()
                if line:
                    actions_log.append(json.loads(line))
    return {"dir": ep_dir, "meta": meta, "rows": rows, "cam_index": cam_index,
            "actions_log": actions_log}


def state_vector(row: dict, state_keys: list[str]) -> np.ndarray:
    parts = []
    for k in state_keys:
        v = row["tensors"].get(k)
        if v is None:
            raise KeyError(f"bundle row missing state key {k!r}")
        parts.append(np.asarray(v, dtype=np.float32).ravel())
    return np.concatenate(parts)


def state_names(row: dict, state_keys: list[str]) -> list[str]:
    names = []
    for k in state_keys:
        vals = np.asarray(row["tensors"][k]).ravel()
        name_key = k.rsplit("__", 1)[0] + "__name"
        labels = row["tensors"].get(name_key)
        if isinstance(labels, list) and len(labels) == len(vals):
            group = k.split("__")[1]
            field = k.rsplit("__", 1)[1]
            names += [f"{group}.{lbl}.{field}" for lbl in labels]
        else:
            base = k.replace("state__", "").replace("__", ".")
            names += [f"{base}.{i}" for i in range(len(vals))]
    return names


def teleop_action_keys(row: dict) -> list[str]:
    return sorted(k for k in row["tensors"] if k.startswith("teleop__action__"))


def pick_action_source(source: str, raw: dict) -> str:
    if source != "auto":
        return source
    if raw["actions_log"]:
        return "actions_file"
    if raw["rows"] and teleop_action_keys(raw["rows"][0]):
        return "teleop"
    return "state_backfill"


def load_image(ep_dir: Path, cam: str, frame_no: int):
    import cv2
    base = ep_dir / "camera" / cam / "frames"
    for ext in (".jpg", ".png"):
        p = base / f"{frame_no:08d}{ext}"
        if p.exists():
            img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
            if img is None:
                return None
            if img.ndim == 2:  # depth/gray — not usable as a video feature
                return img
            return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episodes", required=True,
                    help="root dir containing raw episode dirs, or one episode dir")
    ap.add_argument("--repo-id", required=True, help="e.g. chloeya/agibot_g2_pick")
    ap.add_argument("--root", default=None,
                    help="output dataset dir (default: HF cache under ~/.cache/huggingface)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--cameras", default=None,
                    help="comma list; default: all recorded color cameras")
    ap.add_argument("--state-keys", default=",".join(DEFAULT_STATE_KEYS))
    ap.add_argument("--action-source", default="auto",
                    choices=["auto", "actions_file", "teleop", "state_backfill"])
    ap.add_argument("--robot-timestamps", action="store_true",
                    help="use real robot-clock relative times instead of frame_index/fps")
    ap.add_argument("--task", default=None, help="override task label from meta_info.json")
    ap.add_argument("--vcodec", default="libsvtav1")
    ap.add_argument("--overwrite", action="store_true",
                    help="delete an existing dataset at --root first")
    args = ap.parse_args()

    src = Path(args.episodes).expanduser()
    if (src / "meta_info.json").exists():
        ep_dirs = [src]
    else:
        ep_dirs = sorted(d for d in src.iterdir()
                         if d.is_dir() and (d / "meta_info.json").exists())
    if not ep_dirs:
        sys.exit(f"no episodes found under {src}")
    print(f"found {len(ep_dirs)} raw episode(s)")

    root = Path(args.root).expanduser() if args.root else None
    if root and root.exists():
        if args.overwrite:
            shutil.rmtree(root)
        else:
            sys.exit(f"{root} exists — pass --overwrite to replace it")

    state_keys = [k for k in args.state_keys.split(",") if k]

    # Build features from the first usable episode.
    first = load_episode_raw(ep_dirs[0])
    if not first["rows"]:
        sys.exit(f"{ep_dirs[0]} has no bundle rows")
    row0 = first["rows"][0]
    if args.cameras:
        cams = [c for c in args.cameras.split(",") if c]
    else:
        cams = [c for c, info in first["meta"]["cameras"].items()
                if info["frames"] > 0 and info.get("encoding") in ("jpeg", "png")]
        cams = sorted(cams)
    if not cams:
        sys.exit("no color cameras to convert (depth-only raw streams are skipped)")

    svec0 = state_vector(row0, state_keys)
    snames = state_names(row0, state_keys)
    action_source = pick_action_source(args.action_source, first)
    if action_source == "teleop":
        akeys = teleop_action_keys(row0)
        avec0 = np.concatenate([np.asarray(row0["tensors"][k], np.float32).ravel()
                                for k in akeys])
        anames = [k.replace("teleop__action__", "").replace("__", ".") + f".{i}"
                  for k in akeys
                  for i in range(np.asarray(row0["tensors"][k]).ravel().size)]
    elif action_source == "actions_file":
        avec0 = np.asarray(first["actions_log"][0]["action"], np.float32)
        recorded_names = first["actions_log"][0].get("names")
        anames = (
            [f"cmd.{name}" for name in recorded_names]
            if isinstance(recorded_names, list) and len(recorded_names) == avec0.size
            else [f"cmd.{i}" for i in range(avec0.size)]
        )
    else:
        avec0, anames = svec0, snames
    print(f"state dim {svec0.size}, action dim {avec0.size}, "
          f"action source: {action_source}, cameras: {cams}")

    import cv2  # noqa: F401 — fail early if missing
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    features = {
        "observation.state": {"dtype": "float32", "shape": (svec0.size,), "names": snames},
        "action": {"dtype": "float32", "shape": (avec0.size,), "names": anames},
        "robot_timestamp_ns": {"dtype": "int64", "shape": (1,),
                               "names": ["robot_clock_ns"]},
    }
    for cam in cams:
        img = load_image(first["dir"], cam, 0)
        if img is None or img.ndim != 3:
            sys.exit(f"camera {cam}: no decodable RGB frame 0 (depth cams can't be video)")
        features[f"observation.images.{cam}"] = {
            "dtype": "video", "shape": img.shape,
            "names": ["height", "width", "channels"],
        }

    dataset = LeRobotDataset.create(
        repo_id=args.repo_id, fps=args.fps, features=features,
        root=root, robot_type=first["meta"].get("robot_type", "agibot_g2"),
        use_videos=True, vcodec=args.vcodec,
    )

    sidecar = {"action_source": action_source, "state_keys": state_keys,
               "cameras": cams, "episodes": {}}

    for ep_i, ep_dir in enumerate(ep_dirs):
        raw = first if ep_dir == ep_dirs[0] else load_episode_raw(ep_dir)
        rows = raw["rows"]
        if not rows:
            print(f"  skip {ep_dir.name}: empty")
            continue
        task = args.task or raw["meta"].get("task") or "unknown task"

        # Complete frames only: every selected camera has the exact declared ts.
        joined = []
        dropped = 0
        for row in rows:
            hit = {}
            for cam in cams:
                declared = row.get("cam_ts", {}).get(cam)
                frame_no = raw["cam_index"].get(cam, {}).get(declared) if declared else None
                if frame_no is None:
                    hit = None
                    break
                hit[cam] = frame_no
            if hit is None:
                dropped += 1
            else:
                joined.append((row, hit))
        if len(joined) < 2:
            print(f"  skip {ep_dir.name}: only {len(joined)} complete frames")
            continue

        states = np.stack([state_vector(r, state_keys) for r, _ in joined])
        if action_source == "teleop":
            akeys = teleop_action_keys(joined[0][0])
            actions = np.stack([
                np.concatenate([np.asarray(r["tensors"][k], np.float32).ravel()
                                for k in akeys]) for r, _ in joined])
        elif action_source == "actions_file":
            log_ts = np.array([a["ts_ns"] for a in raw["actions_log"]], dtype=np.int64)
            log_vals = np.stack([np.asarray(a["action"], np.float32)
                                 for a in raw["actions_log"]])
            frame_ts = np.array([r["timestamp_ns"] for r, _ in joined], dtype=np.int64)
            nearest = np.abs(log_ts[None, :] - frame_ts[:, None]).argmin(axis=1)
            actions = log_vals[nearest]
        else:  # state_backfill: action[t] = state[t+1], explicit and logged
            actions = np.vstack([states[1:], states[-1:]])

        t0 = joined[0][0]["timestamp_ns"]
        for i, (row, hit) in enumerate(joined):
            frame = {
                "observation.state": states[i].astype(np.float32),
                "action": actions[i].astype(np.float32),
                "robot_timestamp_ns": np.array([row["timestamp_ns"]], dtype=np.int64),
                "task": task,
            }
            if args.robot_timestamps:
                frame["timestamp"] = (row["timestamp_ns"] - t0) / 1e9
            for cam in cams:
                img = load_image(ep_dir, cam, hit[cam])
                if img is None:
                    raise RuntimeError(f"{ep_dir.name}: unreadable {cam} frame {hit[cam]}")
                frame[f"observation.images.{cam}"] = img
            dataset.add_frame(frame)
        dataset.save_episode()
        sidecar["episodes"][dataset.meta.total_episodes - 1] = {
            "source_episode": ep_dir.name,
            "frames_used": len(joined),
            "frames_dropped_incomplete": dropped,
            "first_bundle_ts_ns": t0,
            "action_backfilled": state_keys if action_source == "state_backfill" else [],
        }
        print(f"  [{ep_i + 1}/{len(ep_dirs)}] {ep_dir.name}: {len(joined)} frames "
              f"({dropped} dropped incomplete) -> episode {dataset.meta.total_episodes - 1}")

    dataset.finalize()
    out_root = Path(dataset.root)
    with open(out_root / "agibot_conversion.json", "w") as f:
        json.dump(sidecar, f, indent=2)
    print(f"\nLeRobot v3.0 dataset written to {out_root}")
    print(f"  episodes: {dataset.meta.total_episodes}, frames: {dataset.meta.total_frames}")
    print(f"  load with: LeRobotDataset(repo_id={args.repo_id!r}, root={str(out_root)!r})")


if __name__ == "__main__":
    main()
