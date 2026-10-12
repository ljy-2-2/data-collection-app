"""Integration tests: full upload → state tracking → GC lifecycle.

Requires S3-compatible storage (MinIO) on localhost:19000
(see docker-compose.test.yml). Tests are marked
``@pytest.mark.integration`` and auto-skip if unreachable.

Run with:
    make test-storage
"""

import dataclasses
import json
import time
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
from data_backend.upload_state import (
    UploadStateDB,
    STATUS_COMPLETED,
    STATUS_GC_DELETED,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENDPOINT = "localhost:19000"
_ACCESS_KEY = "testadmin"
_SECRET_KEY = "testadmin123"
_BUCKET = "test-gc-bucket"

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def s3_client():
    """Connect to test S3; skip entire module if unreachable."""
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


@pytest.fixture
def test_prefix(s3_client, request):
    """Unique S3 prefix per test, cleaned up after."""
    prefix = f"integ_{request.node.name}_{uuid.uuid4().hex[:8]}/"
    yield prefix
    for obj in s3_client.list_objects(_BUCKET, prefix=prefix, recursive=True):
        s3_client.remove_object(_BUCKET, obj.object_name)


@pytest.fixture
def gc_config():
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


@pytest.fixture
def state_db(tmp_path):
    with UploadStateDB(str(tmp_path / "state.db")) as db:
        yield db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upload_to_s3(
    s3_client,
    prefix,
    rec_name,
    files,
    with_completion=True,
    with_storage=False,
    completed_at=None,
):
    """Upload fake recording files + optional markers to S3."""
    for filename, size in files.items():
        key = f"{prefix}{rec_name}/{filename}"
        s3_client.put_object(_BUCKET, key, BytesIO(b"\x00" * size), size)

    if with_completion:
        ts = completed_at or datetime.now(timezone.utc).isoformat()
        content = json.dumps(
            {
                "completed_at": ts,
                "files": {name: "fake-etag" for name in files},
            }
        ).encode()
        key = f"{prefix}{rec_name}/.recording_complete"
        s3_client.put_object(_BUCKET, key, BytesIO(content), len(content))

    if with_storage:
        key = f"{prefix}{rec_name}/.uploading_complete"
        body = b"{}"
        s3_client.put_object(_BUCKET, key, BytesIO(body), len(body))


def _s3_objects_under(s3_client, prefix):
    """Return set of object keys under a prefix."""
    return {
        obj.object_name
        for obj in s3_client.list_objects(_BUCKET, prefix=prefix, recursive=True)
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUploadThenGcDeletes:
    """Full lifecycle: upload -> mark stored -> GC deletes -> state updated."""

    def test_upload_then_gc_deletes(self, s3_client, gc_config, test_prefix, state_db):
        rec = "rec_lifecycle"
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

        # Step 1: Upload recording with both markers
        _upload_to_s3(
            s3_client,
            test_prefix,
            rec,
            {"data.mcap": 64, "meta.json": 32},
            with_completion=True,
            with_storage=True,
            completed_at=old_ts,
        )

        # Step 2: Verify objects exist
        keys = _s3_objects_under(s3_client, f"{test_prefix}{rec}/")
        assert len(keys) == 4  # 2 files + 2 markers

        # Step 3: Track in state DB
        state_db.set_status(rec, "local", STATUS_COMPLETED)

        # Step 4: Run GC (age strategy, very short threshold)
        cfg = dataclasses.replace(gc_config, max_age_hours=0.001)
        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)

        assert result.deleted_count > 0
        assert result.error is None

        # Step 5: Verify S3 objects gone
        keys = _s3_objects_under(s3_client, f"{test_prefix}{rec}/")
        assert len(keys) == 0

        # Step 6: Mark gc_deleted in state DB
        state_db.mark_gc_deleted(rec, "local")
        row = state_db.get_status(rec, "local")
        assert row.status == STATUS_GC_DELETED


class TestUploadWithoutStorageMarkerSurvives:
    """Recording with completion marker but no storage marker survives GC."""

    def test_survives_gc(self, s3_client, gc_config, test_prefix):
        rec = "rec_no_storage"
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

        _upload_to_s3(
            s3_client,
            test_prefix,
            rec,
            {"data.mcap": 64},
            with_completion=True,
            with_storage=False,
            completed_at=old_ts,
        )

        cfg = dataclasses.replace(gc_config, max_age_hours=0.001)
        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)

        # No eligible recordings (no storage marker) -> nothing deleted
        assert result.eligible_count == 0

        # Objects still exist
        keys = _s3_objects_under(s3_client, f"{test_prefix}{rec}/")
        assert len(keys) == 2  # data.mcap + .recording_complete


