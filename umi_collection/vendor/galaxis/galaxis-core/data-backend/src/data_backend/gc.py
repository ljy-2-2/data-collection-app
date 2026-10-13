"""S3 garbage collection service.

Monitors bucket storage and deletes eligible recordings based on
configurable strategy predicates (marker, age, space).

Recording classification (using two abstract markers):
- **Bad: empty** — directory with no files → deleted immediately.
- **Bad: orphan** — no completion marker AND newest object older
  than ``orphan_age_hours`` → deleted (race-safe: in-progress
  recordings are recent).
- **Good, not stored** — has completion marker but no storage
  marker → kept (awaiting persistent storage upload).
- **Good, stored** — has both markers → eligible for strategy-based
  GC deletion.

After deletion, empty ancestor "directories" (prefixes) are pruned
bottom-up.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from minio import Minio
from minio.deleteobjects import DeleteObject

from data_backend.config import GCConfig

logger = logging.getLogger("data_backend.gc")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class GCCycleResult:
    total_recordings: int = 0
    total_bytes: int = 0
    eligible_count: int = 0
    deleted_count: int = 0
    deleted_bytes: int = 0
    orphan_count: int = 0
    orphan_bytes: int = 0
    pruned_branches: int = 0
    error: str | None = None


@dataclass
class RecordingInfo:
    prefix: str
    objects: list = field(default_factory=list)  # list of (object_name, size)
    total_size_bytes: int = 0
    completion_marker_exists: bool = False
    storage_marker_exists: bool = False
    completed_at: datetime | None = None
    latest_object_modified: datetime | None = None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_recordings(
    client: Minio, bucket: str, prefix: str, cfg: GCConfig
) -> list[RecordingInfo]:
    """Discover recording directories at any depth in the tree.

    Groups objects by their parent directory (leaf prefix).  Works with
    arbitrary nesting like ``robot1/2025-01-15/rec_001/data.mcap``.
    """
    recordings: dict[str, RecordingInfo] = {}

    for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
        key = obj.object_name
        if obj.is_dir or "/" not in key:
            continue

        parent = key.rsplit("/", 1)[0] + "/"

        if parent not in recordings:
            recordings[parent] = RecordingInfo(prefix=parent)
        rec = recordings[parent]

        rec.objects.append((key, obj.size))
        rec.total_size_bytes += obj.size

        if obj.last_modified and (
            rec.latest_object_modified is None
            or obj.last_modified > rec.latest_object_modified
        ):
            rec.latest_object_modified = obj.last_modified

        basename = key.rsplit("/", 1)[1]
        if basename == cfg.storage_marker:
            rec.storage_marker_exists = True
        if basename == cfg.completion_marker:
            rec.completion_marker_exists = True

    return list(recordings.values())


def enrich_completed_at(
    client: Minio, bucket: str, cfg: GCConfig, recordings: list[RecordingInfo]
) -> None:
    """Read recording_complete markers to get completed_at timestamps."""
    for rec in recordings:
        marker_key = rec.prefix + cfg.completion_marker
        try:
            response = client.get_object(bucket, marker_key)
            data = json.loads(response.read())
            response.close()
            response.release_conn()
            if "completed_at" in data:
                rec.completed_at = datetime.fromisoformat(data["completed_at"])
        except Exception:
            _fallback_last_modified(rec)


def _fallback_last_modified(rec: RecordingInfo) -> None:
    """Use the newest object's last_modified (captured during discovery) as a fallback."""
    if rec.latest_object_modified is not None:
        rec.completed_at = rec.latest_object_modified


# ---------------------------------------------------------------------------
# Strategy-based selection
# ---------------------------------------------------------------------------


