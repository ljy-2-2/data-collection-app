from __future__ import annotations

import json
import logging
import math
import os
import tarfile
import tempfile
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .collection_gate import collection_available
from .robot_names import TIANJI_DISPLAY_NAME
from .platform_auth import (
    current_platform_username,
    platform_api_client,
    platform_session_available,
)
from .tianji_export import CURVE_FILES, project_row, upload_sides

READY_MARKER = ".data-platform-ready.json"
UPLOADED_MARKER = ".data-platform-uploaded.json"
UPLOAD_NAMESPACE = uuid.UUID("52ff6c85-78c0-48b3-bd33-2e5061328824")

logger = logging.getLogger(__name__)


def _iso_from_ns(value: object) -> str | None:
    if not isinstance(value, int) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1_000_000_000, UTC).isoformat().replace("+00:00", "Z")


def _tianji_playback_timing(metadata: dict) -> tuple[float, float | None]:
    """Preview encoding rate must not include startup, audit or disk cleanup.

    The platform currently encodes all cameras at one constant rate. Report the
    negotiated image rate, retaining actual per-frame clocks in the archive.
    This is not a claim of hardware synchronization or exact variable-rate replay.
    """
    rate = metadata.get("requested_image_hz", 30)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
        raise ValueError("天机 Marvin M6 图像帧率无效，不能生成正确的回放时基")
    first, last = metadata.get("first_bundle_ts_ns"), metadata.get("last_bundle_ts_ns")
    duration = None
    if all(isinstance(t, int) and not isinstance(t, bool) for t in (first, last)) and last >= first:
        duration = (last - first) / 1_000_000_000
    return float(rate), duration


