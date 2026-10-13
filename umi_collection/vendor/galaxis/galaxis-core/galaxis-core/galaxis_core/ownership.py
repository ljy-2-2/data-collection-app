"""Host ownership helpers for files persisted by privileged ROS nodes."""

import os
from pathlib import Path


def resolve_record_owner(record_base_dir: str) -> tuple[int, int]:
    """Resolve the persisted-data owner from explicit env or mount metadata."""
    owner = os.stat(record_base_dir)
    owner_uid = os.getenv("GALAXIS_DATA_UID")
    owner_gid = os.getenv("GALAXIS_DATA_GID")
    if (owner_uid is None) != (owner_gid is None):
        raise ValueError("GALAXIS_DATA_UID and GALAXIS_DATA_GID must be set together")
    if owner_uid is None:
        return owner.st_uid, owner.st_gid
    try:
        uid, gid = int(owner_uid), int(owner_gid)
    except ValueError as exc:
        raise ValueError(
            "GALAXIS_DATA_UID and GALAXIS_DATA_GID must be non-negative integers"
        ) from exc
    if uid < 0 or gid < 0:
        raise ValueError(
            "GALAXIS_DATA_UID and GALAXIS_DATA_GID must be non-negative integers"
        )
    return uid, gid


def normalize_tree_access(path: str, uid: int, gid: int) -> None:
    """Recursively apply operator ownership and mode 777 without following links."""
    for root, dirs, files in os.walk(path, followlinks=False):
        os.chown(root, uid, gid)
        os.chmod(root, 0o777)
        for name in [*dirs, *files]:
            child = Path(root) / name
            if not child.is_symlink():
                os.chown(child, uid, gid)
                os.chmod(child, 0o777)
