"""Storage manager — orchestrates multi-target upload, GC, and retention.

Pure Python, no ROS dependency.  The ROS node is a thin wrapper that
feeds recording paths and wires timers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from minio import Minio

from data_backend.config import (
    Priority,
    StorageConfig,
    TargetConfig,
)
from data_backend.gc import (
    GCCycleResult,
    compute_diagnostic_level,
    run_gc_cycle_with_result,
)
from data_backend.upload import (
    recover_pending_uploads,
    retention_cleanup,
    upload_recording,
)
from data_backend.upload_state import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    UploadStateDB,
)

logger = logging.getLogger("data_backend.manager")


# ---------------------------------------------------------------------------
# Live target — config paired with S3 client
# ---------------------------------------------------------------------------


@dataclass
class LiveTarget:
    cfg: TargetConfig
    client: Minio


@dataclass
class UploadResult:
    """Result of uploading one recording to all targets."""

    rec_name: str
    all_ok: bool = True
    completed_targets: list[str] = field(default_factory=list)
    failed_targets: list[str] = field(default_factory=list)
    can_delete_local: bool = False


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class StorageManager:
    """Manages multi-target upload, GC, retention, and state tracking."""

    def __init__(self, cfg: StorageConfig, log=None):
        self._cfg = cfg
        self._log = log or logger
        self._state_db = UploadStateDB(cfg.state_db)
        self._targets = self._connect_targets(cfg.targets)

    @property
    def targets(self) -> list[LiveTarget]:
        return self._targets

    @property
    def state_db(self) -> UploadStateDB:
        return self._state_db

    # ------------------------------------------------------------------
    # Target initialization
    # ------------------------------------------------------------------

    def _connect_targets(self, configs: list[TargetConfig]) -> list[LiveTarget]:
        live = []
        for tcfg in configs:
            try:
                kwargs: dict = {
                    "access_key": tcfg.access_key,
                    "secret_key": tcfg.secret_key,
                    "secure": tcfg.use_ssl,
                }
                if tcfg.use_ssl and not tcfg.verify_ssl:
                    import urllib3

                    kwargs["http_client"] = urllib3.PoolManager(
                        cert_reqs="CERT_NONE",
                    )
                client = Minio(tcfg.endpoint, **kwargs)
                self._log.info(f"S3 client connected to {tcfg.endpoint}")
                live.append(LiveTarget(cfg=tcfg, client=client))
            except Exception as e:
                self._log.error(f"Failed to create S3 client for {tcfg.endpoint}: {e}")
        return live

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload(self, dir_path: str) -> UploadResult:
        """Upload a recording to all enabled targets.

        Returns an :class:`UploadResult` with per-target outcomes and
        whether the local directory can be deleted.
        """
        rec_name = os.path.basename(dir_path)
        result = UploadResult(rec_name=rec_name)

        if not os.path.isdir(dir_path):
            self._log.warning(f"Upload dir does not exist: {dir_path}")
            result.all_ok = False
            return result

        for lt in self._targets:
            if not self._state_db.should_upload(rec_name, lt.cfg.name):
                self._log.info(f"[{lt.cfg.name}] Skipping {rec_name} (already done)")
                result.completed_targets.append(lt.cfg.name)
                continue

            try:
                etags = upload_recording(
                    lt.client,
                    lt.cfg.bucket,
                    lt.cfg.prefix,
                    dir_path,
                    delete_after=False,
                    path_rule=lt.cfg.path_rule.value,
                    logger=self._log,
                )
                self._state_db.set_status(
                    rec_name, lt.cfg.name, STATUS_COMPLETED, etags=etags
                )
                result.completed_targets.append(lt.cfg.name)
                self._log.info(
                    f"[{lt.cfg.name}] Uploaded {rec_name} ({len(etags)} file(s))"
                )
            except Exception as e:
                result.all_ok = False
                result.failed_targets.append(lt.cfg.name)
                if lt.cfg.priority != Priority.OPTIONAL:
                    self._state_db.set_status(
                        rec_name, lt.cfg.name, STATUS_FAILED, error=str(e)
                    )
                self._log.error(f"[{lt.cfg.name}] Upload failed for {rec_name}: {e}")

        result.can_delete_local = self.can_delete_local(rec_name)
        return result

    def needs_retry(self, rec_name: str) -> bool:
        """Check if any required/preferred targets still need this recording."""
        return any(
            lt.cfg.priority in (Priority.REQUIRED, Priority.PREFERRED)
            and self._state_db.should_upload(rec_name, lt.cfg.name)
            for lt in self._targets
        )

    # ------------------------------------------------------------------
    # Deletion policy
    # ------------------------------------------------------------------

    def _can_delete_local(self, rec_name: str) -> bool:
        """Check priority-based deletion policy.

        Delete when all REQUIRED targets are completed.
        If no REQUIRED targets, delete when any PREFERRED is completed.
        OPTIONAL targets never block deletion.
        """
        required = [lt for lt in self._targets if lt.cfg.priority == Priority.REQUIRED]
        preferred = [
            lt for lt in self._targets if lt.cfg.priority == Priority.PREFERRED
        ]

        if required:
            return all(
                not self._state_db.should_upload(rec_name, lt.cfg.name)
                for lt in required
            )
        if preferred:
            return any(
                not self._state_db.should_upload(rec_name, lt.cfg.name)
                for lt in preferred
            )
        return True

    def can_delete_local(self, rec_name: str) -> bool:
        """Check if local recording can be deleted.

        Returns False if delete_after_upload is disabled.
        Otherwise checks priority-based policy.
        """
        if not self._cfg.delete_after_upload:
            return False
        return self._can_delete_local(rec_name)

    # ------------------------------------------------------------------
    # GC
    # ------------------------------------------------------------------

    def run_gc(self, target_name: str | None = None) -> dict[str, GCCycleResult]:
        """Run GC cycle for one or all GC-enabled targets.

        Returns ``{target_name: GCCycleResult}`` for each target that ran.
        """
        results: dict[str, GCCycleResult] = {}
        for lt in self._targets:
            if lt.cfg.gc is None:
                continue
            if target_name is not None and lt.cfg.name != target_name:
                continue

            self._log.info(f"[{lt.cfg.name}] Running GC cycle...")
            result = run_gc_cycle_with_result(
                lt.client, lt.cfg.bucket, lt.cfg.prefix, lt.cfg.gc
            )
            results[lt.cfg.name] = result

            if result.error:
                self._log.error(f"[{lt.cfg.name}] GC error: {result.error}")
            else:
                total_gb = result.total_bytes / 1_000_000_000
                self._log.info(
                    f"[{lt.cfg.name}] GC complete: "
                    f"{result.total_recordings} recordings ({total_gb:.2f} GB), "
                    f"{result.eligible_count} eligible, "
                    f"{result.deleted_count} deleted, "
                    f"{result.orphan_count} orphans, "
                    f"{result.pruned_branches} pruned"
                )

        return results

    def gc_diagnostic_level(self, target_name: str, result: GCCycleResult) -> int:
        """Compute diagnostic level for a GC result."""
        for lt in self._targets:
            if lt.cfg.name == target_name and lt.cfg.gc is not None:
                return compute_diagnostic_level(lt.cfg.gc, result)
        return 0

    # ------------------------------------------------------------------
    # Recovery & retention
    # ------------------------------------------------------------------

    def recover_pending(self, base_dir: str) -> list[str]:
        """Scan for local dirs with meta.json that may need uploading."""
        return recover_pending_uploads(base_dir, logger=self._log)

    def retention_cleanup(self, base_dir: str) -> list[str]:
        """Remove old local recordings by age using config settings."""
        return retention_cleanup(
            base_dir,
            self._cfg.local_retention_hours,
            self._cfg.delete_after_upload,
            logger=self._log,
        )

    def purge_state(self) -> int:
        """Purge old terminal rows from state DB."""
        if self._cfg.state_purge_age_hours > 0:
            purged = self._state_db.purge(self._cfg.state_purge_age_hours)
            if purged:
                self._log.info(f"Purged {purged} stale state DB rows")
            return purged
        return 0
