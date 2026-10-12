"""Scenario tests for S3 garbage collection.

Each test exercises a realistic multi-step workflow through the GC
pipeline, unlike the unit tests which test individual functions in
isolation.  Uses abstract marker naming (completion_marker,
storage_marker) to be S3-provider-agnostic.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock


from data_backend.config import GCConfig, parse_gc_strategy
from data_backend.gc import (
    GCCycleResult,
    RecordingInfo,
    compute_diagnostic_level,
    discover_recordings,
    enrich_completed_at,
    prune_empty_branches,
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
# Scenarios
# ---------------------------------------------------------------------------


class TestScenarioFullGcLifecycle:
    """End-to-end: 3 recordings (2 eligible, 1 orphan). Age strategy deletes
    old eligible one, orphan cleanup catches the unmarked stale one."""

    def test_scenario_full_gc_lifecycle(self):
        now = datetime.now(timezone.utc)
        client = MagicMock()

        # 3 recordings: rec_old (eligible, old), rec_new (eligible, recent),
        # rec_no_marker (not eligible)
        client.list_objects.return_value = [
            _make_s3_object(
                "rec_old/data.bag",
                1_000_000_000,
                last_modified=now - timedelta(hours=50),
            ),
            _make_s3_object(
                "rec_old/.uploading_complete",
                10,
                last_modified=now - timedelta(hours=50),
            ),
            _make_s3_object(
                "rec_new/data.bag", 500_000_000, last_modified=now - timedelta(hours=1)
            ),
            _make_s3_object(
                "rec_new/.uploading_complete",
                10,
                last_modified=now - timedelta(hours=1),
            ),
            _make_s3_object(
                "rec_no_marker/data.bag",
                200_000_000,
                last_modified=now - timedelta(hours=100),
            ),
        ]

        # enrich_completed_at reads .recording_complete markers
        marker_data = json.dumps(
            {
                "completed_at": (now - timedelta(hours=50)).isoformat(),
                "files": {},
            }
        ).encode()
        marker_new = json.dumps(
            {
                "completed_at": (now - timedelta(hours=1)).isoformat(),
                "files": {},
            }
        ).encode()

        def _get_object(bucket, key):
            resp = MagicMock()
            if "rec_old" in key:
                resp.read.return_value = marker_data
            elif "rec_new" in key:
                resp.read.return_value = marker_new
            else:
                raise Exception("not found")
            return resp

        client.get_object.side_effect = _get_object
        client.remove_objects.return_value = iter([])

        cfg = _make_config(strategy=["marker", "age"], max_age_hours=24)
        result = run_gc_cycle_with_result(client, _BUCKET, _PREFIX, cfg)

        # Verify result fields
        assert result.total_recordings == 3
        assert result.eligible_count == 2
        assert result.deleted_count == 3  # 2 from rec_old + 1 from orphan
        assert result.deleted_bytes == 1_000_000_010
        # rec_no_marker is 100h old with no .recording_complete -> orphan
        assert result.orphan_count == 1
        assert result.orphan_bytes == 200_000_000
        assert result.error is None

        # remove_objects called twice: once for rec_old, once for orphan rec_no_marker
        assert client.remove_objects.call_count == 2
        all_deleted = []
        for call in client.remove_objects.call_args_list:
            assert call[0][0] == "data"
            all_deleted.extend(do.name for do in call[0][1])
        assert "rec_old/data.bag" in all_deleted
        assert "rec_old/.uploading_complete" in all_deleted
        assert "rec_no_marker/data.bag" in all_deleted
        assert all("rec_new" not in n for n in all_deleted)


class TestScenarioSpaceCleanupProgressive:
    """5 x 1GB recordings, 3GB threshold, space strategy.
    Exactly 2 oldest should be deleted to bring total to 3GB."""

    def test_scenario_space_cleanup_progressive(self):
        now = datetime.now(timezone.utc)
        recordings = []
        for i in range(5):
            recordings.append(
                _make_recording(
                    f"rec{i}/",
                    objects=[(f"rec{i}/data.bag", 1_000_000_000)],
                    storage_marker=True,
                    completed_at=now - timedelta(hours=5 - i),
                )
            )

        cfg = _make_config(strategy=["marker", "space"], max_storage_gb=3)
        to_delete = select_for_deletion(recordings, cfg)

        # Should delete exactly 2 oldest (rec0, rec1) -> 5GB - 2GB = 3GB
        assert len(to_delete) == 2
        deleted_prefixes = [r.prefix for r in to_delete]
        assert "rec0/" in deleted_prefixes
        assert "rec1/" in deleted_prefixes

        # Remaining total should be <= threshold
        remaining_bytes = sum(
            r.total_size_bytes for r in recordings if r.prefix not in deleted_prefixes
        )
        assert remaining_bytes <= 3_000_000_000


class TestScenarioCombinedStrategyDeduplication:
    """Recording qualifies under both age AND space criteria.
    Should appear only once in the deletion set."""

    def test_scenario_combined_strategy_deduplication(self):
        now = datetime.now(timezone.utc)
        # Single recording: old (100h) and large (5GB with 2GB threshold)
        rec = _make_recording(
            "both_criteria/",
            objects=[("both_criteria/data.bag", 5_000_000_000)],
            storage_marker=True,
            completed_at=now - timedelta(hours=100),
        )

        cfg = _make_config(
            strategy=["marker", {"any_of": ["age", "space"]}],
            max_age_hours=24,
            max_storage_gb=2,
        )
        to_delete = select_for_deletion([rec], cfg)

        assert len(to_delete) == 1
        assert to_delete[0].prefix == "both_criteria/"


class TestScenarioIneligibleRecordingsProtected:
    """Old/large recordings without storage marker are protected from
    age/space strategies. Only the orphan mechanism can remove them."""

    def test_scenario_ineligible_recordings_protected(self):
        now = datetime.now(timezone.utc)

        ineligible_old = _make_recording(
            "no_marker_old/",
            objects=[("no_marker_old/data.bag", 3_000_000_000)],
            storage_marker=False,
            completed_at=now - timedelta(hours=200),
        )
        ineligible_big = _make_recording(
            "no_marker_big/",
            objects=[("no_marker_big/data.bag", 10_000_000_000)],
            storage_marker=False,
            completed_at=now - timedelta(hours=10),
        )
        eligible_small = _make_recording(
            "eligible/",
            objects=[("eligible/data.bag", 100_000)],
            storage_marker=True,
            completed_at=now - timedelta(hours=1),
        )

        recordings = [ineligible_old, ineligible_big, eligible_small]

        # Age strategy: only eligible_small is eligible but it's only 1h old
        cfg_age = _make_config(strategy=["marker", "age"], max_age_hours=24)
        to_delete_age = select_for_deletion(recordings, cfg_age)
        assert len(to_delete_age) == 0  # eligible_small is too recent

        # Combined strategy with aggressive thresholds
        cfg_combined = _make_config(
            strategy=["marker", {"any_of": ["age", "space"]}],
            max_age_hours=5,
            max_storage_gb=1,
        )
        to_delete_combined = select_for_deletion(recordings, cfg_combined)

        # Only the eligible one could be targeted, and it doesn't meet age
        # threshold (1h < 5h). Space strategy would want to delete but
        # ineligible recordings are protected.
        deleted_prefixes = {r.prefix for r in to_delete_combined}
        assert "no_marker_old/" not in deleted_prefixes
        assert "no_marker_big/" not in deleted_prefixes


class TestScenarioDryRunFullCycle:
    """Full pipeline with dry_run=True. remove_objects never called,
    deleted_count == 0, but eligible_count > 0."""

    def test_scenario_dry_run_full_cycle(self):
        now = datetime.now(timezone.utc)
        client = MagicMock()

        client.list_objects.return_value = [
            _make_s3_object(
                "rec1/data.bag", 2_000_000_000, last_modified=now - timedelta(hours=50)
            ),
            _make_s3_object(
                "rec1/.uploading_complete", 10, last_modified=now - timedelta(hours=50)
            ),
        ]
        client.get_object.side_effect = Exception("not found")

        cfg = _make_config(strategy=["marker", "age"], max_age_hours=24, dry_run=True)
        result = run_gc_cycle_with_result(client, _BUCKET, _PREFIX, cfg)

        assert result.eligible_count == 1
        assert result.deleted_count == 0  # dry run: nothing actually deleted
        assert result.deleted_bytes == 2_000_000_010  # bytes that would be deleted
        assert result.error is None

        # remove_objects should never be called in dry run
        client.remove_objects.assert_not_called()


class TestScenarioDiagnosticLevelProgression:
    """Run GC cycle at different storage levels and verify diagnostic
    level transitions: OK -> WARN -> WARN -> ERROR -> ERROR."""

    def test_scenario_diagnostic_level_progression(self):
        cfg = _make_config(max_storage_gb=10)

        # 1. Under threshold (50%) -> OK
        result_under = GCCycleResult(
            total_bytes=5_000_000_000,
            eligible_count=2,
        )
        assert compute_diagnostic_level(cfg, result_under) == 0

        # 2. Approaching threshold (85%) -> WARN
        result_approaching = GCCycleResult(
            total_bytes=8_500_000_000,
            eligible_count=2,
        )
        assert compute_diagnostic_level(cfg, result_approaching) == 1

        # 3. Over threshold with eligible recordings -> WARN
        result_over_eligible = GCCycleResult(
            total_bytes=12_000_000_000,
            eligible_count=3,
        )
        assert compute_diagnostic_level(cfg, result_over_eligible) == 1

        # 4. Over threshold with NO eligible recordings -> ERROR
        result_over_none = GCCycleResult(
            total_bytes=12_000_000_000,
            eligible_count=0,
        )
        assert compute_diagnostic_level(cfg, result_over_none) == 2

        # 5. Exception during cycle -> ERROR
        result_error = GCCycleResult(
            total_bytes=5_000_000_000,
            error="connection refused",
        )
        assert compute_diagnostic_level(cfg, result_error) == 2


# ---------------------------------------------------------------------------
# Nested directory tree
# ---------------------------------------------------------------------------


class TestScenarioNestedTreeGcCycle:
    """Recordings at various depths in a nested tree. GC discovers leaf
    directories correctly and age strategy selects the right ones."""

    def test_scenario_nested_tree_discovery_and_deletion(self):
        now = datetime.now(timezone.utc)
        client = MagicMock()

        client.list_objects.return_value = [
            # Deep: robot1/2025-01/sess_a/ (old, eligible)
            _make_s3_object(
                "robot1/2025-01/sess_a/data.mcap",
                500,
                last_modified=now - timedelta(hours=50),
            ),
            _make_s3_object(
                "robot1/2025-01/sess_a/.uploading_complete",
                10,
                last_modified=now - timedelta(hours=50),
            ),
            # Deep: robot1/2025-01/sess_b/ (recent, eligible)
            _make_s3_object(
                "robot1/2025-01/sess_b/data.mcap",
                300,
                last_modified=now - timedelta(hours=1),
            ),
            _make_s3_object(
                "robot1/2025-01/sess_b/.uploading_complete",
                10,
                last_modified=now - timedelta(hours=1),
            ),
            # Shallow: robot2/sess_c/ (old, eligible)
            _make_s3_object(
                "robot2/sess_c/data.mcap", 200, last_modified=now - timedelta(hours=50)
            ),
            _make_s3_object(
                "robot2/sess_c/.uploading_complete",
                10,
                last_modified=now - timedelta(hours=50),
            ),
        ]
        client.get_object.side_effect = Exception("not found")
        client.remove_objects.return_value = iter([])

        cfg = _make_config(strategy=["marker", "age"], max_age_hours=24)

        # Step 1: Discovery finds 3 leaf directories at different depths
        recordings = discover_recordings(client, _BUCKET, _PREFIX, cfg)
        prefixes = {r.prefix for r in recordings}
        assert "robot1/2025-01/sess_a/" in prefixes
        assert "robot1/2025-01/sess_b/" in prefixes
        assert "robot2/sess_c/" in prefixes
        assert len(recordings) == 3

        # Step 2: Enrichment + selection
        enrich_completed_at(client, _BUCKET, cfg, recordings)
        to_delete = select_for_deletion(recordings, cfg)

        # sess_a (50h) and sess_c (50h) old enough, sess_b (1h) too recent
        deleted_prefixes = {r.prefix for r in to_delete}
        assert "robot1/2025-01/sess_a/" in deleted_prefixes
        assert "robot2/sess_c/" in deleted_prefixes
        assert "robot1/2025-01/sess_b/" not in deleted_prefixes


# ---------------------------------------------------------------------------
# Orphan lifecycle
# ---------------------------------------------------------------------------


class TestScenarioOrphanCleanupLifecycle:
    """Three recordings without storage markers in different states:
    stale orphan (deleted), active recording (protected), completed
    awaiting upload (protected). Only the orphan is cleaned."""

    def test_scenario_orphan_classification(self):
        now = datetime.now(timezone.utc)

        # Stale orphan: no markers, 5h old -> deleted
        orphan = _make_recording(
            "abandoned/",
            completion_marker=False,
            storage_marker=False,
            latest_object_modified=now - timedelta(hours=5),
        )
        # Active: no markers, 10min old -> protected (might be recording)
        active = _make_recording(
            "in_progress/",
            completion_marker=False,
            storage_marker=False,
            latest_object_modified=now - timedelta(minutes=10),
        )
        # Awaiting upload: has completion marker, no storage -> kept
        awaiting = _make_recording(
            "awaiting_upload/",
            completion_marker=True,
            storage_marker=False,
            latest_object_modified=now - timedelta(hours=5),
        )

        cfg = _make_config(orphan_age_hours=1)
        orphans = select_orphans([orphan, active, awaiting], cfg)

        assert len(orphans) == 1
        assert orphans[0].prefix == "abandoned/"


# ---------------------------------------------------------------------------
# Branch pruning
# ---------------------------------------------------------------------------


class TestScenarioBranchPruningAfterDeletion:
    """After deleting deep recordings, empty ancestors are pruned
    bottom-up. Non-empty branches preserved."""

    def test_scenario_prune_empty_keep_nonempty(self):
        client = MagicMock()
        client.remove_objects.return_value = iter([])

        def _list_objects(bucket, prefix, recursive=False):
            if "2025-01" in prefix:
                return []  # empty after sess_a deleted
            if prefix == "robot1/":
                # Still has another date directory
                return [_make_s3_object("robot1/2025-02/sess_x/data.mcap", 100)]
            return []

        client.list_objects.side_effect = _list_objects

        cfg = _make_config()
        pruned = prune_empty_branches(
            client, _BUCKET, _PREFIX, cfg, ["robot1/2025-01/sess_a/"]
        )

        # robot1/2025-01/ pruned (empty), robot1/ kept (has 2025-02/)
        assert pruned == 1

    def test_scenario_prune_stops_at_configured_prefix(self):
        """Never prunes above the configured s3_prefix."""
        client = MagicMock()
        client.list_objects.return_value = []
        client.remove_objects.return_value = iter([])

        cfg = _make_config()
        pruned = prune_empty_branches(
            client, _BUCKET, "myrobot/", cfg, ["myrobot/rec/"]
        )

        # "myrobot/" is the root prefix -- nothing above to prune
        assert pruned == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestScenarioEmptyBucket:
    """GC cycle on empty bucket -- no errors, all counts zero."""

    def test_scenario_empty_bucket(self):
        client = MagicMock()
        client.list_objects.return_value = []

        cfg = _make_config(strategy=["marker", "age"], max_age_hours=24)
        result = run_gc_cycle_with_result(client, _BUCKET, _PREFIX, cfg)

        assert result.total_recordings == 0
        assert result.eligible_count == 0
        assert result.deleted_count == 0
        assert result.orphan_count == 0
        assert result.pruned_branches == 0
        assert result.error is None


class TestScenarioMixedMarkerStates:
    """All four recording classifications coexist. Each mechanism targets
    only its correct subset."""

    def test_scenario_mixed_classification(self):
        now = datetime.now(timezone.utc)

        # Good, stored, old -> GC eligible
        stored_old = _make_recording(
            "stored_old/",
            storage_marker=True,
            completion_marker=True,
            completed_at=now - timedelta(hours=50),
        )
        # Good, stored, recent -> not old enough for age
        stored_recent = _make_recording(
            "stored_recent/",
            storage_marker=True,
            completion_marker=True,
            completed_at=now - timedelta(hours=1),
        )
        # Good, not uploaded -> kept (awaiting upload)
        awaiting = _make_recording(
            "awaiting/",
            storage_marker=False,
            completion_marker=True,
            completed_at=now - timedelta(hours=50),
        )
        # Bad orphan, stale -> orphan cleanup
        orphan = _make_recording(
            "orphan/",
            storage_marker=False,
            completion_marker=False,
            latest_object_modified=now - timedelta(hours=50),
        )
        # Bad orphan, recent -> protected (active recording)
        active = _make_recording(
            "active/",
            storage_marker=False,
            completion_marker=False,
            latest_object_modified=now - timedelta(minutes=10),
        )

        all_recs = [stored_old, stored_recent, awaiting, orphan, active]
        cfg = _make_config(
            strategy=["marker", "age"], max_age_hours=24, orphan_age_hours=1
        )

        # Age strategy targets only stored_old
        to_delete = select_for_deletion(all_recs, cfg)
        assert [r.prefix for r in to_delete] == ["stored_old/"]

        # Orphan cleanup targets only orphan
        orphans = select_orphans(all_recs, cfg)
        assert [r.prefix for r in orphans] == ["orphan/"]

        # Awaiting, active, stored_recent untouched
        all_targeted = {r.prefix for r in to_delete + orphans}
        assert "awaiting/" not in all_targeted
        assert "active/" not in all_targeted
        assert "stored_recent/" not in all_targeted
