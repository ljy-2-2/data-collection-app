"""Pure-Python recording upload and retention logic (no ROS 2 dependency)."""

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from io import BytesIO

from data_backend.canonical_path import CanonicalPathFields

RECORDING_COMPLETE_MARKER = ".recording_complete"

_logger = logging.getLogger(__name__)


_VALID_PATH_RULES = ("flat", "canonical")


def _resolve_s3_prefix(prefix: str, dir_path: str, path_rule: str) -> str:
    """Compute the S3 prefix for a recording based on the path rule.

    Returns a prefix string (ending with /) to prepend to each filename.
    """
    dir_name = os.path.basename(dir_path)

    if path_rule not in _VALID_PATH_RULES:
        _logger.warning(f"Unknown path_rule={path_rule!r} for {dir_name}, using flat.")

    if path_rule == "canonical":
        meta_path = os.path.join(dir_path, "meta.json")
        try:
            with open(meta_path) as f:
                fields = CanonicalPathFields.from_meta_json(f.read())
            return f"{prefix}{fields.canonical_prefix}/"
        except Exception as exc:
            _logger.warning(
                f"Cannot compute canonical path for {dir_name}: {exc}. "
                f"Falling back to flat path."
            )

    # Default: flat
    return f"{prefix}{dir_name}/"


def upload_recording(
    s3_client,
    bucket: str,
    prefix: str,
    dir_path: str,
    delete_after: bool = False,
    path_rule: str = "flat",
    logger=None,
) -> dict:
    """Upload a recording directory to S3.

    Args:
        path_rule: "flat" (default) uses ``{prefix}{dir_name}/``.
                   "canonical" reads meta.json and uses metadata-derived path.

    Returns dict of filename -> etag.
    """
    dir_name = os.path.basename(dir_path)
    s3_dir = _resolve_s3_prefix(prefix, dir_path, path_rule)
    uploaded_etags: dict[str, str] = {}

    for filename in os.listdir(dir_path):
        filepath = os.path.join(dir_path, filename)
        if not os.path.isfile(filepath):
            continue
        if filename == RECORDING_COMPLETE_MARKER:
            continue

        object_name = f"{s3_dir}{filename}"
        file_size = os.path.getsize(filepath)
        if logger:
            logger.info(
                f"Uploading {filepath} ({file_size} bytes) "
                f"-> s3://{bucket}/{object_name}"
            )
        result = s3_client.fput_object(bucket, object_name, filepath)
        if logger:
            logger.info(f"Uploaded {filename} (etag={result.etag})")
        uploaded_etags[filename] = result.etag

    # Write .recording_complete marker to S3
    marker_content = json.dumps(
        {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "files": uploaded_etags,
        }
    ).encode("utf-8")

    marker_object = f"{s3_dir}{RECORDING_COMPLETE_MARKER}"
    s3_client.put_object(
        bucket,
        marker_object,
        BytesIO(marker_content),
        len(marker_content),
        content_type="application/json",
    )

    if logger:
        logger.info(f"Upload complete for {dir_name} ({len(uploaded_etags)} file(s)).")

    if delete_after:
        if logger:
            logger.info(f"delete_after_upload=true, removing {dir_path}")
        shutil.rmtree(dir_path)

    return uploaded_etags


def recover_pending_uploads(base_dir: str, logger=None) -> list[str]:
    """Scan base_dir for dirs with meta.json that may need uploading.

    Returns list of directory paths.  The caller (uploader node) checks
    the state DB to decide which targets still need this recording.
    """
    if not os.path.isdir(base_dir):
        return []

    pending: list[str] = []
    for entry in os.listdir(base_dir):
        dir_path = os.path.join(base_dir, entry)
        if not os.path.isdir(dir_path):
            continue
        meta_path = os.path.join(dir_path, "meta.json")
        if os.path.exists(meta_path):
            if logger:
                logger.info(f"Recovering pending upload: {dir_path}")
            pending.append(dir_path)

    if pending and logger:
        logger.info(f"Recovered {len(pending)} pending upload(s).")

    return pending


def retention_cleanup(
    base_dir: str, retention_hours: int, delete_after_upload: bool, logger=None
) -> list[str]:
    """Remove old recordings from local disk.

    Uses directory modification time to determine age.

    Returns list of deleted directory paths.
    """
    if delete_after_upload:
        return []
    if retention_hours <= 0:
        return []
    if not os.path.isdir(base_dir):
        return []

    now = time.time()
    max_age_sec = retention_hours * 3600
    deleted: list[str] = []

    for entry in os.listdir(base_dir):
        dir_path = os.path.join(base_dir, entry)
        if not os.path.isdir(dir_path):
            continue

        dir_age = now - os.path.getmtime(dir_path)
        if dir_age > max_age_sec:
            if logger:
                logger.info(
                    f"Retention cleanup: removing {dir_path} "
                    f"(age {dir_age / 3600:.1f}h > {retention_hours}h)"
                )
            shutil.rmtree(dir_path)
            deleted.append(dir_path)

    return deleted