def select_for_deletion(
    recordings: list[RecordingInfo], cfg: GCConfig
) -> list[RecordingInfo]:
    """Return the recordings that should be deleted this cycle.

    Uses the parsed ``cfg.strategy`` predicate to evaluate each recording
    against the (marker, age, space) conditions.
    """
    now = datetime.now(timezone.utc)
    total_bytes = sum(r.total_size_bytes for r in recordings)
    threshold_bytes = cfg.max_storage_gb * 1_000_000_000

    # Sort oldest first for deterministic space-based eviction order
    candidates = sorted(
        recordings,
        key=lambda r: r.completed_at or datetime.min.replace(tzinfo=timezone.utc),
    )

    to_delete: list[RecordingInfo] = []
    for rec in candidates:
        marker = rec.storage_marker_exists
        age = False
        if cfg.max_age_hours > 0 and rec.completed_at is not None:
            age_hours = (now - rec.completed_at).total_seconds() / 3600
            age = age_hours >= cfg.max_age_hours
        space = threshold_bytes > 0 and total_bytes > threshold_bytes

        if cfg.strategy.evaluate(marker=marker, age=age, space=space):
            to_delete.append(rec)
            total_bytes -= rec.total_size_bytes  # update for subsequent space checks

    return to_delete


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------


def select_orphans(
    recordings: list[RecordingInfo], cfg: GCConfig
) -> list[RecordingInfo]:
    """Return recordings that are orphaned (no recording_complete marker and stale)."""
    if cfg.orphan_age_hours <= 0:
        return []
    now = datetime.now(timezone.utc)
    orphans = []
    for rec in recordings:
        if rec.completion_marker_exists or rec.storage_marker_exists:
            continue
        if rec.latest_object_modified is None:
            continue
        age_hours = (now - rec.latest_object_modified).total_seconds() / 3600
        if age_hours >= cfg.orphan_age_hours:
            orphans.append(rec)
    return orphans


# ---------------------------------------------------------------------------
# Empty branch pruning
# ---------------------------------------------------------------------------


def prune_empty_branches(
    client: Minio,
    bucket: str,
    prefix: str,
    cfg: GCConfig,
    deleted_prefixes: list[str],
) -> int:
    """Remove empty ancestor directories bottom-up after deletion."""
    if cfg.dry_run:
        return 0

    candidates: set[str] = set()
    for dp in deleted_prefixes:
        parts = dp.rstrip("/").split("/")
        for i in range(len(parts) - 1, 0, -1):
            ancestor = "/".join(parts[:i]) + "/"
            if ancestor == prefix or len(ancestor) <= len(prefix):
                break
            candidates.add(ancestor)

    pruned = 0
    for pfx in sorted(candidates, key=len, reverse=True):
        objects = list(client.list_objects(bucket, prefix=pfx, recursive=False))
        real_objects = [o for o in objects if not o.is_dir]
        if not real_objects:
            placeholders = [
                o.object_name
                for o in objects
                if o.is_dir and o.object_name.endswith("/")
            ]
            if placeholders:
                delete_list = [DeleteObject(name) for name in placeholders]
                list(client.remove_objects(bucket, delete_list))
            pruned += 1
            logger.debug(f"Pruned empty branch: {pfx}")

    return pruned


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def delete_recordings(
    client: Minio, bucket: str, cfg: GCConfig, recordings: list[RecordingInfo]
) -> int:
    """Delete all objects belonging to the given recordings. Returns count deleted."""
    total_deleted = 0
    for rec in recordings:
        object_names = [name for name, _ in rec.objects]
        if cfg.dry_run:
            logger.info(
                f"[DRY RUN] Would delete {len(object_names)} objects from {rec.prefix}"
            )
            continue
        logger.info(f"Deleting {len(object_names)} objects from {rec.prefix}")
        delete_list = [DeleteObject(name) for name in object_names]
        errors = list(client.remove_objects(bucket, delete_list))
        if errors:
            for err in errors:
                logger.error(f"Delete error: {err}")
        else:
            total_deleted += len(object_names)
    return total_deleted


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------