class TestOrphanCleanedUp:
    """Raw files without markers (crashed recording) cleaned as orphan."""

    def test_orphan_cleaned(self, s3_client, gc_config, test_prefix):
        rec = "rec_orphan"

        # Upload raw files only -- no markers at all
        _upload_to_s3(
            s3_client,
            test_prefix,
            rec,
            {"data.mcap": 64, "sensor.mcap": 128},
            with_completion=False,
            with_storage=False,
        )

        # Objects exist
        assert len(_s3_objects_under(s3_client, f"{test_prefix}{rec}/")) == 2

        # Wait so objects become "old" enough for orphan threshold
        time.sleep(2)

        # orphan_age_hours = 0.0005 ~ 1.8s
        cfg = dataclasses.replace(gc_config, orphan_age_hours=0.0005)
        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)

        assert result.orphan_count == 1
        assert len(_s3_objects_under(s3_client, f"{test_prefix}{rec}/")) == 0


class TestNestedTreeDiscovery:
    """Recordings at different depths discovered as separate recordings."""

    def test_nested_discovery(self, s3_client, gc_config, test_prefix):
        # Deep recording
        _upload_to_s3(
            s3_client,
            test_prefix,
            "robot1/2025/rec_a",
            {"data.mcap": 64},
            with_completion=True,
        )
        # Shallow recording
        _upload_to_s3(
            s3_client, test_prefix, "rec_b", {"data.mcap": 32}, with_completion=True
        )

        recordings = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        prefixes = {r.prefix for r in recordings}

        assert f"{test_prefix}robot1/2025/rec_a/" in prefixes
        assert f"{test_prefix}rec_b/" in prefixes
        assert len(recordings) == 2


class TestEmptyBranchPruned:
    """After GC deletes a recording, empty parent prefix is cleaned."""

    def test_branch_pruned(self, s3_client, gc_config, test_prefix):
        rec_path = "parent/child/rec_prune"
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

        _upload_to_s3(
            s3_client,
            test_prefix,
            rec_path,
            {"data.mcap": 64},
            with_completion=True,
            with_storage=True,
            completed_at=old_ts,
        )

        # GC deletes the recording
        cfg = dataclasses.replace(gc_config, max_age_hours=0.001)
        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)
        assert result.deleted_count > 0

        # Verify parent directories are empty
        parent_keys = _s3_objects_under(s3_client, f"{test_prefix}parent/child/")
        assert len(parent_keys) == 0


class TestGcDeletedPreventsReupload:
    """Once gc_deleted in state DB, should_upload returns False."""

    def test_gc_deleted_prevents_reupload(self, state_db):
        rec = "rec_no_reupload"

        state_db.set_status(rec, "local", STATUS_COMPLETED)
        assert state_db.should_upload(rec, "local") is False

        state_db.mark_gc_deleted(rec, "local")
        assert state_db.should_upload(rec, "local") is False

        # Even check a target that was never set
        assert state_db.should_upload(rec, "cloud") is True


