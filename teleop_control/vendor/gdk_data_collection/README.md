# gdk_data_collection

Visuomotor-policy data collection for the AgiBot G2 using **only `agibot_gdk`
methods** (robot stays unmodified; everything runs on the companion PC), plus
a converter that turns collected episodes into a **LeRobot v3.0 dataset**.

> 中文使用指南（从零开始到 LeRobot 数据集）见 **[USAGE_zh.md](USAGE_zh.md)**。

Implements the collector core of `~/src/design/IMPL_PLAN_gdk_data_collection.md`
(§1–§4: poll loop, dedupe, fetch workers, frame gate, episode writer, action
labeling) — validator/uploader/lifecycle milestones (M4–M5) are not built yet.

## Quick start

```bash
source env.sh                       # GDK env + $GDK_PY / $LEROBOT_PY

$GDK_PY scripts/probe.py            # dump live bundle schema + camera payloads

# record: fixed duration or interactive Enter-to-start/stop
$GDK_PY scripts/collect.py --task "pick up the red cube" --duration 30 --episodes 5
$GDK_PY scripts/collect.py --task "fold the towel"

# convert everything under ~/agibot_episodes to one LeRobot v3.0 dataset
$LEROBOT_PY scripts/convert_to_lerobot.py \
    --episodes ~/agibot_episodes \
    --repo-id chloeya/agibot_g2_pick \
    --root ~/lerobot_datasets/agibot_g2_pick
```

The two scripts run in **different environments** on purpose: the collector
needs `agibot_gdk` (`gdk` env, py3.10 + numpy/opencv/pyyaml; `agibot_gdk`
itself comes from `~/.cache/agibot/app` via PYTHONPATH), the converter needs `lerobot`
(lerobot env). Never import both stacks in one process (native DDS/gRPC
symbol collision — see project memory).

## Configuring sensors (`config/default.yaml`)

Everything the collector touches goes through GDK handles:

| section     | GDK source                            | notes |
|-------------|---------------------------------------|-------|
| `bundle`    | `Bundle.get_latest_bundle`            | ~30 Hz fused proprio (`state__*`, `teleop__action__*` when stock teleop runs); `record_all: true` records every tensor |
| `cameras`   | `Camera.get_nearest_image`            | per-bundle exact-timestamp fetch; `essential: true` + miss ⇒ frame rejected; non-essential miss only marks that camera |
| `imus`      | `Imu.get_nearest_imu`                 | `front`/`back`/`chassis`, nearest sample per frame → `record/imu.jsonl` |
| `lidars`    | `Lidar.get_nearest_pointcloud`        | `front`/`back`, subsampled via `every_n_frames` → `record/lidar/<name>/*.npz` |
| `ultrasonic`| `UltrasonicRadar.get_nearest_…`       | boolean → `record/ultrasonic.jsonl` |

Camera names are the snake_case names from the bundle's
`timestamp__camera__<cam>` keys and map 1:1 to `CameraType` (`head_color` →
`kHeadColor`). Available on this robot (verified live 2026-07-16):
`hand_left_color, hand_right_color, head_color, head_depth,
head_stereo_left, head_stereo_right`.

## Integrity model (deltas vs the stock pipeline, on purpose)

- **Dedupe/continuity by `timestamp_ns`** — the pybind `BundleData` exposes no
  seq index, only `{bundle_info, timestamp_ns}`.
- **Exact-membership enforced live**: an image counts only if
  `img.timestamp_ns == ` the bundle's declared ts (stock applies this rule
  offline only). Configurable retries cover large frames still in flight.
- **Staleness gate**: `bundle_ts − declared cam ts` over budget rejects the
  frame for essential cameras (stock's tolerance param is dead code and its
  100 ms check is warn-only). Per-source deltas are recorded per frame.
- Every failure path increments a counter; all counters land in
  `meta_info.json`.

Measured on this robot: bundles 30.0 Hz; JPEG payloads (hands 1280×1056
~100–130 KB, stereo 1920×1536 ~320 KB, head_color 640×400 ~29 KB); depth is
uncompressed Z16 512 KB → stored as 16-bit PNG. A 15 s / 4-camera episode is
~173 MB and captures 100% of frames with 0 misses (3 fetch workers).

## Raw episode layout

```
<output_root>/<episode_id>/
  camera/<cam>/<cam>.txt          # "<capture_ts_ns> <frame#>" (stock camera_dlb format)
  camera/<cam>/frames/%08d.jpg    # JPEG bytes verbatim (no re-encode) | .png for Z16 depth
  record/bundles.jsonl            # per frame: seq, ts, pc_recv_ns, cam_ts, cam_delta_ms, all tensors
  record/imu.jsonl                # optional
  record/lidar/<name>/%08d.npz    # optional
  record/ultrasonic.jsonl         # optional
  record/clock_offset.jsonl       # {robot_ts_ns, pc_ts_ns} @1 Hz (PC clock never used for alignment)
  record/actions.jsonl            # OPTIONAL — drop your teleop command log here (see below)
  meta_info.json                  # config, per-camera counts, counters, task label
```

## Action labeling (GDK-only reality)

Through the GDK alone `/hal/joint_cmd` is invisible, so the converter picks
actions in this order (`--action-source auto`):

1. **`record/actions.jsonl`** — our own teleop command log, one row per sent
   command: `{"ts_ns": <robot-clock ns>, "action": [floats]}`. The teleop
   stack (`~/src/agibot-teleop`) should write this into the episode dir; it is
   the superior label (zero-loss, no bus hop). Resampled nearest-|Δt| per frame.
2. **`teleop__action__*` bundle keys** — present only when stock teleop runs.
3. **`state_backfill`** — `action[t] = state[t+1]`, recorded explicitly in
   `agibot_conversion.json` (`action_backfilled`), never silent.

## LeRobot v3.0 output

`scripts/convert_to_lerobot.py` builds the dataset through lerobot's own API
(`LeRobotDataset.create/add_frame/save_episode/finalize`, codebase v3.0):

- `observation.images.<cam>` — video (AV1 by default, `--vcodec libx264` if
  needed) from the recorded JPEGs; depth cameras stay raw-only (not video-able).
- `observation.state` — float32 concat of `--state-keys` (default: 14 arm
  joints + L/R gripper + 3 head + 5 waist = 24 dims) with per-dim names taken
  from the robot's own `state__*__name` strings.
- `action` — same dim layout (see above).
- `robot_timestamp_ns` — int64 robot-clock capture time per frame.
- Frame join = stock completeness rule: a bundle becomes a dataset frame only
  if every selected camera stored the exact declared timestamp; incomplete
  frames are dropped and counted in `agibot_conversion.json`.
- `timestamp` defaults to `frame_index/fps` (safe for lerobot's tolerance
  checks); `--robot-timestamps` keeps real robot-clock relative times.

Validated round-trip: dataset loads with `LeRobotDataset(...)`, videos decode
to float32 CHW tensors, state/action/timestamps correct.

## Known limitations

- Joint staleness can't be gated: the bundle carries no per-source timestamp
  for proprio (only cameras get `timestamp__camera__*`).
- `head_depth` is skipped by the converter (LeRobot video features are 3-ch);
  it stays in the raw episode as 16-bit PNGs.
- Offline validator, SQLite episode lifecycle, and uploader (plan §5–§6) are
  not implemented yet.
