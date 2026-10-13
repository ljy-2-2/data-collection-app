"""Tests for S3 garbage collection."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock


from data_backend.config import GCConfig, parse_gc_strategy
from data_backend.gc import (
    GCCycleResult,
    RecordingInfo,
    compute_diagnostic_level,
    delete_recordings,
    discover_recordings,
    enrich_completed_at,
    prune_empty_branches,
    run_gc_cycle,
    run_gc_cycle_with_result,
    select_for_deletion,
    select_orphans,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BUCKET = "data"
_PREFIX = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> GCConfig:
    strategy_raw = overrides.pop("strategy", ["marker", {"any_of": ["age", "space"]}])
    defaults = dict(
        check_interval_sec=300,
        max_storage_gb=0,
        max_age_hours=0,
        dry_run=False,
        storage_marker=".uploading_complete",
        completion_marker=".recording_complete",
        orphan_age_hours=1,
    )
    defaults.update(overrides)
    defaults["strategy"] = parse_gc_strategy(strategy_raw)
    return GCConfig(**defaults)


def _make_s3_object(name: str, size: int = 100, last_modified=None, is_dir=False):
    obj = SimpleNamespace()
    obj.object_name = name
    obj.size = size
    obj.last_modified = last_modified
    obj.is_dir = is_dir
    return obj


def _make_recording(
    prefix: str,
    objects: list[tuple[str, int]] | None = None,
    storage_marker: bool = False,
    completion_marker: bool = True,
    completed_at: datetime | None = None,
    latest_object_modified: datetime | None = None,
) -> RecordingInfo:
    objs = objects or [(f"{prefix}file.bag", 1_000_000)]
    return RecordingInfo(
        prefix=prefix,
        objects=objs,
        total_size_bytes=sum(s for _, s in objs),
        completion_marker_exists=completion_marker,
        storage_marker_exists=storage_marker,
        completed_at=completed_at,
        latest_object_modified=latest_object_modified,
    )


# ---------------------------------------------------------------------------
# discover_recordings
# ---------------------------------------------------------------------------


class TestDiscoverRecordings:
    def test_groups_objects_by_recording_prefix(self):
        client = MagicMock()
        client.list_objects.return_value = [
            _make_s3_object("rec1/data.bag", 1000),
            _make_s3_object("rec1/metadata.json", 200),
            _make_s3_object("rec1/.uploading_complete", 10),
            _make_s3_object("rec2/data.bag", 2000),
        ]
        cfg = _make_config()
        result = discover_recordings(client, _BUCKET, _PREFIX, cfg)

        by_prefix = {r.prefix: r for r in result}
        assert len(by_prefix) == 2

        rec1 = by_prefix["rec1/"]
        assert len(rec1.objects) == 3
        assert rec1.total_size_bytes == 1210
        assert rec1.storage_marker_exists is True

        rec2 = by_prefix["rec2/"]
        assert len(rec2.objects) == 1
        assert rec2.total_size_bytes == 2000
        assert rec2.storage_marker_exists is False

    def test_with_prefix(self):
        client = MagicMock()
        client.list_objects.return_value = [
            _make_s3_object("robot1/rec1/data.bag", 500),
            _make_s3_object("robot1/rec1/.uploading_complete", 10),
        ]
        cfg = _make_config()
        result = discover_recordings(client, _BUCKET, "robot1/", cfg)

        assert len(result) == 1
        assert result[0].prefix == "robot1/rec1/"
        assert result[0].storage_marker_exists is True
        client.list_objects.assert_called_once_with(
            "data", prefix="robot1/", recursive=True
        )

    def test_skips_root_level_objects(self):
        client = MagicMock()
        client.list_objects.return_value = [
            _make_s3_object("stray_file.txt", 100),
            _make_s3_object("rec1/data.bag", 500),
        ]
        cfg = _make_config()
        result = discover_recordings(client, _BUCKET, _PREFIX, cfg)

        assert len(result) == 1
        assert result[0].prefix == "rec1/"


# ---------------------------------------------------------------------------
# enrich_completed_at
# ---------------------------------------------------------------------------


class TestEnrichCompletedAt:
    def test_parses_completion_marker(self):
        ts = "2025-01-15T10:30:00+00:00"
        marker_json = json.dumps({"completed_at": ts, "files": {}}).encode()

        client = MagicMock()
        response = MagicMock()
        response.read.return_value = marker_json
        client.get_object.return_value = response

        rec = _make_recording("rec1/")
        cfg = _make_config()
        enrich_completed_at(client, _BUCKET, cfg, [rec])

        assert rec.completed_at == datetime.fromisoformat(ts)
        client.get_object.assert_called_once_with("data", "rec1/.recording_complete")

    def test_fallback_to_last_modified(self):
        client = MagicMock()
        client.get_object.side_effect = Exception("not found")
        fallback_time = datetime(2025, 1, 10, 12, 0, tzinfo=timezone.utc)

        rec = _make_recording("rec1/")
        rec.latest_object_modified = fallback_time
        cfg = _make_config()
        enrich_completed_at(client, _BUCKET, cfg, [rec])

        assert rec.completed_at == fallback_time
        # Should NOT issue a second list_objects call
        client.list_objects.assert_not_called()


# ---------------------------------------------------------------------------
# select_for_deletion — age strategy
# ---------------------------------------------------------------------------


class TestAgeStrategy:
    def test_deletes_old_eligible(self):
        now = datetime.now(timezone.utc)
        old = _make_recording(
            "old/",
            storage_marker=True,
            completed_at=now - timedelta(hours=25),
        )
        recent = _make_recording(
            "recent/",
            storage_marker=True,
            completed_at=now - timedelta(hours=1),
        )
        cfg = _make_config(strategy=["marker", "age"], max_age_hours=24)
        result = select_for_deletion([old, recent], cfg)

        assert [r.prefix for r in result] == ["old/"]

    def test_disabled_when_zero(self):
        now = datetime.now(timezone.utc)
        old = _make_recording(
            "old/",
            storage_marker=True,
            completed_at=now - timedelta(hours=1000),
        )
        cfg = _make_config(strategy=["marker", "age"], max_age_hours=0)
        result = select_for_deletion([old], cfg)
        assert result == []

    def test_skips_none_completed_at(self):
        rec = _make_recording("x/", storage_marker=True, completed_at=None)
        cfg = _make_config(strategy=["marker", "age"], max_age_hours=1)
        result = select_for_deletion([rec], cfg)
        assert result == []


# ---------------------------------------------------------------------------
# select_for_deletion — space strategy
# ---------------------------------------------------------------------------


class TestSpaceStrategy:
    def test_deletes_oldest_eligible_to_meet_threshold(self):
        now = datetime.now(timezone.utc)
        # 3 recordings, 1 GB each, threshold 2 GB → should delete 1 oldest eligible
        recs = [
            _make_recording(
                "oldest/",
                objects=[("oldest/data.bag", 1_000_000_000)],
                storage_marker=True,
                completed_at=now - timedelta(hours=3),
            ),
            _make_recording(
                "middle/",
                objects=[("middle/data.bag", 1_000_000_000)],
                storage_marker=True,
                completed_at=now - timedelta(hours=2),
            ),
            _make_recording(
                "newest/",
                objects=[("newest/data.bag", 1_000_000_000)],
                storage_marker=True,
                completed_at=now - timedelta(hours=1),
            ),
        ]
        cfg = _make_config(strategy=["marker", "space"], max_storage_gb=2)
        result = select_for_deletion(recs, cfg)

        assert [r.prefix for r in result] == ["oldest/"]

    def test_no_deletion_under_threshold(self):
        now = datetime.now(timezone.utc)
        rec = _make_recording(
            "small/",
            objects=[("small/data.bag", 500_000_000)],
            storage_marker=True,
            completed_at=now - timedelta(hours=1),
        )
        cfg = _make_config(strategy=["marker", "space"], max_storage_gb=1)
        result = select_for_deletion([rec], cfg)
        assert result == []

    def test_disabled_when_zero(self):
        now = datetime.now(timezone.utc)
        rec = _make_recording(
            "big/",
            objects=[("big/data.bag", 100_000_000_000)],
            storage_marker=True,
            completed_at=now - timedelta(hours=1),
        )
        cfg = _make_config(strategy=["marker", "space"], max_storage_gb=0)
        result = select_for_deletion([rec], cfg)
        assert result == []


# ---------------------------------------------------------------------------
# select_for_deletion — combined strategy
# ---------------------------------------------------------------------------


class TestCombinedStrategy:
    def test_deduplicates_across_strategies(self):
        now = datetime.now(timezone.utc)
        # This recording is both old AND over space budget
        rec = _make_recording(
            "both/",
            objects=[("both/data.bag", 2_000_000_000)],
            storage_marker=True,
            completed_at=now - timedelta(hours=100),
        )
        cfg = _make_config(
            strategy=["marker", {"any_of": ["age", "space"]}],
            max_age_hours=24,
            max_storage_gb=1,
        )
        result = select_for_deletion([rec], cfg)

        assert len(result) == 1
        assert result[0].prefix == "both/"

    def test_age_only_fires_in_combined(self):
        now = datetime.now(timezone.utc)
        rec = _make_recording(
            "old/",
            objects=[("old/data.bag", 100)],
            storage_marker=True,
            completed_at=now - timedelta(hours=100),
        )
        # Under space budget, but over age → should still delete
        cfg = _make_config(
            strategy=["marker", {"any_of": ["age", "space"]}],
            max_age_hours=24,
            max_storage_gb=1000,
        )
        result = select_for_deletion([rec], cfg)
        assert [r.prefix for r in result] == ["old/"]


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


class TestEligibility:
    def test_no_storage_marker_never_deleted(self):
        now = datetime.now(timezone.utc)
        ineligible = _make_recording(
            "no_marker/",
            objects=[("no_marker/data.bag", 5_000_000_000)],
            storage_marker=False,
            completed_at=now - timedelta(hours=1000),
        )
        # Both strategies would want to delete it, but no marker
        cfg = _make_config(
            strategy=["marker", {"any_of": ["age", "space"]}],
            max_age_hours=1,
            max_storage_gb=1,
        )
        result = select_for_deletion([ineligible], cfg)
        assert result == []

    def test_mixed_eligibility(self):
        now = datetime.now(timezone.utc)
        eligible = _make_recording(
            "eligible/",
            objects=[("eligible/data.bag", 2_000_000_000)],
            storage_marker=True,
            completed_at=now - timedelta(hours=100),
        )
        ineligible = _make_recording(
            "ineligible/",
            objects=[("ineligible/data.bag", 2_000_000_000)],
            storage_marker=False,
            completed_at=now - timedelta(hours=100),
        )
        cfg = _make_config(strategy=["marker", "age"], max_age_hours=24)
        result = select_for_deletion([eligible, ineligible], cfg)
        assert [r.prefix for r in result] == ["eligible/"]


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_no_remove_objects_called(self):
        client = MagicMock()
        rec = _make_recording(
            "rec1/",
            objects=[("rec1/data.bag", 1000)],
            storage_marker=True,
        )
        cfg = _make_config(dry_run=True)
        deleted = delete_recordings(client, _BUCKET, cfg, [rec])

        client.remove_objects.assert_not_called()
        assert deleted == 0


# ---------------------------------------------------------------------------
# Delete errors
# ---------------------------------------------------------------------------


class TestDeleteErrors:
    def test_errors_logged_no_crash(self):
        client = MagicMock()
        error_obj = SimpleNamespace(
            object_name="rec1/data.bag",
            error_code="InternalError",
            error_message="something failed",
        )
        client.remove_objects.return_value = iter([error_obj])

        rec = _make_recording(
            "rec1/",
            objects=[("rec1/data.bag", 1000)],
            storage_marker=True,
        )
        cfg = _make_config(dry_run=False)
        deleted = delete_recordings(client, _BUCKET, cfg, [rec])

        # Errors occurred, so deleted count should be 0 for this recording
        assert deleted == 0

    def test_successful_deletion(self):
        client = MagicMock()
        client.remove_objects.return_value = iter([])

        rec = _make_recording(
            "rec1/",
            objects=[("rec1/data.bag", 1000), ("rec1/meta.json", 50)],
            storage_marker=True,
        )
        cfg = _make_config(dry_run=False)
        deleted = delete_recordings(client, _BUCKET, cfg, [rec])

        assert deleted == 2
        client.remove_objects.assert_called_once()


# ---------------------------------------------------------------------------
# Space strategy deletes only enough
# ---------------------------------------------------------------------------


class TestSpaceDeletesMinimum:
    def test_stops_deleting_once_under_threshold(self):
        now = datetime.now(timezone.utc)
        # 4 x 1GB, threshold 2GB → need to delete 2
        recs = [
            _make_recording(
                f"rec{i}/",
                objects=[(f"rec{i}/data.bag", 1_000_000_000)],
                storage_marker=True,
                completed_at=now - timedelta(hours=4 - i),
            )
            for i in range(4)
        ]
        cfg = _make_config(strategy=["marker", "space"], max_storage_gb=2)
        result = select_for_deletion(recs, cfg)

        # Should delete exactly 2 oldest
        assert len(result) == 2
        assert result[0].prefix == "rec0/"
        assert result[1].prefix == "rec1/"


# ---------------------------------------------------------------------------
# run_gc_cycle_with_result
# ---------------------------------------------------------------------------


class TestRunGcCycleWithResult:
    def test_returns_structured_result(self):
        client = MagicMock()
        now = datetime.now(timezone.utc)
        client.list_objects.return_value = [
            _make_s3_object(
                "rec1/data.bag", 1_000_000_000, last_modified=now - timedelta(hours=50)
            ),
            _make_s3_object(
                "rec1/.uploading_complete", 10, last_modified=now - timedelta(hours=50)
            ),
            _make_s3_object("rec2/data.bag", 500_000_000, last_modified=now),
        ]
        # Make get_object raise so fallback is used
        client.get_object.side_effect = Exception("not found")
        client.remove_objects.return_value = iter([])

        cfg = _make_config(strategy=["marker", "age"], max_age_hours=24)
        result = run_gc_cycle_with_result(client, _BUCKET, _PREFIX, cfg)

        assert result.total_recordings == 2
        assert result.total_bytes == 1_500_000_010
        assert result.eligible_count == 1
        assert result.deleted_count == 2  # rec1/data.bag + rec1/.uploading_complete
        assert result.error is None

    def test_no_eligible_returns_zero_deleted(self):
        client = MagicMock()
        client.list_objects.return_value = [
            _make_s3_object("rec1/data.bag", 1000),
        ]
        cfg = _make_config(strategy=["marker", "age"], max_age_hours=1)
        result = run_gc_cycle_with_result(client, _BUCKET, _PREFIX, cfg)

        assert result.total_recordings == 1
        assert result.eligible_count == 0
        assert result.deleted_count == 0
        assert result.error is None

    def test_exception_during_cycle_captured(self):
        client = MagicMock()
        client.list_objects.side_effect = Exception("connection refused")
        cfg = _make_config(strategy=["marker", {"any_of": ["age", "space"]}])
        result = run_gc_cycle_with_result(client, _BUCKET, _PREFIX, cfg)

        assert result.error == "connection refused"


# ---------------------------------------------------------------------------
# compute_diagnostic_level
# ---------------------------------------------------------------------------


class TestDiagnosticLevelComputation:
    def test_ok_under_threshold(self):
        cfg = _make_config(max_storage_gb=10)
        result = GCCycleResult(total_bytes=5_000_000_000, eligible_count=1)
        assert compute_diagnostic_level(cfg, result) == 0  # OK

    def test_warn_approaching_threshold(self):
        cfg = _make_config(max_storage_gb=10)
        # 85% of 10 GB = 8.5 GB
        result = GCCycleResult(total_bytes=8_500_000_000, eligible_count=1)
        assert compute_diagnostic_level(cfg, result) == 1  # WARN

    def test_warn_over_threshold_with_eligible(self):
        cfg = _make_config(max_storage_gb=10)
        result = GCCycleResult(total_bytes=12_000_000_000, eligible_count=3)
        assert compute_diagnostic_level(cfg, result) == 1  # WARN

    def test_error_over_threshold_no_eligible(self):
        cfg = _make_config(max_storage_gb=10)
        result = GCCycleResult(total_bytes=12_000_000_000, eligible_count=0)
        assert compute_diagnostic_level(cfg, result) == 2  # ERROR

    def test_error_on_exception(self):
        cfg = _make_config(max_storage_gb=10)
        result = GCCycleResult(error="connection refused")
        assert compute_diagnostic_level(cfg, result) == 2  # ERROR

    def test_error_on_invalid_strategy(self):
        cfg = _make_config(max_storage_gb=10)
        result = GCCycleResult(error="Unknown GC_STRATEGY='invalid'")
        assert compute_diagnostic_level(cfg, result) == 2  # ERROR

    def test_ok_when_disabled(self):
        cfg = _make_config(max_storage_gb=0)
        result = GCCycleResult(total_bytes=100_000_000_000, eligible_count=0)
        assert compute_diagnostic_level(cfg, result) == 0  # OK


# ---------------------------------------------------------------------------
# Nested tree discovery
# ---------------------------------------------------------------------------


class TestDiscoverNestedTree:
    """discover_recordings groups by leaf directory at any depth."""

    def test_nested_tree(self):
        now = datetime.now(timezone.utc)
        client = MagicMock()
        client.list_objects.return_value = [
            _make_s3_object("robot1/2025/rec_001/data.mcap", 100, now),
            _make_s3_object("robot1/2025/rec_001/.uploading_complete", 10, now),
            _make_s3_object("robot1/2025/rec_001/.recording_complete", 10, now),
            _make_s3_object("robot1/2025/rec_002/data.mcap", 200, now),
        ]
        cfg = _make_config()
        recs = discover_recordings(client, _BUCKET, _PREFIX, cfg)
        prefixes = {r.prefix for r in recs}
        assert "robot1/2025/rec_001/" in prefixes
        assert "robot1/2025/rec_002/" in prefixes
        assert len(recs) == 2

    def test_mixed_depth(self):
        now = datetime.now(timezone.utc)
        client = MagicMock()
        client.list_objects.return_value = [
            _make_s3_object("shallow/data.mcap", 100, now),
            _make_s3_object("deep/nest/a/data.mcap", 200, now),
        ]
        cfg = _make_config()
        recs = discover_recordings(client, _BUCKET, _PREFIX, cfg)
        prefixes = {r.prefix for r in recs}
        assert "shallow/" in prefixes
        assert "deep/nest/a/" in prefixes

    def test_recording_complete_detected(self):
        now = datetime.now(timezone.utc)
        client = MagicMock()
        client.list_objects.return_value = [
            _make_s3_object("rec/data.mcap", 100, now),
            _make_s3_object("rec/.recording_complete", 10, now),
        ]
        cfg = _make_config()
        recs = discover_recordings(client, _BUCKET, _PREFIX, cfg)
        assert recs[0].completion_marker_exists is True

    def test_skips_dir_objects(self):
        client = MagicMock()
        client.list_objects.return_value = [
            _make_s3_object("folder/", 0, is_dir=True),
            _make_s3_object("rec/data.mcap", 100),
        ]
        cfg = _make_config()
        recs = discover_recordings(client, _BUCKET, _PREFIX, cfg)
        assert len(recs) == 1
        assert recs[0].prefix == "rec/"


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------


class TestOrphanDetection:
    """select_orphans finds stale directories without completion marker."""

    def test_detects_stale_orphan(self):
        now = datetime.now(timezone.utc)
        orphan = _make_recording(
            "abandoned/",
            completion_marker=False,
            latest_object_modified=now - timedelta(hours=5),
        )
        cfg = _make_config(orphan_age_hours=1)
        result = select_orphans([orphan], cfg)
        assert len(result) == 1
        assert result[0].prefix == "abandoned/"

    def test_spares_recent(self):
        now = datetime.now(timezone.utc)
        recent = _make_recording(
            "in_progress/",
            completion_marker=False,
            latest_object_modified=now - timedelta(minutes=10),
        )
        cfg = _make_config(orphan_age_hours=1)
        result = select_orphans([recent], cfg)
        assert len(result) == 0

    def test_spares_recording_with_marker(self):
        now = datetime.now(timezone.utc)
        good = _make_recording(
            "valid/",
            completion_marker=True,
            latest_object_modified=now - timedelta(hours=5),
        )
        cfg = _make_config(orphan_age_hours=1)
        result = select_orphans([good], cfg)
        assert len(result) == 0

    def test_disabled_when_zero(self):
        now = datetime.now(timezone.utc)
        orphan = _make_recording(
            "abandoned/",
            completion_marker=False,
            latest_object_modified=now - timedelta(hours=100),
        )
        cfg = _make_config(orphan_age_hours=0)
        result = select_orphans([orphan], cfg)
        assert len(result) == 0

    def test_spares_storage_marker(self):
        """A recording with storage marker (even without completion marker)
        is handled by the main GC strategy, not orphan cleanup."""
        now = datetime.now(timezone.utc)
        rec = _make_recording(
            "uploaded/",
            completion_marker=False,
            storage_marker=True,
            latest_object_modified=now - timedelta(hours=5),
        )
        cfg = _make_config(orphan_age_hours=1)
        result = select_orphans([rec], cfg)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Empty branch pruning
# ---------------------------------------------------------------------------


class TestPruneEmptyBranches:
    """prune_empty_branches removes empty ancestors after deletion."""

    def test_prunes_empty_ancestors(self):
        client = MagicMock()
        # Each ancestor check returns empty
        client.list_objects.return_value = []
        client.remove_objects.return_value = iter([])
        cfg = _make_config()
        pruned = prune_empty_branches(client, _BUCKET, "root/", cfg, ["root/a/b/rec/"])
        # Should check "root/a/b/" and "root/a/" (stops before "root/")
        assert pruned == 2

    def test_stops_at_root_prefix(self):
        client = MagicMock()
        client.list_objects.return_value = []
        cfg = _make_config()
        pruned = prune_empty_branches(client, _BUCKET, "data/", cfg, ["data/rec/"])
        # Only "data/" is the root prefix, nothing to prune above rec/
        assert pruned == 0  # "data/" is root, can't prune

    def test_skips_non_empty(self):
        client = MagicMock()
        # Branch has a real object → not empty
        client.list_objects.return_value = [
            _make_s3_object("a/other_rec/data.mcap", 100)
        ]
        cfg = _make_config()
        pruned = prune_empty_branches(client, _BUCKET, _PREFIX, cfg, ["a/deleted_rec/"])
        assert pruned == 0

    def test_dry_run_skips_pruning(self):
        client = MagicMock()
        cfg = _make_config(dry_run=True)
        pruned = prune_empty_branches(client, _BUCKET, _PREFIX, cfg, ["a/b/rec/"])
        assert pruned == 0
        client.list_objects.assert_not_called()


# ---------------------------------------------------------------------------
# run_gc_cycle (non-result variant)
# ---------------------------------------------------------------------------


class TestRunGcCycle:
    """Tests for the void run_gc_cycle() variant."""

    def test_deletes_eligible_recordings(self):
        client = MagicMock()
        now = datetime.now(timezone.utc)
        client.list_objects.return_value = [
            _make_s3_object(
                "rec1/data.bag",
                1_000_000,
                last_modified=now - timedelta(hours=50),
            ),
            _make_s3_object(
                "rec1/.uploading_complete",
                10,
                last_modified=now - timedelta(hours=50),
            ),
        ]
        client.get_object.side_effect = Exception("not found")
        client.remove_objects.return_value = iter([])

        cfg = _make_config(strategy=["marker", "age"], max_age_hours=24)
        run_gc_cycle(client, _BUCKET, _PREFIX, cfg)

        client.remove_objects.assert_called_once()

    def test_no_deletion_when_nothing_eligible(self):
        client = MagicMock()
        now = datetime.now(timezone.utc)
        client.list_objects.return_value = [
            _make_s3_object("rec1/data.bag", 1000, last_modified=now),
        ]
        cfg = _make_config(strategy=["marker", "age"], max_age_hours=24)
        run_gc_cycle(client, _BUCKET, _PREFIX, cfg)

        client.remove_objects.assert_not_called()
