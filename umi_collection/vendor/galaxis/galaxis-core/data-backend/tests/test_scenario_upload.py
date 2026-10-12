"""Scenario tests for upload with path_rule and GC interplay.

Multi-step workflows verifying that canonical/flat uploads produce
S3 structures that GC can discover and process correctly.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from data_backend.config import GCConfig, parse_gc_strategy
from data_backend.gc import discover_recordings
from data_backend.upload import upload_recording

# ---------------------------------------------------------------------------
# Helpers
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


def _make_mock_client():
    client = MagicMock()
    client.fput_object.return_value = SimpleNamespace(etag="etag-1")
    client.remove_objects.return_value = iter([])
    return client


def _make_s3_object(name, size=100, last_modified=None, is_dir=False):
    obj = SimpleNamespace()
    obj.object_name = name
    obj.size = size
    obj.last_modified = last_modified
    obj.is_dir = is_dir
    return obj


def _collect_uploaded_keys(client):
    """Extract all S3 keys from fput_object + put_object calls."""
    keys = [c[0][1] for c in client.fput_object.call_args_list]
    if client.put_object.call_args:
        keys.append(client.put_object.call_args[0][1])
    return keys


def _make_gc_config(**overrides):
    defaults = dict(
        strategy=parse_gc_strategy(["marker", "age"]),
        max_age_hours=24,
        max_storage_gb=0,
        orphan_age_hours=1,
    )
    defaults.update(overrides)
    return GCConfig(**defaults)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


class TestScenarioCanonicalUploadGcDiscovers:
    """Upload with canonical path → GC discovers recording with correct markers."""

    def test_canonical_upload_then_gc_discovers(self, tmp_path):
        rec = tmp_path / "rec1"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 100)
        (rec / "meta.json").write_text(_SAMPLE_META)

        # Step 1: Upload with canonical path
        client = _make_mock_client()
        upload_recording(client, "data", "", str(rec), path_rule="canonical")
        uploaded_keys = _collect_uploaded_keys(client)

        # Step 2: Simulate GC discovering these objects
        now = datetime.now(timezone.utc)
        s3_objects = []
        for key in uploaded_keys:
            s3_objects.append(_make_s3_object(key, 100, last_modified=now))

        gc_client = MagicMock()
        gc_client.list_objects.return_value = s3_objects

        cfg = _make_gc_config()
        recordings = discover_recordings(gc_client, "data", "", cfg)

        # Step 3: Verify GC found exactly one recording under canonical prefix
        assert len(recordings) == 1
        rec_info = recordings[0]
        assert "org=airoa/" in rec_info.prefix
        assert "uuid=abc-123" in rec_info.prefix
        assert rec_info.completion_marker_exists is True


class TestScenarioFlatVsCanonicalDifferentKeys:
    """Same local dir uploaded with flat vs canonical → different S3 keys."""

    def test_flat_and_canonical_produce_different_keys(self, tmp_path):
        rec = tmp_path / "my_recording"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 50)
        (rec / "meta.json").write_text(_SAMPLE_META)

        # Flat upload
        flat_client = _make_mock_client()
        upload_recording(flat_client, "bucket", "pfx/", str(rec), path_rule="flat")
        flat_keys = _collect_uploaded_keys(flat_client)

        # Canonical upload
        canon_client = _make_mock_client()
        upload_recording(
            canon_client, "bucket", "pfx/", str(rec), path_rule="canonical"
        )
        canon_keys = _collect_uploaded_keys(canon_client)

        # Keys differ
        assert flat_keys != canon_keys

        # Flat uses dir name
        assert any("my_recording/" in k for k in flat_keys)

        # Canonical uses metadata
        assert any("org=airoa/" in k for k in canon_keys)
        assert any("uuid=abc-123" in k for k in canon_keys)

        # Both uploaded the same file
        assert any(k.endswith("data.mcap") for k in flat_keys)
        assert any(k.endswith("data.mcap") for k in canon_keys)


class TestScenarioCanonicalFallbackGcStillWorks:
    """meta.json missing → canonical falls back to flat → GC discovers normally."""

    def test_fallback_to_flat_gc_compatible(self, tmp_path):
        rec = tmp_path / "rec_no_meta"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 100)
        # No meta.json — canonical will fall back to flat

        # Step 1: Upload with canonical (falls back)
        client = _make_mock_client()
        upload_recording(client, "data", "pfx/", str(rec), path_rule="canonical")
        uploaded_keys = _collect_uploaded_keys(client)

        # Verify flat path was used
        assert any("pfx/rec_no_meta/" in k for k in uploaded_keys)

        # Step 2: GC discovers it normally
        now = datetime.now(timezone.utc)
        s3_objects = [_make_s3_object(k, 100, last_modified=now) for k in uploaded_keys]

        gc_client = MagicMock()
        gc_client.list_objects.return_value = s3_objects

        cfg = _make_gc_config()
        recordings = discover_recordings(gc_client, "data", "pfx/", cfg)

        assert len(recordings) == 1
        assert recordings[0].prefix == "pfx/rec_no_meta/"
        assert recordings[0].completion_marker_exists is True
