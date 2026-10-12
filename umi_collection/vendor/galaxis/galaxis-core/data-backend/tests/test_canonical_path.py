"""Tests for canonical path computation.

Test cases ported from local-data-workflow to ensure identical output.
"""

import json
from datetime import datetime, timezone

import pytest

from data_backend.canonical_path import (
    UNKNOWN_PATH_VALUE,
    CanonicalPathFields,
    _normalize_path_value,
)


def _make_meta_json(
    *,
    uuid="550e8400-e29b-41d4-a716-446655440000",
    organization="AIROA Lab",
    site="Tokyo HQ",
    location="Floor / 2",
    robot_type="UMI",
    robot_id="Robot-01",
    start_time=1794572400.123,
) -> str:
    return json.dumps(
        {
            "$schema": "https://example.com/v2_0.json",
            "schema_version": "2.0",
            "uuid": uuid,
            "robot": {
                "type": robot_type,
                "id": robot_id,
                "uri": None,
                "checksum": None,
            },
            "files": [{"type": "mcap", "name": "recording_0.mcap", "checksum": None}],
            "environment": {"type": "real_world", "site": site, "location": location},
            "runner": {
                "type": "operator",
                "organization": organization,
                "name": "op-1",
            },
            "devices": [],
            "programs": [],
            "episode": {
                "start_time": start_time,
                "end_time": start_time + 1.0,
                "success": True,
                "label": "ep-a",
            },
            "labels": ["ep-a"],
            "segments": [],
        }
    )


class TestCanonicalPrefix:
    """Must match local-data-workflow output exactly."""

    def test_full_metadata(self):
        started_at = datetime(2026, 11, 12, 10, 0, 0, 123000, tzinfo=timezone.utc)
        fields = CanonicalPathFields.from_meta_json(
            _make_meta_json(start_time=started_at.timestamp())
        )

        assert fields.organization == "AIROA Lab"
        assert fields.site == "Tokyo HQ"
        assert fields.location == "Floor / 2"
        assert fields.robot_type == "UMI"
        assert fields.robot_id == "Robot-01"
        assert fields.timestamp_rfc3339 == "2026-11-12T10:00:00.123Z"
        assert fields.canonical_prefix == (
            "org=airoa-lab/site=tokyo-hq/location=floor-2/date=2026-11-12/"
            "robot_type=umi/robot_id=robot-01/ts=2026-11-12T10:00:00.123Z/"
            "uuid=550e8400-e29b-41d4-a716-446655440000"
        )

    def test_empty_fields_fall_back_to_unknown(self):
        fields = CanonicalPathFields.from_meta_json(
            _make_meta_json(
                organization="",
                site="",
                location="",
                robot_type="",
                robot_id="",
                start_time=0.0,
            )
        )

        assert fields.canonical_prefix == (
            "org=unknown/site=unknown/location=unknown/date=1970-01-01/"
            "robot_type=unknown/robot_id=unknown/ts=1970-01-01T00:00:00.000Z/"
            "uuid=550e8400-e29b-41d4-a716-446655440000"
        )

    def test_uppercase_uuid_lowered(self):
        fields = CanonicalPathFields.from_meta_json(
            _make_meta_json(uuid="550E8400-E29B-41D4-A716-446655440000")
        )
        assert "uuid=550e8400-e29b-41d4-a716-446655440000" in fields.canonical_prefix

    def test_missing_optional_fields(self):
        data = {
            "schema_version": "2.0",
            "uuid": "abc-123",
            "robot": {},
            "environment": {},
            "runner": {},
            "episode": {},
            "files": [],
        }
        fields = CanonicalPathFields.from_meta_json(json.dumps(data))
        assert "org=unknown" in fields.canonical_prefix
        assert "site=unknown" in fields.canonical_prefix
        assert "robot_type=unknown" in fields.canonical_prefix
        assert "robot_id=unknown" in fields.canonical_prefix

    def test_missing_uuid_raises(self):
        data = {
            "schema_version": "2.0",
            "robot": {},
            "environment": {},
            "runner": {},
            "episode": {},
            "files": [],
        }
        with pytest.raises(ValueError, match="uuid"):
            CanonicalPathFields.from_meta_json(json.dumps(data))

    def test_empty_uuid_raises(self):
        data = {
            "schema_version": "2.0",
            "uuid": "",
            "robot": {},
            "environment": {},
            "runner": {},
            "episode": {},
            "files": [],
        }
        with pytest.raises(ValueError, match="uuid"):
            CanonicalPathFields.from_meta_json(json.dumps(data))


class TestNormalizePathValue:
    def test_lowercase(self):
        assert _normalize_path_value("HELLO") == "hello"

    def test_spaces_to_dashes(self):
        assert _normalize_path_value("hello world") == "hello-world"

    def test_slashes_to_dashes(self):
        assert _normalize_path_value("Floor / 2") == "floor-2"

    def test_special_chars_to_dashes(self):
        assert _normalize_path_value("test@#$value") == "test-value"

    def test_multi_dashes_collapsed(self):
        assert _normalize_path_value("a---b") == "a-b"

    def test_strips_leading_trailing_dashes(self):
        assert _normalize_path_value("-hello-") == "hello"

    def test_none_returns_unknown(self):
        assert _normalize_path_value(None) == UNKNOWN_PATH_VALUE

    def test_empty_returns_unknown(self):
        assert _normalize_path_value("") == UNKNOWN_PATH_VALUE

    def test_whitespace_only_returns_unknown(self):
        assert _normalize_path_value("   ") == UNKNOWN_PATH_VALUE

    def test_unicode_nfkc_normalization(self):
        # fullwidth A → normal a
        assert _normalize_path_value("\uff21") == "a"

    def test_traversal_sequences_neutralized(self):
        assert _normalize_path_value("../../../etc") == "etc"
        assert _normalize_path_value("..\\..\\etc") == "etc"

    def test_dot_only_returns_unknown(self):
        assert _normalize_path_value("...") == UNKNOWN_PATH_VALUE