def _file_chunks(path: Path, size: int = 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as source:
        while chunk := source.read(size):
            yield chunk


def _write_episode_tar(
    episode_dir: Path,
    archive_path: Path,
    relative_files: list[str],
    curve_sides: tuple[str, ...] | None = None,
) -> None:
    """Archive original files, optionally projecting only uploaded Tianji curves."""
    with tarfile.open(archive_path, mode="w") as archive:
        for relative in relative_files:
            if curve_sides and len(curve_sides) == 1 and relative in CURVE_FILES | {"meta_info.json"}:
                # Spill long recordings to disk rather than retaining all samples.
                with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as projected:
                    with (episode_dir / relative).open(encoding="utf-8") as source:
                        rows = [json.load(source)] if relative == "meta_info.json" else (
                            json.loads(line) for line in source if line.strip()
                        )
                        for row in rows:
                            value = project_row(relative, row, curve_sides)
                            projected.write((json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode())
                    header = archive.gettarinfo(episode_dir / relative, arcname=f"{episode_dir.name}/{relative}")
                    header.size = projected.tell()
                    projected.seek(0)
                    archive.addfile(header, projected)
                continue
            archive.add(
                episode_dir / relative,
                arcname=f"{episode_dir.name}/{relative}",
                recursive=False,
            )


def _camera_size(
    episode_dir: Path,
    camera_name: str,
    camera_metadata: dict,
) -> tuple[int, int]:
    width = camera_metadata.get("width")
    height = camera_metadata.get("height")
    if isinstance(width, int) and width > 0 and isinstance(height, int) and height > 0:
        return width, height

    report_path = episode_dir / "record" / "image_preprocessing.json"
    try:
        report = json.loads(report_path.read_text())
        output_shape = report[camera_name]["output_shape_h_w"]
        if (
            isinstance(output_shape, list)
            and len(output_shape) == 2
            and all(isinstance(value, int) and value > 0 for value in output_shape)
        ):
            return output_shape[1], output_shape[0]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        pass

    frames = episode_dir / "camera" / camera_name / "frames"
    first_frame = next(
        (
            path
            for suffix in ("*.jpg", "*.jpeg", "*.png")
            for path in sorted(frames.glob(suffix))[:1]
        ),
        None,
    )
    if first_frame is not None:
        try:
            import cv2

            image = cv2.imread(str(first_frame), cv2.IMREAD_UNCHANGED)
            if image is not None:
                return int(image.shape[1]), int(image.shape[0])
        except ImportError:
            pass
    return 640, 480


class CollectionUploader:
    """Synchronize controller-finished GDK episode folders into the platform."""

    def __init__(
        self,
        output_root: str | Path,
        api_url: str,
        *,
        interval_seconds: float = 2.0,
        discover_completed: bool = False,
        client: httpx.Client | None = None,
        targets=None,
    ) -> None:
        self.output_root = Path(output_root).expanduser()
        self.api_url = api_url.rstrip("/")
        self.interval_seconds = interval_seconds
        self.discover_completed = discover_completed
        self.client = client or platform_api_client(timeout=None)
        self._owns_client = client is None
        self.targets = targets
        self._base_client = self.client
        self._target_clients = {}
        self._destination = None
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._episode_status: dict[str, dict] = {}

    def _set_status(self, directory, state, error=""):
        with self._status_lock:
            self._episode_status[str(directory)] = {"state": state, "error": error}

    def status(self):
        external = getattr(self, 'external_status_path', None)
        if external is not None:
            try:
                return json.loads(external.read_text())
            except (OSError, ValueError):
                return dict(pending=0, uploaded=0, uploading=0, failed=0, error='')
        with self._status_lock:
            rows = list(self._episode_status.values())
        return {
            "pending": sum(r["state"] in {"pending", "uploading", "failed"} for r in rows),
            "uploaded": sum(r["state"] == "uploaded" for r in rows),
            "uploading": sum(r["state"] == "uploading" for r in rows),
            "failed": sum(r["state"] == "failed" for r in rows),
            "error": next((r["error"] for r in reversed(rows) if r["error"]), "")
            if collection_available() else "",
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="teleop-collection-uploader",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval_seconds + 1.0))
        if self._owns_client:
            self._base_client.close()
            for client in self._target_clients.values():
                client.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                logger.exception("teleop collection scan failed")
            self._stop.wait(self.interval_seconds)

    def scan_once(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            return
        try:
            self._scan()
        finally:
            self._scan_lock.release()

    def _select_destination(self, info):
        store = None
        if self.targets is not None:
            destination, login = self.targets.resolve(info)
            self._destination = dict(destination)
            self.api_url = destination['api_url']
            store = login.store
            if not platform_session_available(store):
                return False
            if self._owns_client:
                client_key = (self.api_url, str(store.path))
                if client_key not in self._target_clients:
                    self._target_clients[client_key] = platform_api_client(
                        timeout=httpx.Timeout(30.0, connect=5.0, pool=5.0), store=store)
                self.client = self._target_clients[client_key]
        username = current_platform_username(store) if store is not None else current_platform_username()
        return not info.get('author') or info['author'] == username

    def _scan(self) -> None:
        if not self.output_root.is_dir():
            return
        batches: dict[str, dict] = {}
        completed = []
        episode_dirs = {marker.parent for marker in self.output_root.glob(f"*/{READY_MARKER}")}
        if self.discover_completed:
            episode_dirs.update(
                episode_dir
                for episode_dir in self.output_root.iterdir()
                if episode_dir.is_dir()
                and (episode_dir / "meta_info.json").is_file()
                and (episode_dir / "record" / "bundles.jsonl").is_file()
                and (episode_dir / "record" / "actions.jsonl").is_file()
            )
        for episode_dir in sorted(episode_dirs):
            if (episode_dir / UPLOADED_MARKER).is_file():
                self._set_status(episode_dir, "uploaded")
                try:
                    completed.append(self._ready_info(episode_dir))
                except (OSError, ValueError):
                    pass
                continue
            try:
                info = self._ready_info(episode_dir)
                eligible = self._select_destination(info)
            except Exception as exc:
                self._set_status(episode_dir, 'failed', str(exc))
                continue
            # A queued Tianji recording belongs to its recording account. A later
            # account switch must not silently upload another operator's data.
            if not eligible:
                if (episode_dir / UPLOADED_MARKER).is_file():
                    self._set_status(episode_dir, "uploaded")
                else:
                    self._set_status(episode_dir, "pending", "等待采集账号登录后上传")
                continue
            batch = batches.setdefault(
                (self.api_url, info.get('author'), info["import_task_id"]),
                {"info": info, "episodes": [], "pending": []},
            )
            batch["episodes"].append(episode_dir)
            if not (episode_dir / UPLOADED_MARKER).is_file():
                batch["pending"].append(episode_dir)
                self._set_status(episode_dir, "pending")
            else:
                self._set_status(episode_dir, "uploaded")

        if self.targets is None and self._owns_client and (not collection_available() or not platform_session_available()):
            return

        for batch in batches.values():
            if self._stop.is_set():
                break
            pending = batch["pending"]
            if not pending:
                continue
            info = batch["info"]
            episode_total = len(batch["episodes"]) + sum(
                all(record.get(key) == info.get(key) for key in ("import_task_id", "author", "upload_destination"))
                for record in completed
            )
            try:
                if not self._select_destination(info):
                    continue
                self._sync_batch(info, "queued", episode_total=episode_total)
            except Exception as exc:
                for episode_dir in pending:
                    self._set_status(episode_dir, "failed", str(exc))
                logger.exception(
                    "teleop import queue registration failed for %s",
                    info["import_task_id"],
                )
                continue

            errors: list[str] = []
            for episode_dir in pending:
                if self._stop.is_set():
                    return
                try:
                    self._set_status(episode_dir, "uploading")
                    self._sync_batch(info, "processing", episode_total=episode_total)
                    self._upload_episode(episode_dir, info)
                    self._set_status(episode_dir, "uploaded")
                except Exception as exc:
                    self._set_status(episode_dir, "failed", str(exc))
                    errors.append(f"{episode_dir.name}: {exc}")
                    logger.exception("teleop collection upload failed for %s", episode_dir)
            if errors:
                self._sync_batch(
                    info,
                    "failed",
                    episode_total=episode_total,
                    error="; ".join(errors)[:2000],
                )
            else:
                self._sync_batch(info, "ready", episode_total=episode_total)

    def _ready_info(self, episode_dir: Path) -> dict[str, object]:
        marker = episode_dir / READY_MARKER
        try:
            payload = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            if marker.is_file() and self.targets is not None:
                raise ValueError('回合上传记录损坏，保留文件，无法确认目标平台')
            payload = {}
        if not isinstance(payload, dict):
            if self.targets is not None:
                raise ValueError('回合上传记录无效，无法确认目标平台')
            payload = {}
        episode_id = self._episode_id(episode_dir)
        task_id = payload.get("import_task_id")
        folder = payload.get("import_folder")
        session_id = payload.get("teleop_session_id")
        requested_cameras = payload.get("requested_cameras")
        source = payload.get("source")
        collection_trigger = payload.get("collection_trigger")
        dagger_save_mode = payload.get("dagger_save_mode")
        discovered = not marker.is_file()
        discovered_batch = uuid.uuid5(UPLOAD_NAMESPACE, str(self.output_root.resolve()))
        return {
            **({'upload_destination': payload['upload_destination']} if 'upload_destination' in payload else {}),
            "author": payload.get("author"),
            "import_task_id": (
                task_id.strip()
                if isinstance(task_id, str) and task_id.strip()
                else (f"gdk-folder-{discovered_batch}" if discovered else f"teleop-{episode_id}")
            ),
            "import_folder": (
                folder.strip()
                if isinstance(folder, str) and folder.strip()
                else (self.output_root.name if discovered else episode_dir.name)
            ),
            "teleop_session_id": (
                session_id.strip() if isinstance(session_id, str) and session_id.strip() else ""
            ),
            "requested_cameras": [
                camera.strip()
                for camera in requested_cameras
                if isinstance(camera, str) and camera.strip()
            ]
            if isinstance(requested_cameras, list)
            else [],
            "source": source if source in {"teleop", "intervention"} else "teleop",
            "collection_trigger": (
                collection_trigger.strip()
                if isinstance(collection_trigger, str) and collection_trigger.strip()
                else ("filesystem" if discovered else "controller")
            ),
            "dagger_save_mode": (
                dagger_save_mode if dagger_save_mode in {"full", "corrections"} else None
            ),
        }

    def _sync_batch(
        self,
        info: dict[str, object],
        status: str,
        *,
        episode_total: int,
        error: str | None = None,
    ) -> None:
        response = self.client.put(
            f"{self.api_url}/episodes/import-batches/{info['import_task_id']}",
            extensions={"expected_platform_username": info.get("author")},
            json={
                "folder_name": info["import_folder"],
                "source": info["source"],
                "status": status,
                "episode_total": episode_total,
                "error": error,
            },
        )
        response.raise_for_status()

    def _episode_id(self, episode_dir: Path) -> uuid.UUID:
        return uuid.uuid5(UPLOAD_NAMESPACE, str(episode_dir.resolve()))

    def _write_receipt(self, episode_dir: Path, episode_id: uuid.UUID) -> None:
        receipt = episode_dir / UPLOADED_MARKER
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "episode_id": str(episode_id),
                    "api_url": self.api_url,
                    "upload_destination": self._destination,
                    "uploaded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
                indent=2,
            )
        )
        os.replace(temporary, receipt)

    def _upload_episode(self, episode_dir: Path, info: dict[str, object]) -> None:
        episode_id = self._episode_id(episode_dir)
        local_relative_files = sorted(
            path.relative_to(episode_dir).as_posix()
            for path in episode_dir.rglob("*")
            if path.is_file() and path.name not in {READY_MARKER, UPLOADED_MARKER}
        )
        if "meta_info.json" not in local_relative_files or not local_relative_files:
            raise ValueError("completed teleop episode is missing meta_info.json")
        # Keep every Episode below its own subfolder inside the session import.
        # This prevents same-named files from different A/B recordings colliding
        # when the user downloads the complete source folder.
        source_files = [f"{episode_dir.name}/{relative}" for relative in local_relative_files]
        metadata = json.loads((episode_dir / "meta_info.json").read_text())
        if not isinstance(metadata, dict):
            raise ValueError("completed teleop episode has invalid meta_info.json")
        curve_sides = upload_sides(metadata) if metadata.get("robot_type") == "tianji" else None
        if curve_sides and len(curve_sides) == 1:
            local_relative_files = [name for name in local_relative_files
                                    if name not in {f"record/{side}_gripper.jsonl"
                                                    for side in ("left", "right") if side not in curve_sides}]
            source_files = [f"{episode_dir.name}/{name}" for name in local_relative_files]
        requested_cameras = info.get("requested_cameras", [])
        cameras = metadata.get("cameras", {})
        missing_extra_cameras: list[str] = []
        for camera in requested_cameras if isinstance(requested_cameras, list) else []:
            camera_dir = episode_dir / "camera" / str(camera)
            index = camera_dir / f"{camera}.txt"
            image_frames = any(
                path.suffix.lower() in {".png", ".jpg", ".jpeg"}
                for path in (camera_dir / "frames").glob("*")
            )
            camera_meta = cameras.get(camera, {}) if isinstance(cameras, dict) else {}
            frame_count = camera_meta.get("frames", 0) if isinstance(camera_meta, dict) else 0
            if (
                not index.is_file()
                or index.stat().st_size == 0
                or not image_frames
                or frame_count <= 0
            ):
                missing_extra_cameras.append(camera)

        archive_name = f"{episode_dir.name}.tar"
        if metadata.get("robot_type") == "tianji":
            if (
                not metadata.get("complete")
                or not metadata.get("actions_recorded")
                or missing_extra_cameras
            ):
                raise ValueError("天机 Marvin M6回合未完整保存，拒绝上传")
            if any(
                not (episode_dir / "record" / name).is_file()
                or not (episode_dir / "record" / name).stat().st_size
                for name in ("actions.jsonl", "bundles.jsonl")
            ):
                raise ValueError("天机 Marvin M6回合缺少动作或对齐记录，拒绝上传")
            if any("native" in Path(name).parts for name in local_relative_files):
                raise ValueError("天机 Marvin M6回合包含不应上传的 native 图像")
        presign = self.client.post(
            f"{self.api_url}/uploads/presign",
            extensions={"expected_platform_username": info.get("author")},
            json={"episode_id": str(episode_id), "filenames": [archive_name]},
        )
        if presign.status_code == 409:
            existing = self.client.get(
                f"{self.api_url}/episodes/{episode_id}",
                extensions={"expected_platform_username": info.get("author")},
            )
            if existing.is_success:
                self._write_receipt(episode_dir, episode_id)
                return
        presign.raise_for_status()
        targets = {item["filename"]: item for item in presign.json()["urls"]}
        target = targets.get(archive_name)
        if not target:
            raise ValueError(f"presign response omitted {archive_name}")
        with tempfile.TemporaryDirectory(prefix="data-platform-gdk-") as temporary_dir:
            archive_path = Path(temporary_dir) / archive_name
            _write_episode_tar(episode_dir, archive_path, local_relative_files, curve_sides)
            uploaded = self.client.put(
                target["put_url"],
                timeout=httpx.Timeout(180.0, connect=5.0, write=30.0, pool=5.0),
                auth=None,  # Presigned storage URLs must not receive platform Bearer auth.
                content=_file_chunks(archive_path),
                headers={
                    "Content-Type": "application/octet-stream",
                    # httpx cannot infer a length from a generator and falls
                    # back to chunked transfer encoding. S3-compatible PUT
                    # endpoints such as MinIO require the exact object length.
                    "Content-Length": str(archive_path.stat().st_size),
                },
            )
            uploaded.raise_for_status()

        started_at = _iso_from_ns(metadata.get("pc_start_ns"))
        ended_at = _iso_from_ns(metadata.get("pc_stop_ns"))
        duration_s = None
        if started_at and ended_at:
            duration_s = max(
                0.0,
                (
                    datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                    - datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                ).total_seconds(),
            )
        right_only = (episode_dir / "record" / "right_only_meta.json").is_file()
        fps = (
            metadata.get("frames_recorded") / duration_s
            if isinstance(metadata.get("frames_recorded"), int) and duration_s and duration_s > 0
            else 30.0
        )
        fps = round(fps) if 20 <= fps <= 40 else 30
        if metadata.get("robot_type") == "tianji":
            fps, sample_duration = _tianji_playback_timing(metadata)
            if sample_duration is not None:
                duration_s = sample_duration
        joint_names = [
            "idx61_arm_r_joint1",
            "idx62_arm_r_joint2",
            "idx63_arm_r_joint3",
            "idx64_arm_r_joint4",
            "idx65_arm_r_joint5",
            "idx66_arm_r_joint6",
            "idx67_arm_r_joint7",
            "idx71_gripper_r_inner_joint1",
        ]
        camera_features = []
        for name, camera in cameras.items() if isinstance(cameras, dict) else []:
            if not isinstance(camera, dict) or camera.get("frames", 0) <= 0:
                continue
            width, height = _camera_size(episode_dir, name, camera)
            camera_features.append(
                {
                    "key": f"observation.images.{name}",
                    "width": width,
                    "height": height,
                    "codec": "h264",
                    "fps": fps,
                }
            )
        extra = {
            "import_task_id": info["import_task_id"],
            "import_folder": info["import_folder"],
            "source_files": source_files,
            "source_archive": archive_name,
            "source_archive_format": "tar",
            "teleop_session_id": info["teleop_session_id"] or None,
            "collection_trigger": info["collection_trigger"],
            "frames_recorded": metadata.get("frames_recorded"),
            "missing_extra_cameras": missing_extra_cameras,
        }
        if info.get("dagger_save_mode") is not None:
            extra["dagger_save_mode"] = info["dagger_save_mode"]
        if metadata.get("robot_type") == "tianji":
            selected = list(curve_sides) if curve_sides else metadata.get("controlled_sides", ["left", "right"])
            observed = list(curve_sides) if curve_sides else ["left", "right"]
            joint_names = [side + "_joint_" + str(i) for side in selected for i in range(1, 8)]
            extra.update(
                {
                    "robot_type": "tianji",
                    "robot_name": TIANJI_DISPLAY_NAME,
                    "fps": fps,
                    "cameras": camera_features,
                    "camera_mapping": metadata.get("camera_mapping"),
                    "timing": {
                        "preview_fps_source": "requested_image_hz",
                        "sample_clock": "controller_monotonic_ns",
                        "bundle_timestamp_field": "timestamp_ns",
                        "camera_timestamp_files": {
                            name: "record/" + name + "_frames.jsonl"
                            for name in metadata.get("camera_mapping", {}).values()
                        },
                        "hardware_synchronized": False,
                    },
                    "controlled_sides": metadata.get("controlled_sides", ["left", "right"]),
                    "upload_sides": list(curve_sides) if curve_sides else None,
                    "data_validate": metadata.get("data_validate"),
                    "source_features": {
                        "observation.state": {
                            "dtype": "float32",
                            "shape": [7 * len(observed)],
                            "names": [
                                side + "_joint_" + str(i)
                                for side in observed
                                for i in range(1, 8)
                            ],
                        },
                        "action": {
                            "dtype": "float32",
                            "shape": [len(joint_names)],
                            "names": joint_names,
                        },
                    },
                    "gripper_features": {
                        side: {
                            "state": "state__" + side + "_effector__position",
                            "action": "action__" + side + "_effector__position",
                            "unit": "rad",
                            "missing_command_is_null": metadata.get("gripper_action_policy", "command_only") != "initial_feedback_then_last_target",
                            "target_policy": metadata.get("gripper_action_policy", "command_only"),
                            "target_source_field": "gripper_target_sources." + side,
                        }
                        for side in selected
                    },
                }
            )
        if right_only:
            extra.update(
                {
                    "fps": fps,
                    "robot_type": metadata.get("robot_type") or "agibot_g2",
                    "cameras": camera_features,
                    "source_features": {
                        "observation.state": {
                            "dtype": "float32",
                            "shape": [8],
                            "names": [
                                *[f"joint.{name}.position" for name in joint_names[:7]],
                                f"right_effector.{joint_names[7]}.position",
                            ],
                        },
                        "action": {
                            "dtype": "float32",
                            "shape": [8],
                            "names": [f"cmd.{name}" for name in joint_names],
                        },
                        "robot_timestamp_ns": {
                            "dtype": "int64",
                            "shape": [1],
                            "names": ["robot_clock_ns"],
                        },
                    },
                }
            )
        payload = {
            "id": str(episode_id),
            "source": info["source"],
            "format": "gdk",
            "task": metadata.get("task") or "web-teleop",
            # The platform shows robot_id as its robot label. Keep extra.robot_type
            # as the legacy identifier used for decoding and curve projections.
            "robot_id": TIANJI_DISPLAY_NAME if metadata.get("robot_type") == "tianji" else metadata.get("robot_type") or "agibot_g2",
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_s": duration_s,
            "storage_keys": {
                "raw": f"raw/{episode_id}/",
                "archive": target["key"],
            },
            "extra": extra,
            "author": info.get("author") or current_platform_username(),
        }
        registered = self.client.post(
            f"{self.api_url}/episodes",
            json=payload,
            extensions={"expected_platform_username": info.get("author")},
        )
        registered.raise_for_status()
        self._write_receipt(episode_dir, episode_id)
