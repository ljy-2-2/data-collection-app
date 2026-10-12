from __future__ import annotations

import json
import hashlib
import logging
import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from .collection_gate import collection_available
from .platform_auth import (
    current_platform_username,
    platform_api_client,
    platform_session_available,
)

UPLOADED_MARKER = ".data-platform-uploaded.json"
UPLOAD_NAMESPACE = uuid.UUID("70d72ab4-cfed-4d80-b74e-b238098b743b")
logger = logging.getLogger(__name__)


def _episode_times(meta: dict) -> tuple[str | None, str | None, float | None]:
    episode = meta.get("episode") if isinstance(meta.get("episode"), dict) else {}
    start = episode.get("start_time")
    end = episode.get("end_time")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end < start:
        return None, None, None
    return (
        datetime.fromtimestamp(start, UTC).isoformat(),
        datetime.fromtimestamp(end, UTC).isoformat(),
        float(end - start),
    )


def _chunks(path: Path, size: int = 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as source:
        while chunk := source.read(size):
            yield chunk


class UmiRosbagUploader:
    """Import completed Galaxis ROS 2 MCAP bags into the platform."""

    def __init__(self, data_root: str | Path, api_url: str, *,
                 interval_seconds: float = 3.0, client: httpx.Client | None = None) -> None:
        self.root = Path(data_root).expanduser() / "rosbags"
        self.api_url = api_url.rstrip("/")
        self.interval_seconds = interval_seconds
        self.client = client or platform_api_client(timeout=None)
        self._owns_client = client is None
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="umi-rosbag-uploader", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.interval_seconds + 1))
        if self._owns_client:
            self.client.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                logger.exception("UMI rosbag import scan failed")
            self._wake.wait(self.interval_seconds)
            self._wake.clear()

    def request_scan(self) -> None:
        # Retrying never waits on network I/O or video encoding in the UI.
        for pending in self.root.glob("*/*/.mp4-pending.json"):
            failure = pending.parent / "recording-failure.json"
            try:
                if json.loads(failure.read_text()).get("stage") == "mp4":
                    failure.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        self._wake.set()

    @staticmethod
    def _processing(episode_dir: Path) -> bool:
        return any((episode_dir / name).exists() for name in
                   (".mp4-pending.json", ".mp4-converting", "recording-failure.json"))

    def scan_once(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            return
        try:
            self._scan()
        finally:
            self._scan_lock.release()

    def _scan(self) -> None:
        if self._owns_client and (not collection_available() or not platform_session_available()):
            return
        if not self.root.is_dir():
            return
        for meta_path in sorted(self.root.glob("*/*/meta.json")):
            episode_dir = meta_path.parent
            if self._processing(episode_dir) or (episode_dir / UPLOADED_MARKER).is_file():
                continue
            self._owner = None
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                owner = meta.get("author")
                if self._owns_client and owner and owner != current_platform_username():
                    continue
                if not (episode_dir / "metadata.yaml").is_file() and not (episode_dir / "media/video-manifest.json").is_file():
                    continue
                self._owner = owner or current_platform_username()
                self._upload(episode_dir, meta)
            except Exception as exc:
                logger.exception("UMI rosbag import failed for %s", episode_dir)
                episode_id = uuid.uuid5(UPLOAD_NAMESPACE, str(episode_dir.resolve()))
                try:
                    self._batch(f"umi-{episode_id}", episode_dir.name, "failed", str(exc)[:2000])
                except Exception:
                    logger.exception("could not update failed UMI import batch")

    def _auth_extensions(self, owner: str | None = None) -> dict:
        return {"expected_platform_username": owner or getattr(self, "_owner", None)}

    def _batch(self, batch_id: str, folder: str, status: str, error: str | None = None) -> None:
        response = self.client.put(
            f"{self.api_url}/episodes/import-batches/{batch_id}",
            json={"folder_name": folder, "source": "rosbag", "status": status,
                  "episode_total": 1, "error": error},
            extensions=self._auth_extensions(),
        )
        response.raise_for_status()

    def _upload(self, episode_dir: Path, meta: dict) -> None:
        if self._processing(episode_dir):
            return
        episode_id = uuid.uuid5(UPLOAD_NAMESPACE, str(episode_dir.resolve()))
        batch_id = f"umi-{episode_id}"
        video_manifest = episode_dir / "media/video-manifest.json"
        compact = video_manifest.is_file()
        if meta.get("recording_format") == "umi-mp4-v1" and not compact:
            raise ValueError("MP4 recording is missing its video manifest")
        payload_dir = video_manifest.parent if compact else episode_dir
        metadata = yaml.safe_load((payload_dir / "metadata.yaml").read_text(encoding="utf-8"))
        info = metadata["rosbag2_bagfile_information"]
        names = info.get("relative_file_paths") or []
        # The platform's rosbag ingest consumes one MCAP per Episode. Our
        # recorder does not split bags; reject unexpected layouts explicitly.
        if len(names) != 1 or not isinstance(names[0], str) or Path(names[0]).name != names[0]:
            raise ValueError("rosbag Episode must contain one complete MCAP file")
        mcap = payload_dir / names[0]
        if mcap.suffix.lower() != ".mcap" or not mcap.is_file() or mcap.is_symlink():
            raise ValueError("rosbag metadata references a missing MCAP file")
        with mcap.open("rb") as handle:
            magic = b"\x89MCAP0\r\n"
            if handle.read(8) != magic or mcap.stat().st_size < 16:
                raise ValueError("invalid MCAP header")
            handle.seek(-8, 2)
            if handle.read(8) != magic:
                raise ValueError("MCAP has not been finalized")
        files = [mcap, payload_dir / "metadata.yaml", episode_dir / "meta.json"]
        video_files = []
        if compact:
            manifest = json.loads(video_manifest.read_text(encoding="utf-8"))
            if manifest.get("format") != "umi-mp4-v1" or manifest.get("state") != "complete":
                raise ValueError("MP4 recording has not completed")
            cameras = manifest.get("cameras", [])
            topics = [camera["topic"] for camera in cameras]
            expected = {f"/{side}_camera/image_raw/compressed" for side in ("left", "right", "center")}
            if len(set(topics)) != len(topics) or not set(topics) <= expected or len(topics) < 2 or "/center_camera/image_raw/compressed" not in topics:
                raise ValueError("MP4 camera list is incomplete")
            if manifest["signals"]["file"] != mcap.name:
                raise ValueError("MP4 manifest and MCAP metadata disagree")
            for entry in [manifest["signals"], *cameras]:
                name = entry["file"]
                if not name or Path(name).name != name:
                    raise ValueError("Invalid MP4 filename")
                path = payload_dir / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError("MP4 recording artifact is missing")
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                        raise ValueError(f"MP4 recording checksum mismatch: {name}")
                if entry is not manifest["signals"]:
                    if path.suffix != ".mp4" or not entry.get("frames") or len(entry["frames"]) != entry["frame_count"]:
                        raise ValueError("MP4 frame timestamps are incomplete")
                    video_files.append(path)
            files.extend([video_manifest, *video_files])
        if len({path.name for path in files}) != len(files):
            raise ValueError("Duplicate recording artifact filenames")
        if any(path.is_symlink() for path in files):
            raise ValueError("rosbag artifacts must be regular files")
        self._batch(batch_id, episode_dir.name, "processing")
        presign = self.client.post(
            f"{self.api_url}/uploads/presign",
            json={"episode_id": str(episode_id), "filenames": [path.name for path in files]},
            extensions=self._auth_extensions(),
        )
        if presign.status_code == 409:
            existing = self.client.get(f"{self.api_url}/episodes/{episode_id}", extensions=self._auth_extensions())
            if existing.is_success:
                self._batch(batch_id, episode_dir.name, "ready")
                self._receipt(episode_dir, episode_id)
                return
        presign.raise_for_status()
        targets = {item["filename"]: item for item in presign.json()["urls"]}
        for path in files:
            target = targets[path.name]
            response = self.client.put(
                target["put_url"], content=_chunks(path),
                # The signed storage URL supplies its own authentication.
                # Sending the platform Bearer token as well makes MinIO reject
                # the upload with "multiple authentication types".
                auth=None,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(path.stat().st_size)},
            )
            response.raise_for_status()
        started_at, ended_at, duration_s = _episode_times(meta)
        storage = {
            "raw": f"raw/{episode_id}/",
            "mcap": targets[mcap.name]["key"],
            "metadata": targets["metadata.yaml"]["key"],
            "meta": targets["meta.json"]["key"],
        }
        if compact:
            storage["video_manifest"] = targets[video_manifest.name]["key"]
            storage["source_videos"] = [targets[path.name]["key"] for path in video_files]
        payload = {
            "id": str(episode_id), "source": "rosbag", "format": "rosbag",
            "task": meta.get("task_name") or meta.get("task") or episode_dir.parent.name,
            "robot_id": str(meta.get("robot_name") or meta.get("robot_id") or "galaxis-portable"),
            "started_at": started_at, "ended_at": ended_at,
            "duration_s": duration_s, "storage_keys": storage,
            "extra": {"import_task_id": batch_id, "import_folder": episode_dir.name,
                      "source_files": [path.name for path in files],
                      "collection_source": "umi",
                      "recording_format": "umi-mp4-v1" if compact else "rosbag2-mcap",
                      "episode_metadata": meta.get("episode", {}),
                      "segments": meta.get("segments", [])},
            "author": meta.get("author") or current_platform_username(),
        }
        registered = self.client.post(f"{self.api_url}/episodes", json=payload, extensions=self._auth_extensions())
        registered.raise_for_status()
        self._batch(batch_id, episode_dir.name, "ready")
        self._receipt(episode_dir, episode_id)

    def _receipt(self, episode_dir: Path, episode_id: uuid.UUID) -> None:
        path = episode_dir / UPLOADED_MARKER
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"episode_id": str(episode_id),
            "uploaded_at": datetime.now(UTC).isoformat()}), encoding="utf-8")
        os.replace(temporary, path)
