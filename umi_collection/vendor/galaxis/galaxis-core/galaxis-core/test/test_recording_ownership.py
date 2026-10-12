"""Tests for host identity selection without requiring a ROS installation."""

import pytest

from unittest.mock import patch

from galaxis_core.ownership import normalize_tree_access, resolve_record_owner


def test_explicit_operator_identity_overrides_mount_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("GALAXIS_DATA_UID", "1234")
    monkeypatch.setenv("GALAXIS_DATA_GID", "5678")
    assert resolve_record_owner(str(tmp_path)) == (1234, 5678)


def test_mount_owner_is_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("GALAXIS_DATA_UID", raising=False)
    monkeypatch.delenv("GALAXIS_DATA_GID", raising=False)
    stat_result = tmp_path.stat()
    assert resolve_record_owner(str(tmp_path)) == (
        stat_result.st_uid,
        stat_result.st_gid,
    )


def test_partial_operator_identity_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GALAXIS_DATA_UID", "1000")
    monkeypatch.delenv("GALAXIS_DATA_GID", raising=False)
    with pytest.raises(ValueError, match="must be set together"):
        resolve_record_owner(str(tmp_path))


@pytest.mark.parametrize("uid,gid", [("root", "1000"), ("1000", "-1")])
def test_invalid_operator_identity_is_rejected(tmp_path, monkeypatch, uid, gid):
    monkeypatch.setenv("GALAXIS_DATA_UID", uid)
    monkeypatch.setenv("GALAXIS_DATA_GID", gid)
    with pytest.raises(ValueError, match="non-negative integers"):
        resolve_record_owner(str(tmp_path))


def test_normalize_tree_access_sets_every_regular_path_to_777(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir(mode=0o700)
    payload = nested / "record.mcap"
    payload.write_text("mcap")
    payload.chmod(0o600)
    link = tmp_path / "record-link"
    link.symlink_to(payload)

    with patch("galaxis_core.ownership.os.chown") as chown:
        normalize_tree_access(str(tmp_path), 1000, 1000)

    assert tmp_path.stat().st_mode & 0o777 == 0o777
    assert nested.stat().st_mode & 0o777 == 0o777
    assert payload.stat().st_mode & 0o777 == 0o777
    changed = {str(call.args[0]) for call in chown.call_args_list}
    assert str(tmp_path) in changed
    assert str(nested) in changed
    assert str(payload) in changed
    assert str(link) not in changed