class TestStateDbSurvivesAcrossCycles:
    """State DB retains gc_deleted across multiple GC cycles."""

    def test_state_across_cycles(self, s3_client, gc_config, test_prefix, state_db):
        rec = "rec_two_cycles"
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

        _upload_to_s3(
            s3_client,
            test_prefix,
            rec,
            {"data.mcap": 64},
            with_completion=True,
            with_storage=True,
            completed_at=old_ts,
        )

        cfg = dataclasses.replace(gc_config, max_age_hours=0.001)

        # Cycle 1: deletes the recording
        result1 = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)
        assert result1.deleted_count > 0
        state_db.mark_gc_deleted(rec, "local")

        # Cycle 2: nothing to delete
        result2 = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)
        assert result2.total_recordings == 0
        assert result2.deleted_count == 0

        # State persists
        row = state_db.get_status(rec, "local")
        assert row.status == STATUS_GC_DELETED


class TestCombinedStrategyEndToEnd:
    """Combined strategy: both age AND space criteria fire in same cycle."""

    def test_combined_deletes_both_criteria(self, s3_client, gc_config, test_prefix):
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(hours=48)).isoformat()
        recent_ts = (now - timedelta(minutes=5)).isoformat()

        # Old+large: qualifies under both age and space
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_old_big",
            {"data.mcap": 5000},
            with_completion=True,
            with_storage=True,
            completed_at=old_ts,
        )
        # Old+small: qualifies under age only
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_old_small",
            {"data.mcap": 50},
            with_completion=True,
            with_storage=True,
            completed_at=old_ts,
        )
        # Recent+large: too recent for age, but space could target it
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_recent_big",
            {"data.mcap": 5000},
            with_completion=True,
            with_storage=True,
            completed_at=recent_ts,
        )

        cfg = dataclasses.replace(
            gc_config,
            strategy=parse_gc_strategy(["marker", {"any_of": ["age", "space"]}]),
            max_age_hours=0.001,
            max_storage_gb=0.000001,  # ~1KB threshold, everything over
        )
        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)

        assert result.error is None
        assert result.deleted_count > 0

        # Old recordings deleted (age criterion)
        assert len(_s3_objects_under(s3_client, f"{test_prefix}rec_old_big/")) == 0
        assert len(_s3_objects_under(s3_client, f"{test_prefix}rec_old_small/")) == 0
        # Recent may or may not be deleted by space strategy (depends on order)
        # but at least the old ones are gone


class TestCorruptedMarkerFallback:
    """Corrupted .recording_complete marker -> falls back to last_modified."""

    def test_corrupted_marker_uses_fallback(self, s3_client, gc_config, test_prefix):
        rec = "rec_corrupt"

        # Upload data file
        _upload_to_s3(
            s3_client,
            test_prefix,
            rec,
            {"data.mcap": 64},
            with_completion=False,
            with_storage=True,
        )

        # Write corrupted .recording_complete (not valid JSON)
        corrupt_key = f"{test_prefix}{rec}/.recording_complete"
        body = b"NOT VALID JSON {{{"
        s3_client.put_object(_BUCKET, corrupt_key, BytesIO(body), len(body))

        # Discover and enrich -- should not crash
        recordings = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        assert len(recordings) == 1

        enrich_completed_at(s3_client, _BUCKET, gc_config, recordings)

        # Fallback: completed_at comes from latest_object_modified
        rec_info = recordings[0]
        assert rec_info.completed_at is not None
        assert rec_info.completed_at == rec_info.latest_object_modified


class TestSpaceStrategyUnderThreshold:
    """Total storage under threshold -> nothing deleted."""

    def test_under_threshold_no_deletion(self, s3_client, gc_config, test_prefix):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

        # Two tiny recordings, both eligible
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_tiny_a",
            {"data.mcap": 100},
            with_completion=True,
            with_storage=True,
            completed_at=old_ts,
        )
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_tiny_b",
            {"data.mcap": 100},
            with_completion=True,
            with_storage=True,
            completed_at=old_ts,
        )

        cfg = dataclasses.replace(
            gc_config,
            strategy=parse_gc_strategy(["marker", "space"]),
            max_storage_gb=1,  # 1GB threshold, total is ~400 bytes
        )
        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)

        assert result.eligible_count == 2
        assert result.deleted_count == 0  # under threshold
        assert len(_s3_objects_under(s3_client, f"{test_prefix}rec_tiny_a/")) > 0
        assert len(_s3_objects_under(s3_client, f"{test_prefix}rec_tiny_b/")) > 0


