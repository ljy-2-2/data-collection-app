"""Tests for StorageManager — upload, GC, deletion policy, retention."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from data_backend.config import (
    GCConfig,
    PathRule,
    Priority,
    StorageConfig,
    TargetConfig,
    parse_gc_strategy,
)
from data_backend.manager import StorageManager
from data_backend.upload_state import (
    STATUS_COMPLETED,
    STATUS_FAILED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_target(name="local", priority="required", gc=None, **kw):
    return TargetConfig(
        name=name,
        endpoint=kw.get("endpoint", "localhost:9000"),
        access_key=kw.get("access_key", "admin"),
        secret_key=kw.get("secret_key", "secret"),
        use_ssl=kw.get("use_ssl", False),
        bucket=kw.get("bucket", "data"),
        prefix=kw.get("prefix", ""),
        path_rule=PathRule(kw.get("path_rule", "flat")),
        priority=Priority(priority),
        gc=gc,
    )


def _make_config(targets, tmp_path, delete_after=False, retention_hours=24):
    db_path = str(tmp_path / "state.db")
    return StorageConfig(
        targets=targets,
        state_db=db_path,
        delete_after_upload=delete_after,
        local_retention_hours=retention_hours,
    )


def _make_manager(targets, tmp_path, delete_after=False, retention_hours=24):
    """Create a StorageManager with mocked S3 clients."""
    cfg = _make_config(targets, tmp_path, delete_after, retention_hours)
    with patch("data_backend.manager.Minio") as mock_minio:
        mock_client = MagicMock()
        mock_minio.return_value = mock_client
        mgr = StorageManager(cfg)
    # Patch all clients to the same mock
    for lt in mgr.targets:
        lt.client = MagicMock()
    return mgr


def _setup_recording(tmp_path, name="rec1"):
    rec = tmp_path / name
    rec.mkdir()
    (rec / "data.mcap").write_bytes(b"\x00" * 100)
    (rec / "meta.json").write_text('{"schema_version": "2.0"}')
    return str(rec)


def _mock_upload_success(client):
    """Make fput_object return a mock result with etag."""
    client.fput_object.return_value = SimpleNamespace(etag="abc123")
    client.put_object.return_value = None


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


class TestUpload:
    def test_upload_to_single_target(self, tmp_path):
        mgr = _make_manager([_make_target("local")], tmp_path)
        _mock_upload_success(mgr.targets[0].client)

        dir_path = _setup_recording(tmp_path)
        result = mgr.upload(dir_path)

        assert result.all_ok
        assert "local" in result.completed_targets
        assert mgr.targets[0].client.fput_object.called

    def test_upload_to_multiple_targets(self, tmp_path):
        mgr = _make_manager(
            [_make_target("local"), _make_target("heap", priority="preferred")],
            tmp_path,
        )
        for lt in mgr.targets:
            _mock_upload_success(lt.client)

        dir_path = _setup_recording(tmp_path)
        result = mgr.upload(dir_path)

        assert result.all_ok
        assert "local" in result.completed_targets
        assert "heap" in result.completed_targets

    def test_upload_failure_marks_failed(self, tmp_path):
        mgr = _make_manager([_make_target("local")], tmp_path)
        mgr.targets[0].client.fput_object.side_effect = Exception("S3 down")

        dir_path = _setup_recording(tmp_path)
        result = mgr.upload(dir_path)

        assert not result.all_ok
        assert "local" in result.failed_targets
        status = mgr.state_db.get_status("rec1", "local")
        assert status.status == STATUS_FAILED

    def test_skips_already_completed(self, tmp_path):
        mgr = _make_manager([_make_target("local")], tmp_path)
        mgr.state_db.set_status("rec1", "local", STATUS_COMPLETED)

        dir_path = _setup_recording(tmp_path)
        result = mgr.upload(dir_path)

        assert result.all_ok
        assert "local" in result.completed_targets
        assert not mgr.targets[0].client.fput_object.called

    def test_partial_failure_multi_target(self, tmp_path):
        mgr = _make_manager(
            [_make_target("local"), _make_target("heap", priority="preferred")],
            tmp_path,
        )
        _mock_upload_success(mgr.targets[0].client)
        mgr.targets[1].client.fput_object.side_effect = Exception("timeout")

        dir_path = _setup_recording(tmp_path)
        result = mgr.upload(dir_path)

        assert not result.all_ok
        assert "local" in result.completed_targets
        assert "heap" in result.failed_targets

    def test_optional_failure_no_state_db_entry(self, tmp_path):
        mgr = _make_manager(
            [_make_target("local"), _make_target("cloud", priority="optional")],
            tmp_path,
        )
        _mock_upload_success(mgr.targets[0].client)
        mgr.targets[1].client.fput_object.side_effect = Exception("timeout")

        dir_path = _setup_recording(tmp_path)
        result = mgr.upload(dir_path)

        # local succeeded, cloud failed but it's optional
        assert "local" in result.completed_targets
        assert "cloud" in result.failed_targets

    def test_nonexistent_dir_returns_failure(self, tmp_path):
        mgr = _make_manager([_make_target("local")], tmp_path)
        result = mgr.upload(str(tmp_path / "nonexistent"))
        assert not result.all_ok


# ---------------------------------------------------------------------------
# Deletion policy
# ---------------------------------------------------------------------------


class TestCanDeleteLocal:
    def test_disabled_when_delete_after_false(self, tmp_path):
        mgr = _make_manager(
            [_make_target("a", priority="required")],
            tmp_path,
            delete_after=False,
        )
        mgr.state_db.set_status("rec1", "a", STATUS_COMPLETED)
        assert mgr.can_delete_local("rec1") is False

    def test_all_required_completed(self, tmp_path):
        mgr = _make_manager(
            [
                _make_target("a", priority="required"),
                _make_target("b", priority="required"),
            ],
            tmp_path,
            delete_after=True,
        )
        mgr.state_db.set_status("rec1", "a", STATUS_COMPLETED)
        mgr.state_db.set_status("rec1", "b", STATUS_COMPLETED)
        assert mgr.can_delete_local("rec1") is True

    def test_required_not_completed_blocks(self, tmp_path):
        mgr = _make_manager(
            [
                _make_target("a", priority="required"),
                _make_target("b", priority="required"),
            ],
            tmp_path,
            delete_after=True,
        )
        mgr.state_db.set_status("rec1", "a", STATUS_COMPLETED)
        # b not uploaded yet
        assert mgr.can_delete_local("rec1") is False

    def test_no_required_any_preferred_ok(self, tmp_path):
        mgr = _make_manager(
            [
                _make_target("a", priority="preferred"),
                _make_target("b", priority="preferred"),
            ],
            tmp_path,
            delete_after=True,
        )
        mgr.state_db.set_status("rec1", "a", STATUS_COMPLETED)
        # b not done, but a preferred succeeded
        assert mgr.can_delete_local("rec1") is True

    def test_only_optional_always_ok(self, tmp_path):
        mgr = _make_manager(
            [_make_target("a", priority="optional")],
            tmp_path,
            delete_after=True,
        )
        assert mgr.can_delete_local("rec1") is True

    def test_required_plus_preferred(self, tmp_path):
        mgr = _make_manager(
            [
                _make_target("a", priority="required"),
                _make_target("b", priority="preferred"),
            ],
            tmp_path,
            delete_after=True,
        )
        # required done, preferred not
        mgr.state_db.set_status("rec1", "a", STATUS_COMPLETED)
        assert mgr.can_delete_local("rec1") is True  # only required matters


# ---------------------------------------------------------------------------
# Needs retry
# ---------------------------------------------------------------------------


class TestNeedsRetry:
    def test_required_pending(self, tmp_path):
        mgr = _make_manager([_make_target("a", priority="required")], tmp_path)
        assert mgr.needs_retry("rec1") is True

    def test_required_completed(self, tmp_path):
        mgr = _make_manager([_make_target("a", priority="required")], tmp_path)
        mgr.state_db.set_status("rec1", "a", STATUS_COMPLETED)
        assert mgr.needs_retry("rec1") is False

    def test_optional_never_retries(self, tmp_path):
        mgr = _make_manager([_make_target("a", priority="optional")], tmp_path)
        assert mgr.needs_retry("rec1") is False


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------


class TestGC:
    def _gc_target(self, name="local"):
        return _make_target(
            name,
            gc=GCConfig(
                strategy=parse_gc_strategy(["marker", "age"]),
                max_age_hours=24.0,
            ),
        )

    def test_run_gc_all_targets(self, tmp_path):
        mgr = _make_manager(
            [self._gc_target("local"), _make_target("heap", priority="preferred")],
            tmp_path,
        )
        # Mock list_objects to return empty
        mgr.targets[0].client.list_objects.return_value = []

        results = mgr.run_gc()
        assert "local" in results
        assert "heap" not in results  # no gc config

    def test_run_gc_single_target(self, tmp_path):
        mgr = _make_manager(
            [self._gc_target("local"), self._gc_target("staging")],
            tmp_path,
        )
        for lt in mgr.targets:
            lt.client.list_objects.return_value = []

        results = mgr.run_gc("local")
        assert "local" in results
        assert "staging" not in results

    def test_gc_diagnostic_level(self, tmp_path):
        mgr = _make_manager([self._gc_target("local")], tmp_path)
        from data_backend.gc import GCCycleResult

        result = GCCycleResult()
        level = mgr.gc_diagnostic_level("local", result)
        assert level == 0  # OK


# ---------------------------------------------------------------------------
# Recovery & retention
# ---------------------------------------------------------------------------


class TestRecoveryAndRetention:
    def test_recover_pending(self, tmp_path):
        rec = tmp_path / "rec1"
        rec.mkdir()
        (rec / "meta.json").write_text("{}")

        mgr = _make_manager([_make_target("local")], tmp_path)
        pending = mgr.recover_pending(str(tmp_path))
        assert len(pending) == 1

    def test_retention_cleanup(self, tmp_path):
        old = tmp_path / "old_rec"
        old.mkdir()
        (old / "meta.json").write_text("{}")
        # Make it old
        import time

        old_time = time.time() - 48 * 3600
        os.utime(str(old), (old_time, old_time))

        mgr = _make_manager([_make_target("local")], tmp_path, retention_hours=24)
        deleted = mgr.retention_cleanup(str(tmp_path))
        assert str(old) in deleted

    def test_purge_state(self, tmp_path):
        cfg = StorageConfig(
            targets=[_make_target("local")],
            state_db=str(tmp_path / "state.db"),
            state_purge_age_hours=0,  # disabled
        )
        with patch("data_backend.manager.Minio"):
            mgr = StorageManager(cfg)
        assert mgr.purge_state() == 0
