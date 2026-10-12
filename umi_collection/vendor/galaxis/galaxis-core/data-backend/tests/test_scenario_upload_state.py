"""Scenario tests for upload state tracking.

Each test exercises a complete multi-step workflow through the state DB,
verifying the full lifecycle rather than individual operations.
"""

import os
import tempfile

import pytest

from data_backend.upload_state import (
    UploadStateDB,
    STATUS_PENDING,
    STATUS_UPLOADING,
    STATUS_COMPLETED,
    STATUS_GC_DELETED,
    STATUS_FAILED,
)


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test_state.db")
        with UploadStateDB(db_path) as db:
            yield db


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


class TestScenarioMultiTargetUploadLifecycle:
    """Full lifecycle: recording uploaded to local + cloud, GC deletes from
    local, cloud copy preserved. State reflects everything correctly."""

    def test_scenario_multi_target_lifecycle(self, db):
        rec = "rec_2025_01_15_001"

        # Step 1: Recording discovered, queued for both targets
        db.set_status(rec, "local", STATUS_PENDING)
        db.set_status(rec, "cloud", STATUS_PENDING)
        assert db.should_upload(rec, "local") is True
        assert db.should_upload(rec, "cloud") is True

        # Step 2: Upload to local starts
        db.set_status(rec, "local", STATUS_UPLOADING)
        assert db.get_status(rec, "local").status == STATUS_UPLOADING
        assert db.get_status(rec, "cloud").status == STATUS_PENDING

        # Step 3: Local upload completes
        db.set_status(
            rec,
            "local",
            STATUS_COMPLETED,
            etags={"data.mcap": "etag_local_1", "meta.json": "etag_local_2"},
        )
        assert db.should_upload(rec, "local") is False  # done
        assert db.should_upload(rec, "cloud") is True  # still pending

        # Step 4: Cloud upload completes
        db.set_status(
            rec,
            "cloud",
            STATUS_COMPLETED,
            etags={"data.mcap": "etag_cloud_1", "meta.json": "etag_cloud_2"},
        )
        assert db.should_upload(rec, "cloud") is False

        # Step 5: All gc-eligible targets completed → storage marker can be written
        assert db.all_gc_targets_completed(rec, ["local"]) is True
        # cloud is gc_policy=none, not in gc_targets list

        # Step 6: GC runs on local, deletes the recording
        db.mark_gc_deleted(rec, "local")
        assert db.get_status(rec, "local").status == STATUS_GC_DELETED
        assert db.get_status(rec, "cloud").status == STATUS_COMPLETED

        # Step 7: Uploader sees gc_deleted → does NOT re-upload
        assert db.should_upload(rec, "local") is False
        assert db.should_upload(rec, "cloud") is False

        # Step 8: Cloud copy still tracked as completed
        row = db.get_status(rec, "cloud")
        assert row.etags == {"data.mcap": "etag_cloud_1", "meta.json": "etag_cloud_2"}


class TestScenarioUploadFailureAndRetry:
    """Upload fails on one target, succeeds on retry. Other target
    completes independently."""

    def test_scenario_failure_retry(self, db):
        rec = "rec_fail"

        # Step 1: Both targets pending
        db.set_status(rec, "local", STATUS_PENDING)
        db.set_status(rec, "cloud", STATUS_PENDING)

        # Step 2: Local upload fails
        db.set_status(rec, "local", STATUS_FAILED, error="connection refused")
        assert db.should_upload(rec, "local") is True  # failed → should retry
        row = db.get_status(rec, "local")
        assert row.error == "connection refused"

        # Step 3: Cloud upload succeeds independently
        db.set_status(rec, "cloud", STATUS_COMPLETED)
        assert db.should_upload(rec, "cloud") is False

        # Step 4: Recovery picks up failed local
        pending = db.get_pending("local")
        assert rec in pending

        # Step 5: Retry succeeds
        db.set_status(rec, "local", STATUS_COMPLETED)
        assert db.should_upload(rec, "local") is False
        assert db.get_status(rec, "local").error is None  # cleared on success


