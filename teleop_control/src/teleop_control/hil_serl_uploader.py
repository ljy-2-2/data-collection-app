from __future__ import annotations

import tarfile
import tempfile
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .hil_serl import HilSerlManager
from .models import HilSerlArtifact
from .platform_auth import platform_api_client

ARTIFACT_NAMESPACE = uuid.UUID("8df461d4-f967-4814-b49b-b89df7cef5d8")


def _chunks(path: Path, size: int = 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while chunk := stream.read(size):
            yield chunk


class HilSerlArtifactUploader:
    """Mirror stable HIL-SERL checkpoints and RL pickle files to platform storage."""

    def __init__(
        self,
        manager: HilSerlManager,
        api_url: str,
        *,
        interval_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.manager = manager
        self.api_url = api_url.rstrip("/")
        self.interval_seconds = interval_seconds
        self.client = client or platform_api_client(timeout=None)
        self._owns_client = client is None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hil-serl-uploader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval_seconds + 1.0))
        if self._owns_client:
            self.client.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception:
                pass
            self._stop.wait(self.interval_seconds)

    @staticmethod
    def _fingerprint(path: Path) -> tuple[str, int, int]:
        files = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
        size = sum(item.stat().st_size for item in files)
        modified_ns = max(
            (item.stat().st_mtime_ns for item in files), default=path.stat().st_mtime_ns
        )
        return f"{len(files)}:{size}:{modified_ns}", size, modified_ns

    @staticmethod
    def _artifact_id(item: HilSerlArtifact, fingerprint: str) -> uuid.UUID:
        return uuid.uuid5(ARTIFACT_NAMESPACE, f"{item.kind}:{item.path}:{fingerprint}")

    def _upload(
        self, item: HilSerlArtifact, fingerprint: str, source_size: int, modified_ns: int
    ) -> uuid.UUID:
        path = Path(item.path)
        artifact_id = self._artifact_id(item, fingerprint)
        archive_name = f"{path.name}.tar" if path.is_dir() else path.name
        response = self.client.post(
            f"{self.api_url}/hil-serl/artifacts/presign",
            json={
                "id": str(artifact_id),
                "kind": item.kind,
                "name": item.name,
                "fingerprint": fingerprint,
                "size_bytes": source_size,
                "modified_at": datetime.fromtimestamp(modified_ns / 1e9, UTC).isoformat(),
                "archive_name": archive_name,
            },
        )
        response.raise_for_status()
        target = response.json()
        if target.get("ready"):
            return artifact_id
        with tempfile.TemporaryDirectory(prefix="hil-serl-upload-") as temporary:
            if path.is_dir():
                upload_path = Path(temporary) / archive_name
                with tarfile.open(upload_path, mode="w") as archive:
                    archive.add(path, arcname=path.name)
            else:
                upload_path = path
            uploaded = self.client.put(
                target["put_url"],
                auth=None,  # Presigned storage URLs already contain their authentication.
                content=_chunks(upload_path),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(upload_path.stat().st_size),
                },
            )
            uploaded.raise_for_status()
            completed = self.client.post(
                f"{self.api_url}/hil-serl/artifacts/{artifact_id}/complete"
            )
            completed.raise_for_status()
        return artifact_id

    def sync_once(self) -> None:
        if not self.manager.root.is_dir():
            return
        artifacts = self.manager.artifacts()
        items = [*artifacts.models, *artifacts.bc_models, *artifacts.replays]
        current_root = None
        if self.manager.active():
            value = self.manager.status().checkpoint_path
            current_root = Path(value) if value else None
        ids: list[str] = []
        for item in items:
            path = Path(item.path)
            fingerprint, size, modified = self._fingerprint(path)
            artifact_id = self._artifact_id(item, fingerprint)
            ids.append(str(artifact_id))
            if current_root is not None and path.is_relative_to(current_root):
                continue
            self._upload(item, fingerprint, size, modified)
        reconciled = self.client.post(
            f"{self.api_url}/hil-serl/artifacts/sync/reconcile", json={"ids": ids}
        )
        reconciled.raise_for_status()
