"""Unit tests for BackendClient.

Pure Python – no ROS2 mocking needed. ``requests.Session`` is patched.
The ``requests`` module is mocked at sys.modules level so it doesn't need
to be installed in the test environment.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Inject a fake ``requests`` module before importing BackendClient
# ---------------------------------------------------------------------------


def _ensure_requests_mock():
    """Ensure a mock ``requests`` module is available in sys.modules."""
    if (
        "requests" not in sys.modules
        or isinstance(sys.modules["requests"], types.ModuleType)
        and not hasattr(sys.modules["requests"], "_is_test_mock")
    ):
        requests_mod = types.ModuleType("requests")
        requests_mod._is_test_mock = True

        session_cls = MagicMock()
        requests_mod.Session = session_cls

        requests_exc = types.ModuleType("requests.exceptions")

        class _RequestException(Exception):
            pass

        requests_exc.RequestException = _RequestException
        requests_mod.exceptions = requests_exc

        sys.modules["requests"] = requests_mod
        sys.modules["requests.exceptions"] = requests_exc

    return sys.modules["requests"]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    """Create a BackendClient with a mocked requests.Session."""
    requests_mod = _ensure_requests_mock()

    mock_session = MagicMock()
    requests_mod.Session.return_value = mock_session

    # Flush any cached import of backend_client
    for key in list(sys.modules):
        if key.startswith("galaxis_core"):
            monkeypatch.delitem(sys.modules, key, raising=False)

    from galaxis_core.backend_client import BackendClient

    c = BackendClient("http://api.test/v1/", "test-key")
    c._mock_session = mock_session
    c._requests_mod = requests_mod
    return c


def _make_response(status_code=200, json_data=None, content=b"something"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.text = content.decode() if isinstance(content, bytes) else str(content)
    resp.json.return_value = json_data
    return resp


# ===================================================================
# TestRequest
# ===================================================================


class TestRequest:
    """Tests for ``_request`` method."""

    def test_request_success_json(self, client):
        client._mock_session.request.return_value = _make_response(
            200, json_data={"ok": True}
        )
        result = client._request("GET", "/test")
        assert result == {"ok": True}

    def test_request_204_returns_empty_dict(self, client):
        client._mock_session.request.return_value = _make_response(204, content=b"")
        result = client._request("GET", "/test")
        assert result == {}

    def test_request_empty_content_returns_empty_dict(self, client):
        client._mock_session.request.return_value = _make_response(200, content=b"")
        result = client._request("GET", "/test")
        assert result == {}

    def test_request_4xx_returns_none(self, client):
        client._mock_session.request.return_value = _make_response(
            400, content=b"bad request"
        )
        result = client._request("GET", "/test")
        assert result is None

    def test_request_5xx_returns_none(self, client):
        client._mock_session.request.return_value = _make_response(
            500, content=b"server error"
        )
        result = client._request("GET", "/test")
        assert result is None

    def test_request_network_error_returns_none(self, client):
        RequestException = client._requests_mod.exceptions.RequestException
        client._mock_session.request.side_effect = RequestException("timeout")
        result = client._request("GET", "/test")
        assert result is None


# ===================================================================
# TestHelpers
# ===================================================================


class TestHelpers:
    """Tests for init and helper methods."""

    def test_init_strips_trailing_slash(self, client):
        assert not client.base_url.endswith("/")
        assert client.base_url == "http://api.test/v1"

    def test_init_sets_api_key_header(self, client):
        client._mock_session.headers.update.assert_called()
        call_args = client._mock_session.headers.update.call_args[0][0]
        assert call_args["X-API-Key"] == "test-key"

    def test_now_iso_returns_utc_string(self, client):
        from galaxis_core.backend_client import BackendClient

        result = BackendClient._now_iso()
        assert "+00:00" in result


# ===================================================================
# TestEndpoints
# ===================================================================


class TestEndpoints:
    """Tests for specific API endpoint wrappers."""

    def test_get_robot_self_calls_robot_me(self, client):
        client._mock_session.request.return_value = _make_response(
            200, json_data={"id": "robot-1", "active_episode_id": "ep-1"}
        )
        result = client.get_robot_self()
        call_args = client._mock_session.request.call_args
        assert "/robot/me" in call_args[0][1]
        assert result["active_episode_id"] == "ep-1"

    def test_get_episode_calls_robot_episodes(self, client):
        client._mock_session.request.return_value = _make_response(
            200, json_data={"id": "ep-1", "task_id": "task-1"}
        )
        result = client.get_episode("ep-1")
        call_args = client._mock_session.request.call_args
        assert "/robot/episodes/ep-1" in call_args[0][1]
        assert result["task_id"] == "task-1"

    def test_start_episode_sends_occurred_at(self, client):
        client._mock_session.request.return_value = _make_response(
            200, json_data={"status": "started"}
        )
        client.start_episode("ep-123")
        call_args = client._mock_session.request.call_args
        assert call_args[1]["json"]["occurred_at"] is not None

    def test_list_episodes_returns_list(self, client):
        episodes = [{"id": "ep-1"}, {"id": "ep-2"}]
        client._mock_session.request.return_value = _make_response(
            200, json_data=episodes
        )
        result = client.list_episodes()
        call_args = client._mock_session.request.call_args
        assert "/robot/episodes" in call_args[0][1]
        assert result == episodes

    def test_list_episodes_returns_none_on_failure(self, client):
        client._mock_session.request.return_value = _make_response(
            500, content=b"error"
        )
        result = client.list_episodes()
        assert result is None

    def test_create_execution_returns_id(self, client):
        client._mock_session.request.return_value = _make_response(
            200, json_data={"execution_id": "exec-456"}
        )
        result = client.create_execution("ep-123", "st-789")
        assert result == "exec-456"

    def test_create_execution_returns_none_on_failure(self, client):
        client._mock_session.request.return_value = _make_response(
            500, content=b"error"
        )
        result = client.create_execution("ep-123", "st-789")
        assert result is None


# ===================================================================
# TestOfflineBackendRepeat
# ===================================================================


class TestOfflineBackendRepeat:
    """Tests for OfflineBackend.repeat_last_episode clone-chain prevention."""

    def _make_backend(self):
        _ensure_requests_mock()
        from galaxis_core.backend_client import OfflineBackend

        return OfflineBackend("")

    def test_repeat_clones_from_completed_not_last_created(self):
        backend = self._make_backend()
        # Seed an episode and finish it
        ep1 = backend._create_episode_from_task(
            {"id": "t1", "name": "task", "subtasks": [{"id": "s1", "name": "step"}]}
        )
        backend.finish_episode(ep1["id"])

        # First repeat
        r1 = backend.repeat_last_episode()
        assert r1 is not None
        assert r1["id"] != ep1["id"]
        assert r1["task_id"] == "t1"

        # Second repeat — should clone from ep1 (completed), not r1 (pending)
        r2 = backend.repeat_last_episode()
        assert r2 is not None
        assert r2["id"] != r1["id"]
        assert r2["task_id"] == "t1"

    def test_repeat_falls_back_to_last_when_none_completed(self):
        backend = self._make_backend()
        # Create episode but don't finish it
        ep1 = backend._create_episode_from_task(
            {"id": "t1", "name": "task", "subtasks": [{"id": "s1", "name": "step"}]}
        )
        assert ep1["status"] == "pending"

        result = backend.repeat_last_episode()
        assert result is not None
        assert result["task_id"] == "t1"