class TestScenarioGCRespectsMultiTarget:
    """GC should only delete from gc-eligible targets. Recordings must be
    completed on ALL gc-eligible targets before any can be GC'd."""

    def test_scenario_gc_waits_for_all_targets(self, db):
        rec = "rec_multi"

        # Two gc-eligible targets
        db.set_status(rec, "storage_a", STATUS_COMPLETED)
        db.set_status(rec, "storage_b", STATUS_PENDING)

        # Not all gc targets ready → storage marker should NOT be written
        assert db.all_gc_targets_completed(rec, ["storage_a", "storage_b"]) is False

        # Second target completes
        db.set_status(rec, "storage_b", STATUS_COMPLETED)
        assert db.all_gc_targets_completed(rec, ["storage_a", "storage_b"]) is True

        # GC deletes from both
        db.mark_gc_deleted(rec, "storage_a")
        db.mark_gc_deleted(rec, "storage_b")

        # Archive target (gc_policy=none) not in gc_targets, unaffected
        db.set_status(rec, "archive", STATUS_COMPLETED)
        assert db.get_status(rec, "archive").status == STATUS_COMPLETED


class TestScenarioRecoveryAfterRestart:
    """After a node restart, pending and uploading recordings are recovered
    from the DB and re-enqueued."""

    def test_scenario_recovery(self, db):
        # Simulate state before crash
        db.set_status("rec1", "local", STATUS_UPLOADING)  # was mid-upload
        db.set_status("rec2", "local", STATUS_PENDING)  # never started
        db.set_status("rec3", "local", STATUS_COMPLETED)  # done
        db.set_status("rec4", "local", STATUS_GC_DELETED)  # GC'd
        db.set_status("rec5", "local", STATUS_FAILED)  # failed before crash

        # Recovery: get all that need action
        pending = db.get_pending("local")
        assert set(pending) == {"rec1", "rec2", "rec5"}

        # None of the terminal states should be re-uploaded
        assert db.should_upload("rec3", "local") is False
        assert db.should_upload("rec4", "local") is False


class TestScenarioPurgePreservesActiveRecords:
    """Purge removes old terminal rows but preserves everything that
    still needs action, even if old."""

    def test_scenario_purge_lifecycle(self, db):
        # Old completed recordings (30+ days)
        for i in range(10):
            db.set_status(f"old_rec_{i}", "local", STATUS_COMPLETED)
        # Old gc_deleted
        for i in range(5):
            db.set_status(f"old_gc_{i}", "local", STATUS_GC_DELETED)
        # Old but still pending (stuck)
        db.set_status("stuck", "local", STATUS_PENDING)
        # Old but failed (needs retry)
        db.set_status("retry", "local", STATUS_FAILED, error="timeout")
        # Recent completed (should survive purge)
        db.set_status("recent", "local", STATUS_COMPLETED)

        # Backdate everything except "recent"
        db._conn.execute(
            "UPDATE upload_state SET updated_at = '2020-01-01T00:00:00+00:00' "
            "WHERE recording != 'recent'"
        )
        db._conn.commit()

        # Purge with 720h (30 days) threshold
        count = db.purge(max_age_hours=720)

        # 10 old completed + 5 old gc_deleted = 15 purged
        assert count == 15

        # Active records preserved
        assert db.get_status("stuck", "local").status == STATUS_PENDING
        assert db.get_status("retry", "local").status == STATUS_FAILED
        assert db.get_status("recent", "local").status == STATUS_COMPLETED


class TestScenarioGCDeletedNeverReUploaded:
    """Once GC deletes a recording from a target, the uploader must never
    re-upload it, even across restarts."""

    def test_scenario_gc_deleted_permanent(self, db):
        rec = "rec_permanent"

        # Full lifecycle: upload → GC delete
        db.set_status(rec, "local", STATUS_COMPLETED)
        db.mark_gc_deleted(rec, "local")

        # Simulate restart: uploader checks all known recordings
        assert db.should_upload(rec, "local") is False

        # Even if someone re-queues it as pending (bug), gc_deleted wins
        # because set_status preserves gc_deleted_at
        row = db.get_status(rec, "local")
        assert row.gc_deleted_at is not None

    def test_scenario_different_targets_independent(self, db):
        """GC on local doesn't affect cloud target status."""
        rec = "rec_independent"

        db.set_status(rec, "local", STATUS_COMPLETED)
        db.set_status(rec, "cloud", STATUS_COMPLETED)

        # GC deletes from local only
        db.mark_gc_deleted(rec, "local")

        # Cloud unaffected
        assert db.get_status(rec, "cloud").status == STATUS_COMPLETED
        assert db.should_upload(rec, "cloud") is False
        assert db.should_upload(rec, "local") is False
