"""Canonical storage-path computation from recording metadata.

Produces the same path structure as local-data-workflow's canonical_path.py,
but without the rebake dependency.  Uses airoa-metadata for JSON parsing.

Canonical prefix format::

    org={org}/site={site}/location={location}/date={date}/
    robot_type={type}/robot_id={id}/ts={timestamp}/uuid={uuid}
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

UNKNOWN_PATH_VALUE = "unknown"
_PATH_VALUE_PATTERN = re.compile(r"[^\w.-]+", re.UNICODE)
_MULTI_DASH_PATTERN = re.compile(r"-{2,}")


@dataclass(frozen=True)
class CanonicalPathFields:
    """Metadata fields used to build canonical storage paths."""

    uuid: str
    organization: str
    site: str
    location: str
    robot_type: str
    robot_id: str
    started_at: datetime

    @property
    def date(self) -> str:
        return self.started_at.date().isoformat()

    @property
    def timestamp_rfc3339(self) -> str:
        return self.started_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @property
    def canonical_prefix(self) -> str:
        return (
            f"org={_normalize_path_value(self.organization)}/"
            f"site={_normalize_path_value(self.site)}/"
            f"location={_normalize_path_value(self.location)}/"
            f"date={self.date}/"
            f"robot_type={_normalize_path_value(self.robot_type)}/"
            f"robot_id={_normalize_path_value(self.robot_id)}/"
            f"ts={self.timestamp_rfc3339}/"
            f"uuid={self.uuid.lower()}"
        )

    @classmethod
    def from_meta_json(cls, meta_json_text: str) -> CanonicalPathFields:
        """Parse a meta.json string (V2.0 schema) and extract path fields."""
        data = json.loads(meta_json_text)
        robot = data.get("robot", {})
        env = data.get("environment", {})
        runner = data.get("runner", {})
        episode = data.get("episode", {})

        uuid = data.get("uuid")
        if not uuid:
            raise ValueError("meta.json missing required 'uuid' field")

        return cls(
            uuid=uuid,
            organization=runner.get("organization") or UNKNOWN_PATH_VALUE,
            site=env.get("site") or UNKNOWN_PATH_VALUE,
            location=env.get("location") or UNKNOWN_PATH_VALUE,
            robot_type=robot.get("type") or UNKNOWN_PATH_VALUE,
            robot_id=robot.get("id") or UNKNOWN_PATH_VALUE,
            started_at=_as_utc_timestamp(float(episode.get("start_time", 0.0))),
        )


def _normalize_path_value(value: str | None) -> str:
    """Normalize one metadata field into a stable object-key segment.

    Matches the normalization in local-data-workflow exactly:
    NFKC → lowercase → slashes to dashes → whitespace to dashes →
    non-word chars to dashes → collapse multi-dashes → strip edges.
    """
    if value is None:
        return UNKNOWN_PATH_VALUE

    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    if not normalized:
        return UNKNOWN_PATH_VALUE

    normalized = normalized.replace("/", "-")
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = _PATH_VALUE_PATTERN.sub("-", normalized)
    normalized = _MULTI_DASH_PATTERN.sub("-", normalized).strip("-.")
    return normalized or UNKNOWN_PATH_VALUE


def _as_utc_timestamp(timestamp_seconds: float) -> datetime:
    return datetime.fromtimestamp(timestamp_seconds, timezone.utc)
