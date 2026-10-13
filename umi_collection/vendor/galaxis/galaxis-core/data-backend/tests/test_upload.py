"""Tests for pure-Python upload, recovery, and retention logic."""

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from data_backend.upload import (
    _resolve_s3_prefix,
    recover_pending_uploads,
    retention_cleanup,
    upload_recording,
)


# ---------------------------------------------------------------------------
# recover_pending_uploads
# ---------------------------------------------------------------------------


class TestRecoverPendingUploads:
    def test_finds_pending(self, tmp_path):
        rec = tmp_path / "rec1"
        rec.mkdir()
        (rec / "meta.json").write_text("{}")

        result = recover_pending_uploads(str(tmp_path))
        assert result == [str(rec)]

    def test_skips_without_meta(self, tmp_path):
        rec = tmp_path / "rec1"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00")

        result = recover_pending_uploads(str(tmp_path))
        assert result == []

    def test_returns_empty_for_nonexistent_dir(self):
        result = recover_pending_uploads("/nonexistent/path")
        assert result == []

    def test_returns_empty_for_empty_dir(self, tmp_path):
        result = recover_pending_uploads(str(tmp_path))
        assert result == []

    def test_skips_files_not_dirs(self, tmp_path):
        (tmp_path / "not_a_dir.txt").write_text("hello")
        result = recover_pending_uploads(str(tmp_path))
        assert result == []

    def test_multiple_pending(self, tmp_path):
        for name in ("rec_a", "rec_b", "rec_c"):
            d = tmp_path / name
            d.mkdir()
            (d / "meta.json").write_text("{}")

        result = recover_pending_uploads(str(tmp_path))
        basenames = sorted(os.path.basename(p) for p in result)
        assert basenames == ["rec_a", "rec_b", "rec_c"]


# ---------------------------------------------------------------------------
# retention_cleanup
# ---------------------------------------------------------------------------


class TestRetentionCleanup:
    def test_deletes_old_dir(self, tmp_path):
        rec = tmp_path / "rec_old"
        rec.mkdir()
        (rec / "meta.json").write_text("{}")
        # Backdate dir mtime so it's older than retention
        old_time = time.time() - 48 * 3600
        os.utime(str(rec), (old_time, old_time))

        deleted = retention_cleanup(
            str(tmp_path), retention_hours=24, delete_after_upload=False
        )
        assert str(rec) in deleted
        assert not rec.exists()

    def test_keeps_recent_dir(self, tmp_path):
        rec = tmp_path / "rec_recent"
        rec.mkdir()
        (rec / "meta.json").write_text("{}")
        # mtime is "now" by default — within retention

        deleted = retention_cleanup(
            str(tmp_path), retention_hours=24, delete_after_upload=False
        )
        assert deleted == []
        assert rec.exists()

    def test_noop_when_delete_after_upload(self, tmp_path):
        rec = tmp_path / "rec1"
        rec.mkdir()

        deleted = retention_cleanup(
            str(tmp_path), retention_hours=24, delete_after_upload=True
        )
        assert deleted == []

    def test_noop_when_retention_zero(self, tmp_path):
        rec = tmp_path / "rec1"
        rec.mkdir()

        deleted = retention_cleanup(
            str(tmp_path), retention_hours=0, delete_after_upload=False
        )
        assert deleted == []

    def test_noop_for_nonexistent_dir(self):
        deleted = retention_cleanup(
            "/nonexistent", retention_hours=24, delete_after_upload=False
        )
        assert deleted == []

    def test_skips_files_not_dirs(self, tmp_path):
        (tmp_path / "not_a_dir.txt").write_text("hello")

        deleted = retention_cleanup(
            str(tmp_path), retention_hours=24, delete_after_upload=False
        )
        assert deleted == []


# ---------------------------------------------------------------------------
# _resolve_s3_prefix
# ---------------------------------------------------------------------------

_SAMPLE_META = json.dumps(
    {
        "schema_version": "2.0",
        "uuid": "abc-123",
        "robot": {"type": "umi", "id": "robot-01"},
        "environment": {"type": "real_world", "site": "tokyo", "location": "lab"},
        "runner": {"type": "operator", "organization": "airoa", "name": "op"},
        "episode": {"start_time": 0.0, "end_time": 1.0, "success": True, "label": "a"},
        "files": [],
    }
)


