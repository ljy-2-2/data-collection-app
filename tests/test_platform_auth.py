import json
import stat
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "teleop_control" / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from teleop_control.platform_auth import PlatformBearerAuth, PlatformLogin, PlatformTokenStore  # noqa: E402
from run_app import clear_previous_app_login  # noqa: E402


def test_token_store_persists_only_token_and_user_with_private_permissions(tmp_path):
    path = tmp_path / "auth.json"
    store = PlatformTokenStore(path)
    store.write(
        token="session-token",
        expires_in=3600,
        user={"id": "user-1", "username": "collector01", "is_admin": True, "is_active": True},
    )

    payload = json.loads(path.read_text())
    assert payload["access_token"] == "session-token"
    assert payload["user"]["username"] == "collector01"
    assert "password" not in payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_http_client_reads_latest_token_for_each_upload_request(tmp_path):
    store = PlatformTokenStore(tmp_path / "auth.json")
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler), auth=PlatformBearerAuth(store))
    client.get("https://platform.example/first")
    store.write(token="new-token", expires_in=3600, user={"username": "collector01"})
    client.get("https://platform.example/second")

    assert seen == [None, "Bearer new-token"]


def test_logout_clears_local_token_before_remote_request(tmp_path, monkeypatch):
    store = PlatformTokenStore(tmp_path / "auth.json")
    store.write(token="session-token", expires_in=3600, user={"username": "collector01"})

    def unavailable(_url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer session-token"
        assert store.token() == ""
        raise httpx.ConnectError("platform unavailable")

    monkeypatch.setattr(httpx, "post", unavailable)
    PlatformLogin("https://platform.example", store).logout()

    assert store.token() == ""


def test_upload_auth_rejects_account_switch_before_request(tmp_path):
    store = PlatformTokenStore(tmp_path / "auth.json")
    seen = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),
                      auth=PlatformBearerAuth(store)) as client:
        store.write(token="synthetic-a", expires_in=3600, user={"username": "A"})
        client.post("https://platform.test/episodes", extensions={"expected_platform_username": "A"})
        store.write(token="synthetic-b", expires_in=3600, user={"username": "B"})
        with pytest.raises(RuntimeError, match="账号已变化"):
            client.post("https://platform.test/episodes", extensions={"expected_platform_username": "A"})
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == "Bearer synthetic-a"


def test_new_app_launch_requires_login_again(tmp_path):
    path = tmp_path / "app-auth.json"
    store = PlatformTokenStore(path)
    store.write(token="session-token", expires_in=3600, user={"username": "collector01"})

    clear_previous_app_login({"PLATFORM_TOKEN_FILE": str(path)})

    assert store.token() == ""