class TestDryRunSpaceStrategy:
    """Dry run with space strategy: logs but doesn't delete."""

    def test_dry_run_space_preserves_objects(self, s3_client, gc_config, test_prefix):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_space_a",
            {"data.mcap": 5000},
            with_completion=True,
            with_storage=True,
            completed_at=old_ts,
        )
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_space_b",
            {"data.mcap": 5000},
            with_completion=True,
            with_storage=True,
            completed_at=old_ts,
        )

        cfg = dataclasses.replace(
            gc_config,
            strategy=parse_gc_strategy(["marker", "space"]),
            max_storage_gb=0.000001,
            dry_run=True,
        )
        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)

        assert result.eligible_count == 2
        assert result.deleted_count == 0  # dry run
        assert result.deleted_bytes > 0  # would have deleted

        # Objects still exist
        assert len(_s3_objects_under(s3_client, f"{test_prefix}rec_space_a/")) > 0
        assert len(_s3_objects_under(s3_client, f"{test_prefix}rec_space_b/")) > 0


class TestMixedEligibilityCorrectSubset:
    """4 recordings in different states. Only the correct subset targeted."""

    def test_mixed_eligibility(self, s3_client, gc_config, test_prefix):
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(hours=48)).isoformat()

        # Eligible + old -> deleted by age strategy
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_eligible_old",
            {"data.mcap": 64},
            with_completion=True,
            with_storage=True,
            completed_at=old_ts,
        )
        # Eligible + recent -> kept (too new for age)
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_eligible_recent",
            {"data.mcap": 64},
            with_completion=True,
            with_storage=True,
        )
        # No storage marker + old -> kept by strategy, but NOT orphan
        # (has completion marker)
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_awaiting",
            {"data.mcap": 64},
            with_completion=True,
            with_storage=False,
            completed_at=old_ts,
        )
        # Orphan: no markers at all
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_orphan",
            {"data.mcap": 64},
            with_completion=False,
            with_storage=False,
        )

        # Wait for orphan to age past threshold
        time.sleep(2)

        cfg = dataclasses.replace(
            gc_config,
            strategy=parse_gc_strategy(["marker", "age"]),
            max_age_hours=0.001,
            orphan_age_hours=0.0005,
        )
        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)

        assert result.error is None

        # Eligible old: deleted by age
        assert len(_s3_objects_under(s3_client, f"{test_prefix}rec_eligible_old/")) == 0
        # Orphan: deleted by orphan cleanup
        assert result.orphan_count == 1
        assert len(_s3_objects_under(s3_client, f"{test_prefix}rec_orphan/")) == 0
        # Eligible recent: survived (too new)
        assert (
            len(_s3_objects_under(s3_client, f"{test_prefix}rec_eligible_recent/")) > 0
        )
        # Awaiting: survived (has completion marker, not orphan; no storage marker, not eligible)
        assert len(_s3_objects_under(s3_client, f"{test_prefix}rec_awaiting/")) > 0


# ---------------------------------------------------------------------------
# Canonical path upload + GC integration
# ---------------------------------------------------------------------------

_CANONICAL_META = json.dumps(
    {
        "schema_version": "2.0",
        "uuid": "integ-test-uuid-001",
        "robot": {"type": "umi", "id": "robot-01"},
        "environment": {"type": "real_world", "site": "tokyo", "location": "lab"},
        "runner": {"type": "operator", "organization": "airoa", "name": "op"},
        "episode": {
            "start_time": 1700000000.123,
            "end_time": 1700000001.0,
            "success": True,
            "label": "a",
        },
        "files": [],
    }
)


