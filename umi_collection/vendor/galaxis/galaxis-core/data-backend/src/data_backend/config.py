"""Storage configuration: targets, GC strategy, upload priority.

Parses ``upload_targets.yaml`` into typed dataclasses.  Pure Python,
no ROS dependency.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum

import yaml

logger = logging.getLogger("data_backend.config")

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Priority(Enum):
    """Upload priority — controls retry and local deletion behaviour."""

    REQUIRED = "required"
    PREFERRED = "preferred"
    OPTIONAL = "optional"


class PathRule(Enum):
    """S3 object key layout.

    FLAT:      ``{prefix}{dir_name}/{filename}``
    CANONICAL: ``{prefix}{canonical_prefix}/{filename}``
               where *canonical_prefix* is derived from ``meta.json``::

                   org={org}/site={site}/location={loc}/date={date}/
                   robot_type={type}/robot_id={id}/ts={ts}/uuid={uuid}
    """

    FLAT = "flat"
    CANONICAL = "canonical"


# ---------------------------------------------------------------------------
# GC strategy
# ---------------------------------------------------------------------------

_VALID_CONDITIONS = frozenset({"marker", "age", "space"})


@dataclass(frozen=True)
class GCStrategy:
    """Parsed GC strategy predicate.

    ``evaluate(marker, age, space)`` returns True when the recording
    should be deleted.
    """

    _predicate: object  # Callable[[bool, bool, bool], bool]
    description: str

    def evaluate(self, *, marker: bool, age: bool, space: bool) -> bool:
        return self._predicate(marker, age, space)  # type: ignore[operator]


def parse_gc_strategy(cfg: list | str) -> GCStrategy:
    """Parse a YAML gc.strategy value into a :class:`GCStrategy`.

    Top-level list = AND.  ``any_of`` dict key = OR.

    Examples::

        ["marker", "age"]                         # marker AND age
        ["marker", {"any_of": ["age", "space"]}]  # marker AND (age OR space)
        ["marker"]                                 # marker only
    """
    if isinstance(cfg, str):
        if cfg in _VALID_CONDITIONS:
            cfg = [cfg]
        else:
            raise ValueError(
                f"Invalid gc_strategy string {cfg!r}. "
                f"Use a YAML list of conditions: {sorted(_VALID_CONDITIONS)}"
            )

    if not isinstance(cfg, list) or not cfg:
        raise ValueError(f"gc_strategy must be a non-empty list, got {cfg!r}")

    predicates = []
    for item in cfg:
        if isinstance(item, str):
            if item not in _VALID_CONDITIONS:
                raise ValueError(
                    f"Unknown GC condition {item!r}. Valid: {sorted(_VALID_CONDITIONS)}"
                )
            predicates.append(_make_condition(item))
        elif isinstance(item, dict) and "any_of" in item:
            children = item["any_of"]
            if not isinstance(children, list) or not children:
                raise ValueError("any_of must be a non-empty list")
            or_preds = []
            for child in children:
                if child not in _VALID_CONDITIONS:
                    raise ValueError(
                        f"Unknown GC condition {child!r} in any_of. "
                        f"Valid: {sorted(_VALID_CONDITIONS)}"
                    )
                or_preds.append(_make_condition(child))
            predicates.append(
                lambda m, a, s, _ps=or_preds: any(p(m, a, s) for p in _ps)
            )
        else:
            raise ValueError(
                f"Invalid gc_strategy item {item!r}. "
                f"Expected a condition name or {{any_of: [...]}}."
            )

    desc = str(cfg)

    def predicate(m, a, s, _ps=predicates):
        return all(p(m, a, s) for p in _ps)

    return GCStrategy(_predicate=predicate, description=desc)


def _make_condition(name: str):
    if name == "marker":
        return lambda m, a, s: m
    if name == "age":
        return lambda m, a, s: a
    if name == "space":
        return lambda m, a, s: s
    raise ValueError(f"Unknown condition: {name!r}")


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GCConfig:
    """Per-target GC configuration."""

    strategy: GCStrategy
    max_age_hours: float = 0.0
    max_storage_gb: float = 0.0
    orphan_age_hours: float = 1.0
    check_interval_sec: int = 300
    completion_marker: str = ".recording_complete"
    storage_marker: str = ".uploading_complete"
    dry_run: bool = False


@dataclass(frozen=True)
class TargetConfig:
    """One S3 upload target."""

    name: str
    endpoint: str
    access_key: str
    secret_key: str
    use_ssl: bool = False
    verify_ssl: bool = True
    bucket: str = "data"
    prefix: str = ""
    path_rule: PathRule = PathRule.FLAT
    priority: Priority = Priority.REQUIRED
    gc: GCConfig | None = None


@dataclass(frozen=True)
class StorageConfig:
    """Top-level storage configuration from upload_targets.yaml."""

    targets: list[TargetConfig] = field(default_factory=list)
    state_db: str = "/var/lib/galaxis/upload_state.db"
    state_purge_age_hours: float = 720.0
    delete_after_upload: bool = False
    local_retention_hours: int = 24


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

_TARGET_DEFAULTS = {
    "use_ssl": False,
    "verify_ssl": True,
    "bucket": "data",
    "prefix": "",
    "path_rule": "flat",
    "priority": "required",
}


def load_storage_config(path: str) -> StorageConfig:
    """Load ``upload_targets.yaml`` and return a typed :class:`StorageConfig`."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Expected a YAML dict in {path}, got {type(raw).__name__}")

    defaults = {**_TARGET_DEFAULTS, **raw.get("defaults", {})}

    targets = []
    for name, tcfg in raw.get("targets", {}).items():
        if not isinstance(tcfg, dict):
            continue
        if not tcfg.get("enabled", True):
            continue

        merged = {**defaults, **tcfg}

        access_key = merged.get("access_key") or os.environ.get(
            merged.get("access_key_env", ""), ""
        )
        secret_key = merged.get("secret_key") or os.environ.get(
            merged.get("secret_key_env", ""), ""
        )

        raw_prefix = str(merged.get("prefix", "")).strip("/")
        prefix = f"{raw_prefix}/" if raw_prefix else ""

        gc = _parse_target_gc(merged.get("gc"))

        targets.append(
            TargetConfig(
                name=name,
                endpoint=merged["endpoint"],
                access_key=access_key,
                secret_key=secret_key,
                use_ssl=merged.get("use_ssl", False),
                verify_ssl=merged.get("verify_ssl", True),
                bucket=merged.get("bucket", "data"),
                prefix=prefix,
                path_rule=PathRule(merged.get("path_rule", "flat")),
                priority=Priority(merged.get("priority", "required")),
                gc=gc,
            )
        )

    return StorageConfig(
        targets=targets,
        state_db=raw.get("state_db", "/var/lib/galaxis/upload_state.db"),
        state_purge_age_hours=float(raw.get("state_purge_age_hours", 720)),
        delete_after_upload=bool(raw.get("delete_after_upload", False)),
        local_retention_hours=int(raw.get("local_retention_hours", 24)),
    )


def _parse_target_gc(gc_raw) -> GCConfig | None:
    """Parse a target's ``gc:`` block into a :class:`GCConfig` or None."""
    if gc_raw is None or gc_raw == "none":
        return None
    if not isinstance(gc_raw, dict):
        raise ValueError(f"Target gc: must be a dict or 'none', got {gc_raw!r}")

    strategy_raw = gc_raw.get("strategy")
    if not strategy_raw:
        raise ValueError("Target gc: missing 'strategy' key")

    return GCConfig(
        strategy=parse_gc_strategy(strategy_raw),
        max_age_hours=float(gc_raw.get("max_age_hours", 0.0)),
        max_storage_gb=float(gc_raw.get("max_storage_gb", 0.0)),
        orphan_age_hours=float(gc_raw.get("orphan_age_hours", 1.0)),
        check_interval_sec=int(gc_raw.get("check_interval_sec", 300)),
        completion_marker=gc_raw.get("completion_marker", ".recording_complete"),
        storage_marker=gc_raw.get("storage_marker", ".uploading_complete"),
        dry_run=bool(gc_raw.get("dry_run", False)),
    )
