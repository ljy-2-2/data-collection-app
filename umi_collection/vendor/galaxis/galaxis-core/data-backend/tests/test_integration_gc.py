"""Integration tests against a real S3 instance.

Requires S3-compatible storage (MinIO) on localhost:19000
(see docker-compose.test.yml). Tests are marked
``@pytest.mark.integration`` and auto-skip if unreachable.

Run with:
    make test-storage
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest

from data_backend.config import GCConfig, parse_gc_strategy
from data_backend.gc import (
    discover_recordings,
    enrich_completed_at,
    run_gc_cycle_with_result,
)
from data_backend.upload import upload_recording

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENDPOINT = "localhost:19000"
_ACCESS_KEY = "testadmin"
_SECRET_KEY = "testadmin123"
_BUCKET = "test-gc-bucket"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def s3_client():
    """Connect to the test S3; skip entire module if unreachable."""
    from minio import Minio
    from urllib3.exceptions import MaxRetryError

    client = Minio(
        _ENDPOINT,
        access_key=_ACCESS_KEY,
        secret_key=_SECRET_KEY,
        secure=False,
    )
    try:
        client.list_buckets()
    except (MaxRetryError, Exception) as exc:
        pytest.skip(f"S3 not reachable at {_ENDPOINT}: {exc}")
    return client


@pytest.fixture()
def test_prefix(s3_client, request):
    """Unique S3 prefix per test, cleaned up after."""
    prefix = f"test_{request.node.name}_{uuid.uuid4().hex[:8]}/"
    yield prefix
    # Cleanup: remove all objects under this prefix
    objects = list(s3_client.list_objects(_BUCKET, prefix=prefix, recursive=True))
    for obj in objects:
        s3_client.remove_object(_BUCKET, obj.object_name)


@pytest.fixture()
def gc_config(test_prefix):
    """Return a GCConfig (no S3 connection details — those live outside now)."""
    return GCConfig(
        check_interval_sec=1,
        strategy=parse_gc_strategy(["marker", "age"]),
        max_storage_gb=0,
        max_age_hours=0,
        dry_run=False,
        storage_marker=".uploading_complete",
        completion_marker=".recording_complete",
        orphan_age_hours=1,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upload_fake_recording(
    s3_client,
    prefix,
    rec_name,
    file_sizes,
    with_marker=True,
    marker_content=None,
    with_uploading_complete=False,
):
    """Upload a fake recording directly to S3.

    Args:
        s3_client: S3 client.
        prefix: S3 prefix (e.g. "test_xxx/").
        rec_name: Recording directory name.
        file_sizes: dict of filename -> size in bytes.
        with_marker: If True, write a .recording_complete marker.
        marker_content: Custom marker JSON dict. Auto-generated if None.
        with_uploading_complete: If True, write an .uploading_complete marker.
    """
    for filename, size in file_sizes.items():
        key = f"{prefix}{rec_name}/{filename}"
        data = b"\x00" * size
        s3_client.put_object(_BUCKET, key, BytesIO(data), size)

    if with_marker:
        content = marker_content or {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "files": {name: "fake-etag" for name in file_sizes},
        }
        body = json.dumps(content).encode("utf-8")
        marker_key = f"{prefix}{rec_name}/.recording_complete"
        s3_client.put_object(
            _BUCKET,
            marker_key,
            BytesIO(body),
            len(body),
            content_type="application/json",
        )

    if with_uploading_complete:
        marker_key = f"{prefix}{rec_name}/.uploading_complete"
        body = b"{}"
        s3_client.put_object(_BUCKET, marker_key, BytesIO(body), len(body))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUploadAndDiscover:
    """upload_recording() -> discover_recordings() round-trip."""

    def test_upload_and_discover(self, s3_client, gc_config, test_prefix, tmp_path):
        # Create a local recording directory
        rec_dir = tmp_path / "rec_upload_test"
        rec_dir.mkdir()
        (rec_dir / "meta.json").write_text('{"task": "integration"}')
        (rec_dir / "data_0.mcap").write_bytes(b"\x00" * 256)

        # Upload using the extracted pure-Python function
        etags = upload_recording(s3_client, _BUCKET, test_prefix, str(rec_dir))

        assert "meta.json" in etags
        assert "data_0.mcap" in etags

        # Discover via data_backend.gc
        recordings = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        assert len(recordings) == 1
        rec = recordings[0]
        assert rec.prefix == f"{test_prefix}rec_upload_test/"

        # 2 data files + .recording_complete marker = 3 objects
        assert len(rec.objects) == 3
        assert rec.total_size_bytes > 0


class TestEnrichReadsMarker:
    """Upload with .recording_complete JSON -> enrich parses timestamp."""

    def test_enrich_reads_marker(self, s3_client, gc_config, test_prefix):
        ts = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        _upload_fake_recording(
            s3_client,
            test_prefix,
            "rec_enrich",
            {"data.mcap": 100},
            marker_content={
                "completed_at": ts.isoformat(),
                "files": {"data.mcap": "etag"},
            },
        )

        recordings = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        enrich_completed_at(s3_client, _BUCKET, gc_config, recordings)

        assert len(recordings) == 1
        assert recordings[0].completed_at == ts


class TestEnrichFallbackNoMarker:
    """Upload without .recording_complete -> falls back to latest_object_modified."""

    def test_enrich_fallback_no_marker(self, s3_client, gc_config, test_prefix):
        _upload_fake_recording(
            s3_client,
            test_prefix,
            "rec_no_marker",
            {"data.mcap": 100},
            with_marker=False,
        )

        recordings = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        assert len(recordings) == 1
        assert recordings[0].completed_at is None

        enrich_completed_at(s3_client, _BUCKET, gc_config, recordings)
        # Should fall back to latest_object_modified
        assert recordings[0].completed_at is not None
        assert recordings[0].completed_at == recordings[0].latest_object_modified


class TestGcCycleDeletesEligible:
    """3 recordings (2 eligible, 1 not) -> age strategy -> verify deleted."""

    def test_gc_cycle_deletes_eligible(self, s3_client, gc_config, test_prefix):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

        # 2 old eligible recordings (have .uploading_complete + old timestamp)
        for name in ("rec_old_1", "rec_old_2"):
            _upload_fake_recording(
                s3_client,
                test_prefix,
                name,
                {"data.mcap": 64},
                marker_content={"completed_at": old_ts, "files": {}},
                with_uploading_complete=True,
            )

        # 1 recent ineligible recording (no .uploading_complete)
        _upload_fake_recording(
            s3_client,
            test_prefix,
            "rec_recent",
            {"data.mcap": 64},
            with_uploading_complete=False,
        )

        cfg = GCConfig(
            check_interval_sec=1,
            strategy=parse_gc_strategy(["marker", "age"]),
            max_storage_gb=0,
            max_age_hours=1,  # anything older than 1h
            dry_run=False,
            storage_marker=".uploading_complete",
            completion_marker=".recording_complete",
            orphan_age_hours=0,
        )

        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)
        assert result.total_recordings == 3
        assert result.eligible_count == 2
        assert result.deleted_count > 0
        assert result.error is None

        # Verify old recordings are gone
        remaining = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        remaining_prefixes = {r.prefix for r in remaining}
        assert f"{test_prefix}rec_old_1/" not in remaining_prefixes
        assert f"{test_prefix}rec_old_2/" not in remaining_prefixes
        # Recent one still exists
        assert f"{test_prefix}rec_recent/" in remaining_prefixes


class TestGcCycleDryRun:
    """Same setup as above, dry_run=True -> all objects still present."""

    def test_gc_cycle_dry_run(self, s3_client, gc_config, test_prefix):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

        _upload_fake_recording(
            s3_client,
            test_prefix,
            "rec_dry_1",
            {"data.mcap": 64},
            marker_content={"completed_at": old_ts, "files": {}},
            with_uploading_complete=True,
        )
        _upload_fake_recording(
            s3_client,
            test_prefix,
            "rec_dry_2",
            {"data.mcap": 64},
            marker_content={"completed_at": old_ts, "files": {}},
            with_uploading_complete=True,
        )

        cfg = GCConfig(
            check_interval_sec=1,
            strategy=parse_gc_strategy(["marker", "age"]),
            max_storage_gb=0,
            max_age_hours=1,
            dry_run=True,
            storage_marker=".uploading_complete",
            completion_marker=".recording_complete",
            orphan_age_hours=0,
        )

        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)
        assert result.deleted_count == 0

        # All objects still present
        remaining = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        assert len(remaining) == 2


class TestMarkerJsonRoundtrip:
    """Upload .recording_complete -> read back -> verify JSON structure."""

    def test_marker_json_roundtrip(self, s3_client, gc_config, test_prefix, tmp_path):
        rec_dir = tmp_path / "rec_marker_rt"
        rec_dir.mkdir()
        (rec_dir / "meta.json").write_text('{"task": "roundtrip"}')
        (rec_dir / "data_0.mcap").write_bytes(b"\x00" * 128)

        upload_recording(s3_client, _BUCKET, test_prefix, str(rec_dir))

        # Read back the marker
        marker_key = f"{test_prefix}rec_marker_rt/.recording_complete"
        response = s3_client.get_object(_BUCKET, marker_key)
        data = json.loads(response.read())
        response.close()
        response.release_conn()

        assert "completed_at" in data
        assert "files" in data
        assert "meta.json" in data["files"]
        assert "data_0.mcap" in data["files"]
        # Verify timestamp is valid ISO format
        datetime.fromisoformat(data["completed_at"])


class TestSpaceStrategyReal:
    """3 recordings of known sizes -> space strategy -> correct ones deleted."""

    def test_space_strategy_real(self, s3_client, gc_config, test_prefix):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

        # Create 3 recordings: 500B, 300B, 200B (all eligible)
        _upload_fake_recording(
            s3_client,
            test_prefix,
            "rec_large",
            {"data.mcap": 500},
            marker_content={"completed_at": old_ts, "files": {}},
            with_uploading_complete=True,
        )
        slightly_newer = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        _upload_fake_recording(
            s3_client,
            test_prefix,
            "rec_medium",
            {"data.mcap": 300},
            marker_content={"completed_at": slightly_newer, "files": {}},
            with_uploading_complete=True,
        )
        newest = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _upload_fake_recording(
            s3_client,
            test_prefix,
            "rec_small",
            {"data.mcap": 200},
            marker_content={"completed_at": newest, "files": {}},
            with_uploading_complete=True,
        )

        # Discover to calculate total size (includes markers + data)
        recordings = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        total_bytes = sum(r.total_size_bytes for r in recordings)

        # Set threshold so we need to delete at least the oldest to get under
        # Total is ~1000B of data + markers. Set threshold to keep ~500B.
        threshold_gb = (total_bytes * 0.5) / 1_000_000_000

        cfg = GCConfig(
            check_interval_sec=1,
            strategy=parse_gc_strategy(["marker", "space"]),
            max_storage_gb=threshold_gb,
            max_age_hours=0,
            dry_run=False,
            storage_marker=".uploading_complete",
            completion_marker=".recording_complete",
            orphan_age_hours=0,
        )

        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)
        assert result.deleted_count > 0
        assert result.error is None

        # The oldest (rec_large) should be deleted first
        remaining = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        remaining_prefixes = {r.prefix for r in remaining}
        assert f"{test_prefix}rec_large/" not in remaining_prefixes
        # The smallest/newest should still exist
        assert f"{test_prefix}rec_small/" in remaining_prefixes