class TestResolveS3Prefix:
    def test_flat_uses_dir_name(self, tmp_path):
        rec = tmp_path / "my_rec"
        rec.mkdir()
        result = _resolve_s3_prefix("data/", str(rec), "flat")
        assert result == "data/my_rec/"

    def test_flat_is_default_for_unknown_rule(self, tmp_path):
        rec = tmp_path / "my_rec"
        rec.mkdir()
        result = _resolve_s3_prefix("", str(rec), "something_else")
        assert result == "my_rec/"

    def test_canonical_uses_metadata(self, tmp_path):
        rec = tmp_path / "my_rec"
        rec.mkdir()
        (rec / "meta.json").write_text(_SAMPLE_META)

        result = _resolve_s3_prefix("uploads/", str(rec), "canonical")
        assert result.startswith("uploads/org=airoa/")
        assert "uuid=abc-123" in result
        assert result.endswith("/")

    def test_canonical_falls_back_on_missing_meta(self, tmp_path):
        rec = tmp_path / "my_rec"
        rec.mkdir()
        # No meta.json
        result = _resolve_s3_prefix("", str(rec), "canonical")
        assert result == "my_rec/"

    def test_canonical_falls_back_on_bad_json(self, tmp_path):
        rec = tmp_path / "my_rec"
        rec.mkdir()
        (rec / "meta.json").write_text("not json")

        result = _resolve_s3_prefix("", str(rec), "canonical")
        assert result == "my_rec/"

    def test_canonical_falls_back_on_invalid_timestamp(self, tmp_path):
        rec = tmp_path / "my_rec"
        rec.mkdir()
        (rec / "meta.json").write_text(
            json.dumps(
                {
                    "schema_version": "2.0",
                    "uuid": "abc",
                    "episode": {"start_time": "not-a-number"},
                    "robot": {},
                    "environment": {},
                    "runner": {},
                    "files": [],
                }
            )
        )
        result = _resolve_s3_prefix("", str(rec), "canonical")
        assert result == "my_rec/"

    def test_canonical_falls_back_on_missing_uuid(self, tmp_path):
        rec = tmp_path / "my_rec"
        rec.mkdir()
        (rec / "meta.json").write_text(
            json.dumps(
                {
                    "schema_version": "2.0",
                    "robot": {},
                    "environment": {},
                    "runner": {},
                    "episode": {},
                    "files": [],
                }
            )
        )
        result = _resolve_s3_prefix("", str(rec), "canonical")
        assert result == "my_rec/"


# ---------------------------------------------------------------------------
# upload_recording with path_rule
# ---------------------------------------------------------------------------


class TestUploadRecordingPathRule:
    def _make_mock_client(self):
        client = MagicMock()
        client.fput_object.return_value = SimpleNamespace(etag="etag-1")
        return client

    def test_flat_default(self, tmp_path):
        rec = tmp_path / "rec1"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 10)

        client = self._make_mock_client()
        upload_recording(client, "bucket", "pfx/", str(rec))

        call_args = client.fput_object.call_args
        assert call_args[0][1] == "pfx/rec1/data.mcap"

    def test_canonical_path_rule(self, tmp_path):
        rec = tmp_path / "rec1"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 10)
        (rec / "meta.json").write_text(_SAMPLE_META)

        client = self._make_mock_client()
        upload_recording(client, "bucket", "", str(rec), path_rule="canonical")

        uploaded_keys = [c[0][1] for c in client.fput_object.call_args_list]
        assert any("org=airoa/" in k for k in uploaded_keys)
        assert any(k.endswith("data.mcap") for k in uploaded_keys)

    def test_canonical_marker_uses_same_prefix(self, tmp_path):
        rec = tmp_path / "rec1"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 10)
        (rec / "meta.json").write_text(_SAMPLE_META)

        client = self._make_mock_client()
        upload_recording(client, "bucket", "", str(rec), path_rule="canonical")

        # put_object is called for the .recording_complete marker
        marker_call = client.put_object.call_args
        marker_key = marker_call[0][1]
        assert "org=airoa/" in marker_key
        assert marker_key.endswith(".recording_complete")

    def test_canonical_fallback_uploads_flat(self, tmp_path):
        """Missing meta.json → falls back to flat path, upload still succeeds."""
        rec = tmp_path / "rec_no_meta"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 10)

        client = self._make_mock_client()
        etags = upload_recording(
            client, "bucket", "pfx/", str(rec), path_rule="canonical"
        )

        assert len(etags) == 1
        call_args = client.fput_object.call_args
        assert call_args[0][1] == "pfx/rec_no_meta/data.mcap"

    def test_canonical_with_prefix(self, tmp_path):
        """Canonical path is prepended with the target prefix."""
        rec = tmp_path / "rec1"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 10)
        (rec / "meta.json").write_text(_SAMPLE_META)

        client = self._make_mock_client()
        upload_recording(client, "bucket", "archive/", str(rec), path_rule="canonical")

        uploaded_keys = [c[0][1] for c in client.fput_object.call_args_list]
        assert all(k.startswith("archive/org=airoa/") for k in uploaded_keys)