def run_gc_cycle(client: Minio, bucket: str, prefix: str, cfg: GCConfig) -> None:
    """Execute a single garbage collection cycle."""
    logger.info(f"Starting GC cycle (strategy={cfg.strategy.description})")

    recordings = discover_recordings(client, bucket, prefix, cfg)
    total_gb = sum(r.total_size_bytes for r in recordings) / 1_000_000_000
    eligible_count = sum(1 for r in recordings if r.storage_marker_exists)
    logger.info(
        f"Discovered {len(recordings)} recordings ({total_gb:.2f} GB), "
        f"{eligible_count} eligible for GC"
    )

    enrich_completed_at(client, bucket, cfg, recordings)

    all_deleted_prefixes: list[str] = []
    to_delete = select_for_deletion(recordings, cfg)
    if to_delete:
        delete_size_gb = sum(r.total_size_bytes for r in to_delete) / 1_000_000_000
        logger.info(
            f"Selected {len(to_delete)} recordings for deletion ({delete_size_gb:.2f} GB)"
        )
        delete_recordings(client, bucket, cfg, to_delete)
        all_deleted_prefixes.extend(r.prefix for r in to_delete)

    orphans = select_orphans(recordings, cfg)
    if orphans:
        orphan_gb = sum(r.total_size_bytes for r in orphans) / 1_000_000_000
        logger.info(f"Found {len(orphans)} orphaned recordings ({orphan_gb:.2f} GB)")
        delete_recordings(client, bucket, cfg, orphans)
        all_deleted_prefixes.extend(r.prefix for r in orphans)

    if all_deleted_prefixes:
        pruned = prune_empty_branches(client, bucket, prefix, cfg, all_deleted_prefixes)
        if pruned:
            logger.info(f"Pruned {pruned} empty branches")


def run_gc_cycle_with_result(
    client: Minio, bucket: str, prefix: str, cfg: GCConfig
) -> GCCycleResult:
    """Execute a single GC cycle and return a structured result."""
    result = GCCycleResult()

    try:
        recordings = discover_recordings(client, bucket, prefix, cfg)
        result.total_recordings = len(recordings)
        result.total_bytes = sum(r.total_size_bytes for r in recordings)
        result.eligible_count = sum(1 for r in recordings if r.storage_marker_exists)

        enrich_completed_at(client, bucket, cfg, recordings)

        all_deleted_prefixes: list[str] = []
        to_delete = select_for_deletion(recordings, cfg)
        if to_delete:
            result.deleted_bytes = sum(r.total_size_bytes for r in to_delete)
            result.deleted_count = delete_recordings(client, bucket, cfg, to_delete)
            all_deleted_prefixes.extend(r.prefix for r in to_delete)

        orphans = select_orphans(recordings, cfg)
        if orphans:
            result.orphan_count = len(orphans)
            result.orphan_bytes = sum(r.total_size_bytes for r in orphans)
            result.deleted_count += delete_recordings(client, bucket, cfg, orphans)
            all_deleted_prefixes.extend(r.prefix for r in orphans)

        if all_deleted_prefixes:
            result.pruned_branches = prune_empty_branches(
                client, bucket, prefix, cfg, all_deleted_prefixes
            )
    except Exception as exc:
        result.error = str(exc)

    return result


# Diagnostic level constants (mirror diagnostic_msgs/DiagnosticStatus)
_DIAG_OK = 0
_DIAG_WARN = 1
_DIAG_ERROR = 2


def compute_diagnostic_level(cfg: GCConfig, result: GCCycleResult) -> int:
    """Return diagnostic level: 0=OK, 1=WARN, 2=ERROR."""
    if result.error is not None:
        return _DIAG_ERROR

    if cfg.max_storage_gb <= 0:
        return _DIAG_OK

    threshold_bytes = cfg.max_storage_gb * 1_000_000_000

    if result.total_bytes > threshold_bytes:
        if result.eligible_count == 0:
            return _DIAG_ERROR
        return _DIAG_WARN

    if result.total_bytes > threshold_bytes * 0.8:
        return _DIAG_WARN

    return _DIAG_OK