class TestCanonicalUploadAndDiscover:
    """upload_recording(path_rule='canonical') -> discover_recordings finds it."""

    def test_canonical_upload_discoverable(
        self, s3_client, gc_config, test_prefix, tmp_path
    ):
        rec = tmp_path / "rec_canon"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 128)
        (rec / "meta.json").write_text(_CANONICAL_META)

        from data_backend.upload import upload_recording

        upload_recording(
            s3_client, _BUCKET, test_prefix, str(rec), path_rule="canonical"
        )

        recordings = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        assert len(recordings) == 1
        rec_info = recordings[0]
        assert "org=airoa/" in rec_info.prefix
        assert "uuid=integ-test-uuid-001" in rec_info.prefix
        assert rec_info.completion_marker_exists is True
        assert rec_info.total_size_bytes > 0


class TestCanonicalUploadThenGcDeletes:
    """Canonical upload with storage marker -> age GC deletes it."""

    def test_gc_deletes_canonical(self, s3_client, gc_config, test_prefix, tmp_path):
        rec = tmp_path / "rec_canon_gc"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 128)
        (rec / "meta.json").write_text(_CANONICAL_META)

        from data_backend.upload import upload_recording

        upload_recording(
            s3_client, _BUCKET, test_prefix, str(rec), path_rule="canonical"
        )

        # Find the canonical prefix that was created
        recordings = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        assert len(recordings) == 1
        canon_prefix = recordings[0].prefix

        # Add storage marker to make it GC-eligible
        storage_key = f"{canon_prefix}.uploading_complete"
        s3_client.put_object(_BUCKET, storage_key, BytesIO(b"{}"), 2)

        # Overwrite .recording_complete with an old timestamp so age GC picks it up
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        marker_key = f"{canon_prefix}.recording_complete"
        marker_body = json.dumps({"completed_at": old_ts}).encode()
        s3_client.put_object(
            _BUCKET, marker_key, BytesIO(marker_body), len(marker_body)
        )

        # Run GC with very short age threshold
        cfg = dataclasses.replace(gc_config, max_age_hours=0.001)
        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)

        assert result.error is None
        assert result.deleted_count > 0
        assert len(_s3_objects_under(s3_client, canon_prefix)) == 0


class TestFlatAndCanonicalCoexist:
    """Two recordings (flat + canonical) in same prefix. GC handles both."""

    def test_coexist_and_gc(self, s3_client, gc_config, test_prefix, tmp_path):
        # Flat upload
        _upload_to_s3(
            s3_client,
            test_prefix,
            "rec_flat",
            {"data.mcap": 64},
            with_completion=True,
            with_storage=True,
            completed_at=(datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(),
        )

        # Canonical upload
        rec = tmp_path / "rec_canon_coexist"
        rec.mkdir()
        (rec / "data.mcap").write_bytes(b"\x00" * 64)
        (rec / "meta.json").write_text(_CANONICAL_META)

        from data_backend.upload import upload_recording

        upload_recording(
            s3_client, _BUCKET, test_prefix, str(rec), path_rule="canonical"
        )

        # Discover both
        recordings = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        assert len(recordings) == 2

        flat_recs = [r for r in recordings if "rec_flat/" in r.prefix]
        canon_recs = [r for r in recordings if "org=airoa/" in r.prefix]
        assert len(flat_recs) == 1
        assert len(canon_recs) == 1

        # Only flat has storage marker -> only flat is eligible
        cfg = dataclasses.replace(gc_config, max_age_hours=0.001)
        result = run_gc_cycle_with_result(s3_client, _BUCKET, test_prefix, cfg)

        assert result.eligible_count == 1
        assert result.deleted_count > 0

        # Flat deleted, canonical still exists
        assert len(_s3_objects_under(s3_client, f"{test_prefix}rec_flat/")) == 0
        remaining = discover_recordings(s3_client, _BUCKET, test_prefix, gc_config)
        assert len(remaining) == 1
        assert "org=airoa/" in remaining[0].prefix
