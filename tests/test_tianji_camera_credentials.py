"""Camera credentials persist per user; no camera/robot/network is opened."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from teleop_control import tianji
from teleop_control import tianji_camera_credentials as credentials


@pytest.fixture
def local_config(tmp_path, monkeypatch):
    monkeypatch.delenv("TIANJI_CAMERA_CREDENTIALS", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    source = tmp_path / "sdk/client.json"
    source.parent.mkdir()
    # A public system CA is sufficient to exercise certificate parsing/copying.
    pem = (Path(__file__).parent / "fixtures/camera_test_ca.pem").read_text()
    (source.parent / "server.pem").write_text(pem)
    source.write_text(json.dumps(dict(token="fixture-secret", certificate="server.pem", port=57491)))
    return source


def test_import_is_private_relocatable_and_survives_workspace_upgrade(local_config, tmp_path):
    path = credentials.import_credentials(local_config)
    saved = json.loads(path.read_text())
    assert path == credentials.credentials_path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not Path(saved["certificate"]).is_absolute()
    assert (path.parent / saved["certificate"]).read_bytes() == (local_config.parent / "server.pem").read_bytes()
    assert saved["token"] == "fixture-secret"
    local_config.unlink()
    (local_config.parent / "server.pem").unlink()
    assert credentials.resolve_credentials(tmp_path / "workspace-v2/runtime/tianji") == path


def test_legacy_workspace_is_migrated_with_its_certificate(local_config, tmp_path):
    runtime = local_config.parent
    local_config.rename(runtime / "camera-client.json")
    saved = credentials.resolve_credentials(runtime)
    assert saved != runtime / "camera-client.json"
    assert (saved.parent / json.loads(saved.read_text())["certificate"]).is_file()


def test_old_version_workspace_is_discovered(local_config, tmp_path):
    import shutil
    legacy = tmp_path / "data" / credentials.APP_ID / "workspaces/old/teleop_control/runtime/tianji"
    legacy.mkdir(parents=True)
    shutil.copy2(local_config, legacy / "camera-client.json")
    shutil.copy2(local_config.parent / "server.pem", legacy / "server.pem")
    assert credentials.resolve_credentials(tmp_path / "new/runtime") == credentials.credentials_path()
    assert credentials.credentials_path().is_file()


@pytest.mark.parametrize("change", [dict(token=""), dict(certificate="missing.pem"), dict(port=True), dict(port=70000)])
def test_invalid_import_does_not_replace_working_authorization(local_config, change):
    path = credentials.import_credentials(local_config)
    original = path.read_bytes()
    data = json.loads(local_config.read_text());data.update(change)
    local_config.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        credentials.import_credentials(local_config)
    assert path.read_bytes() == original


def test_explicit_path_is_respected(local_config, tmp_path, monkeypatch):
    path = tmp_path / "custom/client.json"
    monkeypatch.setenv("TIANJI_CAMERA_CREDENTIALS", str(path))
    assert credentials.import_credentials(local_config) == path
    assert credentials.resolve_credentials(tmp_path / "runtime") == path


def test_missing_config_has_actionable_error_without_workspace_path(local_config, monkeypatch):
    monkeypatch.setattr(tianji, "camera_host", lambda: "192.0.2.1")
    monkeypatch.setattr(tianji, "resolve_credentials", lambda runtime: Path("/missing/client.json"))
    factory = Mock(side_effect=FileNotFoundError("/missing/client.json"))
    import sys
    monkeypatch.setitem(sys.modules, "fusion_camera", SimpleNamespace(FusionStreamClient=factory))
    with pytest.raises(RuntimeError, match="导入 client.json") as exc:
        object.__new__(tianji.TianjiManager).new_camera_client()
    assert "/missing" not in str(exc.value)


@pytest.mark.parametrize("blocked", ["armed", "busy", "recording", "running", "other_task", None])
def test_import_requires_idle_and_drops_stale_preview(local_config, monkeypatch, blocked):
    from threading import RLock
    manager = object.__new__(tianji.TianjiManager)
    manager.command_lock = manager.lock = RLock()
    manager.controller = SimpleNamespace(hardware_enabled=True)
    state = dict(state="running" if blocked == "running" else "idle", collection={})
    if blocked in ("armed", "busy", "recording"):
        state["collection"][blocked] = True
    manager.snapshot = Mock(return_value=state)
    manager.require_other_idle = Mock(side_effect=RuntimeError("其他任务正在运行") if blocked == "other_task" else None)
    manager.camera_thread = Mock(is_alive=Mock(return_value=True))
    manager.pause_cameras = Mock()
    manager.frames = {"usb_1": b"old-frame"}
    manager.camera_error = "old-error"
    manager.camera_phase = "fault"
    if blocked:
        with pytest.raises(RuntimeError):
            manager.import_camera_credentials(local_config)
        manager.pause_cameras.assert_not_called()
        assert not credentials.credentials_path().exists()
        assert manager.frames == {"usb_1": b"old-frame"}
    else:
        assert manager.import_camera_credentials(local_config) == {"ok": True}
        manager.pause_cameras.assert_called_once()
        assert manager.frames == {} and manager.camera_error == ""
        assert manager.camera_phase == "idle"
        assert credentials.credentials_path().is_file()
