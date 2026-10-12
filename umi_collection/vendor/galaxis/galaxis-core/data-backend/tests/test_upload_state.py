"""Tests for upload_state SQLite state tracking."""

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


class TestUploadStateDB:
    def test_get_status_missing(self, db):
        assert db.get_status("rec1", "local") is None

    def test_set_and_get(self, db):
        db.set_status("rec1", "local", STATUS_PENDING)
        row = db.get_status("rec1", "local")
        assert row.status == STATUS_PENDING
        assert row.recording == "rec1"
        assert row.target == "local"

    def test_set_completed(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED, etags={"data.mcap": "abc123"})
        row = db.get_status("rec1", "local")
        assert row.status == STATUS_COMPLETED
        assert row.completed_at is not None
        assert row.etags == {"data.mcap": "abc123"}

    def test_mark_gc_deleted(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        db.mark_gc_deleted("rec1", "local")
        row = db.get_status("rec1", "local")
        assert row.status == STATUS_GC_DELETED
        assert row.gc_deleted_at is not None
        # completed_at preserved
        assert row.completed_at is not None

    def test_should_upload_missing(self, db):
        assert db.should_upload("rec1", "local") is True

    def test_should_upload_pending(self, db):
        db.set_status("rec1", "local", STATUS_PENDING)
        assert db.should_upload("rec1", "local") is True

    def test_should_upload_completed(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        assert db.should_upload("rec1", "local") is False

    def test_should_upload_gc_deleted(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        db.mark_gc_deleted("rec1", "local")
        assert db.should_upload("rec1", "local") is False

    def test_should_upload_failed(self, db):
        db.set_status("rec1", "local", STATUS_FAILED, error="timeout")
        assert db.should_upload("rec1", "local") is True

    def test_get_pending(self, db):
        db.set_status("rec1", "local", STATUS_PENDING)
        db.set_status("rec2", "local", STATUS_UPLOADING)
        db.set_status("rec3", "local", STATUS_COMPLETED)
        db.set_status("rec4", "local", STATUS_FAILED)
        db.set_status("rec5", "local", STATUS_GC_DELETED)
        pending = db.get_pending("local")
        assert set(pending) == {"rec1", "rec2", "rec4"}

    def test_get_completed(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        db.set_status("rec2", "local", STATUS_GC_DELETED)
        db.set_status("rec3", "local", STATUS_COMPLETED)
        completed = db.get_completed("local")
        assert set(completed) == {"rec1", "rec3"}

    def test_all_gc_targets_completed(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        db.set_status("rec1", "cloud", STATUS_COMPLETED)
        assert db.all_gc_targets_completed("rec1", ["local", "cloud"]) is True

    def test_all_gc_targets_not_completed(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        # cloud not set
        assert db.all_gc_targets_completed("rec1", ["local", "cloud"]) is False

    def test_all_gc_targets_empty_list(self, db):
        assert db.all_gc_targets_completed("rec1", []) is True

    def test_upsert_preserves_completed_at(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        first_completed = db.get_status("rec1", "local").completed_at
        db.set_status("rec1", "local", STATUS_FAILED, error="retry")
        row = db.get_status("rec1", "local")
        assert row.status == STATUS_FAILED
        assert row.completed_at == first_completed  # preserved

    def test_error_stored(self, db):
        db.set_status("rec1", "local", STATUS_FAILED, error="connection refused")
        row = db.get_status("rec1", "local")
        assert row.error == "connection refused"

    def test_multiple_targets_independent(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        db.set_status("rec1", "cloud", STATUS_PENDING)
        assert db.get_status("rec1", "local").status == STATUS_COMPLETED
        assert db.get_status("rec1", "cloud").status == STATUS_PENDING

    def test_db_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep_path = os.path.join(tmp, "a", "b", "c", "state.db")
            with UploadStateDB(deep_path) as db:
                db.set_status("rec1", "local", STATUS_PENDING)
                assert db.get_status("rec1", "local").status == STATUS_PENDING


class TestPurge:
    def test_purges_old_completed(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        # Backdate updated_at
        db._conn.execute(
            "UPDATE upload_state SET updated_at = '2020-01-01T00:00:00+00:00'"
        )
        db._conn.commit()
        count = db.purge(max_age_hours=1)
        assert count == 1
        assert db.get_status("rec1", "local") is None

    def test_purges_old_gc_deleted(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        db.mark_gc_deleted("rec1", "local")
        db._conn.execute(
            "UPDATE upload_state SET updated_at = '2020-01-01T00:00:00+00:00'"
        )
        db._conn.commit()
        count = db.purge(max_age_hours=1)
        assert count == 1

    def test_keeps_recent_completed(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        count = db.purge(max_age_hours=720)
        assert count == 0
        assert db.get_status("rec1", "local") is not None

    def test_keeps_pending_regardless_of_age(self, db):
        db.set_status("rec1", "local", STATUS_PENDING)
        db._conn.execute(
            "UPDATE upload_state SET updated_at = '2020-01-01T00:00:00+00:00'"
        )
        db._conn.commit()
        count = db.purge(max_age_hours=1)
        assert count == 0
        assert db.get_status("rec1", "local") is not None

    def test_keeps_failed_regardless_of_age(self, db):
        db.set_status("rec1", "local", STATUS_FAILED, error="timeout")
        db._conn.execute(
            "UPDATE upload_state SET updated_at = '2020-01-01T00:00:00+00:00'"
        )
        db._conn.commit()
        count = db.purge(max_age_hours=1)
        assert count == 0

    def test_disabled_when_zero(self, db):
        db.set_status("rec1", "local", STATUS_COMPLETED)
        db._conn.execute(
            "UPDATE upload_state SET updated_at = '2020-01-01T00:00:00+00:00'"
        )
        db._conn.commit()
        count = db.purge(max_age_hours=0)
        assert count == 0
        assert db.get_status("rec1", "local") is not None

    def test_returns_correct_count(self, db):
        for i in range(5):
            db.set_status(f"rec{i}", "local", STATUS_GC_DELETED)
        db.set_status("rec_keep", "local", STATUS_PENDING)
        db._conn.execute(
            "UPDATE upload_state SET updated_at = '2020-01-01T00:00:00+00:00'"
        )
        db._conn.commit()
        count = db.purge(max_age_hours=1)
        assert count == 5
        assert db.get_status("rec_keep", "local") is not None
