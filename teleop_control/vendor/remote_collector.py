#!/usr/bin/env python3
"""Button-controlled wrapper around the bundled gdk_data_collection project."""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import socket
import sys
import threading
import time
from pathlib import Path

RIGHT_ARM_JOINTS = [
    "idx61_arm_r_joint1",
    "idx62_arm_r_joint2",
    "idx63_arm_r_joint3",
    "idx64_arm_r_joint4",
    "idx65_arm_r_joint5",
    "idx66_arm_r_joint6",
    "idx67_arm_r_joint7",
]
RIGHT_GRIPPER = "idx71_gripper_r_inner_joint1"
READY_MARKER = ".data-platform-ready.json"


def collection_available() -> bool:
    filename = os.environ.get("DATA_COLLECTION_GATE_FILE")
    if not filename:
        return True
    try:
        with open(filename) as handle:
            value = json.load(handle)
        return value.get("available") is True and 0 <= time.time() - value["checked_at"] < 10
    except (OSError, ValueError, KeyError, TypeError):
        return False


def collection_context() -> dict:
    filename = os.environ.get('DATA_COLLECTION_GATE_FILE')
    if not filename:
        return {}
    try:
        value = json.loads(Path(filename).read_text())
        if value.get('available') is not True or not 0 <= time.time()-value['checked_at'] < 10:
            raise ValueError('platform lease expired')
        return {'author': value['username'],
                **({'upload_destination': value['upload_destination']} if 'upload_destination' in value else {})}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError('无法确认采集平台和账号，请检查所选平台登录状态') from exc


def mark_episode_ready(
    episode_dir: Path,
    *,
    session_id: str,
    session_folder: str,
    requested_cameras: list[str] | None = None,
    platform_context: dict | None = None,
) -> None:
    marker = episode_dir / READY_MARKER
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "ready_at_ns": time.time_ns(),
        "import_task_id": f"teleop-session-{session_id}",
        "import_folder": session_folder,
        "teleop_session_id": session_id,
        "requested_cameras": requested_cameras or [],
        **(platform_context or {}),
    }))
    os.replace(temporary, marker)


class ControlServer:
    def __init__(self, path: Path, *, recording_status: Path | None = None):
        self.path = path
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(1)
        self.starts: queue.Queue[dict] = queue.Queue()
        self.stop_event = threading.Event()
        self.shutdown_event = threading.Event()
        self._lock = threading.Lock()
        self._recording = False
        self.recording_status = recording_status
        self._start_pending = False
        self._actions: list[dict] = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._publish_recording(False)
        self._thread.start()

    def _publish_recording(self, recording: bool):
        if self.recording_status is None:
            return
        self.recording_status.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.recording_status.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "recording": recording,
            "updated_at_ns": time.time_ns(),
        }, separators=(",", ":")))
        os.replace(temporary, self.recording_status)

    def _run(self):
        try:
            conn, _ = self.sock.accept()
            with conn, conn.makefile("r") as f:
                for line in f:
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = msg.get("type")
                    if kind == "start":
                        if not collection_available():
                            continue
                        with self._lock:
                            # Once B has requested the current episode to stop,
                            # allow one A press to queue the next episode while
                            # camera files are still being finalized.
                            active = (
                                self._start_pending
                                or (self._recording and not self.stop_event.is_set())
                            )
                            if not active:
                                self._start_pending = True
                                self._publish_recording(True)
                        if not active:
                            self.starts.put(msg)
                    elif kind == "stop":
                        self._publish_recording(False)
                        self.stop_event.set()
                    elif kind == "action":
                        with self._lock:
                            if self._recording:
                                self._actions.append(msg)
                    elif kind == "shutdown":
                        self._publish_recording(False)
                        self.shutdown_event.set()
                        self.stop_event.set()
                        break
        finally:
            self._publish_recording(False)
            self.shutdown_event.set()
            self.stop_event.set()

    def begin_episode(self):
        with self._lock:
            self._start_pending = False
            self._recording = True
            self._actions = []
        self.stop_event.clear()

    def finish_episode(self) -> list[dict]:
        with self._lock:
            self._recording = False
            rows = self._actions
            self._actions = []
        return rows

    def close(self):
        self._publish_recording(False)
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _clock_pairs(ep_dir: Path) -> list[tuple[int, int]]:
    pairs = []
    path = ep_dir / "record" / "clock_offset.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                pairs.append((int(row["pc_ts_ns"]), int(row["robot_ts_ns"])))
    if pairs:
        return pairs
    # Short episodes can end before the 1 Hz clock logger fires.  Bundle rows
    # carry the same robot/PC receive-time pair and are a valid fallback.
    path = ep_dir / "record" / "bundles.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                pairs.append((int(row["pc_recv_ns"]), int(row["timestamp_ns"])))
    return pairs


