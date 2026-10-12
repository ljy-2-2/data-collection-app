"""SQLite-backed upload state tracking.

Tracks per-recording × per-target upload and GC status.  Pure Python,
no ROS dependency — testable standalone.

Statuses:
    pending     — queued for upload
    uploading   — upload in progress
    completed   — uploaded, files exist on target
    gc_deleted  — was uploaded, then intentionally GC'd (never re-uploaded)
    failed      — upload attempt failed
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS upload_state (
    recording   TEXT NOT NULL,
    target      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    completed_at TEXT,
    gc_deleted_at TEXT,
    etags       TEXT,
    error       TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (recording, target)
);
"""

STATUS_PENDING = "pending"
STATUS_UPLOADING = "uploading"
STATUS_COMPLETED = "completed"
STATUS_GC_DELETED = "gc_deleted"
STATUS_FAILED = "failed"

_SKIP_STATUSES = (STATUS_COMPLETED, STATUS_GC_DELETED)


@dataclass
class UploadStatus:
    recording: str
    target: str
    status: str
    completed_at: str | None = None
    gc_deleted_at: str | None = None
    etags: dict | None = None
    error: str | None = None
    updated_at: str = ""


class UploadStateDB:
    """Thread-safe SQLite state database for upload tracking."""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_status(self, recording: str, target: str) -> UploadStatus | None:
        """Get the upload status for a recording on a target."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM upload_state WHERE recording = ? AND target = ?",
                (recording, target),
            ).fetchone()
        if row is None:
            return None
        etags = json.loads(row["etags"]) if row["etags"] else None
        return UploadStatus(
            recording=row["recording"],
            target=row["target"],
            status=row["status"],
            completed_at=row["completed_at"],
            gc_deleted_at=row["gc_deleted_at"],
            etags=etags,
            error=row["error"],
            updated_at=row["updated_at"],
        )

    def set_status(
        self,
        recording: str,
        target: str,
        status: str,
        *,
        etags: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Insert or update the status for a recording on a target."""
        now = self._now()
        completed_at = now if status == STATUS_COMPLETED else None
        gc_deleted_at = now if status == STATUS_GC_DELETED else None
        etags_json = json.dumps(etags) if etags else None

        with self._lock:
            self._conn.execute(
                """INSERT INTO upload_state
                   (recording, target, status, completed_at, gc_deleted_at, etags, error, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(recording, target) DO UPDATE SET
                       status = excluded.status,
                       completed_at = COALESCE(excluded.completed_at, upload_state.completed_at),
                       gc_deleted_at = COALESCE(excluded.gc_deleted_at, upload_state.gc_deleted_at),
                       etags = COALESCE(excluded.etags, upload_state.etags),
                       error = excluded.error,
                       updated_at = excluded.updated_at
                """,
                (
                    recording,
                    target,
                    status,
                    completed_at,
                    gc_deleted_at,
                    etags_json,
                    error,
                    now,
                ),
            )
            self._conn.commit()

    def mark_gc_deleted(self, recording: str, target: str) -> None:
        """Mark a recording as GC-deleted on a target. Prevents re-upload."""
        self.set_status(recording, target, STATUS_GC_DELETED)

    def should_upload(self, recording: str, target: str) -> bool:
        """Return True if this recording should be uploaded to this target."""
        row = self.get_status(recording, target)
        if row is None:
            return True
        return row.status not in _SKIP_STATUSES

    def get_pending(self, target: str) -> list[str]:
        """Return recording names that need uploading to this target."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT recording FROM upload_state WHERE target = ? AND status IN (?, ?, ?)",
                (target, STATUS_PENDING, STATUS_UPLOADING, STATUS_FAILED),
            ).fetchall()
        return [r["recording"] for r in rows]

    def get_completed(self, target: str) -> list[str]:
        """Return recording names that are completed on this target."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT recording FROM upload_state WHERE target = ? AND status = ?",
                (target, STATUS_COMPLETED),
            ).fetchall()
        return [r["recording"] for r in rows]

    def all_gc_targets_completed(self, recording: str, gc_targets: list[str]) -> bool:
        """Check if a recording is completed on ALL gc-eligible targets."""
        for target in gc_targets:
            row = self.get_status(recording, target)
            if row is None or row.status != STATUS_COMPLETED:
                return False
        return True

    def purge(self, max_age_hours: float) -> int:
        """Remove terminal-state rows older than *max_age_hours*.

        Only deletes rows in ``completed`` or ``gc_deleted`` status.
        Rows in ``pending``, ``uploading``, or ``failed`` are never
        purged — they still need action.

        Returns the number of rows deleted.  Pass 0 to disable.
        """
        if max_age_hours <= 0:
            return 0
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM upload_state WHERE updated_at < ? AND status IN (?, ?)",
                (cutoff, STATUS_COMPLETED, STATUS_GC_DELETED),
            )
            self._conn.commit()
        return cur.rowcount