def write_actions(ep_dir: Path, actions: list[dict]) -> int:
    pairs = _clock_pairs(ep_dir)
    if not actions or not pairs:
        return 0
    pairs.sort()
    out = ep_dir / "record" / "actions.jsonl"
    names = actions[0].get("names", [])
    with out.open("w") as f:
        for row in actions:
            pc_ns = int(row["pc_ts_ns"])
            pair_pc, pair_robot = min(pairs, key=lambda p: abs(p[0] - pc_ns))
            robot_ns = pc_ns + (pair_robot - pair_pc)
            f.write(json.dumps({
                "ts_ns": robot_ns,
                "pc_ts_ns": pc_ns,
                "names": names,
                "action": row["action"],
            }, separators=(",", ":")) + "\n")
    (ep_dir / "record" / "actions_meta.json").write_text(json.dumps({
        "clock_mapping": "nearest robot-PC clock-offset sample",
        "names": names,
        "count": len(actions),
    }, indent=2))
    return len(actions)


def keep_right_arm_state_only(ep_dir: Path) -> int:
    """Trim raw bundle tensors to right arm 7 + right gripper 1.

    Camera timestamps and non-state alignment fields remain intact. This makes
    the stock converter produce an 8-D observation when invoked with
    state__joint__position,state__right_effector__position.
    """
    path = ep_dir / "record" / "bundles.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"episode has no bundle log: {path}")
    tmp = path.with_suffix(".jsonl.right-only.tmp")
    count = 0
    with path.open() as src, tmp.open("w") as dst:
        for line_no, line in enumerate(src, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            tensors = row.get("tensors", {})
            names = tensors.get("state__joint__name")
            positions = tensors.get("state__joint__position")
            if not isinstance(names, list) or not isinstance(positions, list):
                raise ValueError(
                    f"{path}:{line_no}: missing joint names/positions"
                )
            by_name = dict(zip((str(name) for name in names), positions))
            missing = [name for name in RIGHT_ARM_JOINTS if name not in by_name]
            if missing:
                raise ValueError(
                    f"{path}:{line_no}: missing right-arm joints {missing}"
                )
            tensors["state__joint__name"] = list(RIGHT_ARM_JOINTS)
            tensors["state__joint__position"] = [
                by_name[name] for name in RIGHT_ARM_JOINTS
            ]
            for key in list(tensors):
                if key.startswith("state__") and key not in {
                    "state__joint__name",
                    "state__joint__position",
                    "state__right_effector__name",
                    "state__right_effector__position",
                }:
                    del tensors[key]
            right_names = tensors.get("state__right_effector__name")
            right_positions = tensors.get("state__right_effector__position")
            if (
                isinstance(right_names, list)
                and isinstance(right_positions, list)
                and RIGHT_GRIPPER in [str(name) for name in right_names]
            ):
                right_by_name = dict(zip(
                    (str(name) for name in right_names), right_positions
                ))
                tensors["state__right_effector__name"] = [RIGHT_GRIPPER]
                tensors["state__right_effector__position"] = [
                    right_by_name[RIGHT_GRIPPER]
                ]
            else:
                raise ValueError(
                    f"{path}:{line_no}: missing right gripper {RIGHT_GRIPPER}"
                )
            dst.write(json.dumps(row, separators=(",", ":")) + "\n")
            count += 1
    os.replace(tmp, path)
    (ep_dir / "record" / "right_only_meta.json").write_text(json.dumps({
        "state_names": RIGHT_ARM_JOINTS + [RIGHT_GRIPPER],
        "state_dim": 8,
        "action_names": RIGHT_ARM_JOINTS + [RIGHT_GRIPPER],
        "action_dim": 8,
    }, indent=2))
    return count


def standardize_color_images(
    ep_dir: Path,
    camera_names: list[str],
    width: int,
    height: int,
) -> dict[str, dict]:
    """Center-crop without distortion, then resize all color views equally."""
    import cv2

    dst_ratio = width / height
    report = {}
    for camera in camera_names:
        frames_dir = ep_dir / "camera" / camera / "frames"
        for temporary in frames_dir.glob("*.resized.jpg"):
            temporary.unlink(missing_ok=True)
        paths = sorted(frames_dir.glob("*.jpg"))
        if not paths:
            raise FileNotFoundError(f"{camera}: no JPEG frames in {frames_dir}")
        source_shapes = set()
        for path in paths:
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f"cannot decode camera frame {path}")
            src_h, src_w = image.shape[:2]
            source_shapes.add((src_h, src_w))
            src_ratio = src_w / src_h
            if src_ratio > dst_ratio:
                crop_w = max(1, round(src_h * dst_ratio))
                x0 = (src_w - crop_w) // 2
                image = image[:, x0:x0 + crop_w]
            elif src_ratio < dst_ratio:
                crop_h = max(1, round(src_w / dst_ratio))
                y0 = (src_h - crop_h) // 2
                image = image[y0:y0 + crop_h, :]
            interpolation = (
                cv2.INTER_AREA
                if image.shape[0] >= height and image.shape[1] >= width
                else cv2.INTER_LINEAR
            )
            image = cv2.resize(image, (width, height), interpolation=interpolation)
            tmp = path.with_name(path.stem + ".resized.jpg")
            if not cv2.imwrite(
                str(tmp), image, [cv2.IMWRITE_JPEG_QUALITY, 95]
            ):
                raise OSError(f"failed to write resized camera frame {tmp}")
            os.replace(tmp, path)
        report[camera] = {
            "frames": len(paths),
            "source_shapes_h_w": [list(shape) for shape in sorted(source_shapes)],
            "output_shape_h_w": [height, width],
            "fit": "center-crop",
            "jpeg_quality": 95,
        }
    (ep_dir / "record" / "image_preprocessing.json").write_text(
        json.dumps(report, indent=2)
    )
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--session-folder", required=True)
    ap.add_argument("--out", default="~/agibot_episodes")
    ap.add_argument("--config", required=True)
    ap.add_argument("--collection-project", required=True)
    ap.add_argument("--right-only", action="store_true",
                    help="trim saved state to right arm 7 + right gripper 1")
    ap.add_argument("--image-width", type=int, default=640)
    ap.add_argument("--image-height", type=int, default=480)
    ap.add_argument("--recording-status", type=Path, default=None)
    args = ap.parse_args()
    if args.image_width <= 0 or args.image_height <= 0:
        ap.error("--image-width and --image-height must be positive")

    sys.path.insert(0, str(Path(args.collection_project).expanduser()))
    from collector.config import config_as_dict, load_config
    from collector.collector import Collector
    from collector.episode_writer import EpisodeWriter

    cfg = load_config(args.config)
    cfg.output_root = args.out
    out_root = cfg.output_root_path
    out_root.mkdir(parents=True, exist_ok=True)

    server = ControlServer(Path(args.socket), recording_status=args.recording_status)

    def request_shutdown(_signum, _frame):
        # The process shares a group with the teleop runner. Ending teleop must
        # stop new capture but must not raise KeyboardInterrupt in the middle of
        # image normalization, otherwise the ready marker is never written.
        server.shutdown_event.set()
        server.stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    print(f"[dataset] IPC ready: {args.socket}", flush=True)
    print("[dataset] initializing GDK/cameras (about 3 seconds)...", flush=True)
    collector = Collector(cfg)
    print(f"[dataset] READY; A=start, B=stop+save; output={out_root}", flush=True)

    try:
        while not server.shutdown_event.is_set():
            try:
                server.starts.get(timeout=0.2)
            except queue.Empty:
                continue
            if server.shutdown_event.is_set():
                break
            if not collection_available():
                server.begin_episode()
                server.finish_episode()
                server._publish_recording(False)
                continue
            platform_context = collection_context()
            writer = EpisodeWriter(out_root, cfg.cameras, cfg.jpeg_passthrough,
                                   cfg.depth_png, args.task, config_as_dict(cfg))
            server.begin_episode()
            print(f"[dataset] RECORDING {writer.episode_id}", flush=True)
            collector.run_episode(writer, None, server.stop_event)
            actions = server.finish_episode()
            count = write_actions(writer.dir, actions)
            frames = keep_right_arm_state_only(writer.dir) if args.right_only else None
            mark_episode_ready(
                writer.dir,
                session_id=args.session_id,
                session_folder=args.session_folder,
                requested_cameras=[camera.name for camera in cfg.cameras if not camera.essential],
                platform_context=platform_context,
            )
            suffix = (
                f", {frames} right-only state frames"
                if frames is not None else ""
            )
            print(
                f"[dataset] SAVED {writer.dir} "
                f"({count} dual-arm command actions{suffix}; "
                "image processing handed to import queue)",
                flush=True,
            )
    finally:
        collector.close()
        server.close()
        print("[dataset] collector stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
