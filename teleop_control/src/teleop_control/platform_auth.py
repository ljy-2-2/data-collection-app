from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .login_credentials import CredentialError, LoginPreferences


def default_token_file() -> Path:
    configured = os.getenv("PLATFORM_TOKEN_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "galaxis-data-collection" / "platform-auth.json"


class PlatformTokenStore:
    """Persist one user session token locally; passwords are never stored."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_token_file()

    def read(self) -> dict[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, str):
            try:
                if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(UTC):
                    self.clear()
                    return {}
            except ValueError:
                self.clear()
                return {}
        return payload

    def write(self, *, token: str, expires_in: int, user: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        payload = {
            "access_token": token,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
            "user": user,
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def token(self) -> str:
        value = self.read().get("access_token")
        return value if isinstance(value, str) else ""

    def user(self) -> dict[str, object] | None:
        value = self.read().get("user")
        return value if isinstance(value, dict) else None


class PlatformBearerAuth(httpx.Auth):
    def __init__(self, store: PlatformTokenStore) -> None:
        self.store = store

    def auth_flow(self, request: httpx.Request):
        record = self.store.read()
        expected = request.extensions.get("expected_platform_username")
        if expected and (record.get("user") or {}).get("username") != expected:
            raise RuntimeError("平台登录账号已变化，等待采集账号重新登录后上传")
        token = record.get("access_token")
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request


class PlatformLogin:
    def __init__(self, api_url: str, store: PlatformTokenStore | None = None,
                 preferences: LoginPreferences | None = None) -> None:
        self.api_url = api_url.rstrip("/")
        self.store = store or PlatformTokenStore()
        self.preferences = preferences or LoginPreferences(self.store.path, self.api_url)
        self._lock = threading.RLock()
        self._generation = 0

    def status(self) -> dict[str, object]:
        with self._lock:
            token = self.store.token()
            generation = self._generation
        if not token:
            return {"authenticated": False, "user": None}
        try:
            response = httpx.get(
                f"{self.api_url}/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            return {
                "authenticated": False,
                "user": self.store.user(),
                "unavailable": True,
                "detail": str(exc),
            }
        if response.status_code == 401:
            with self._lock:
                if generation == self._generation and token == self.store.token():
                    self.store.clear()
            return {"authenticated": False, "user": None}
        try:
            response.raise_for_status()
            user = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"authenticated": False, "user": self.store.user(),
                    "unavailable": True, "detail": str(exc)}
        return {"authenticated": True, "user": user}

    def login(self, username: str, password: str, *, remember_password: bool = False,
              auto_login: bool = False, use_saved_password: bool = False) -> dict[str, object]:
        username = username.strip()
        with self._lock:
            self._generation += 1
            generation = self._generation
            saved_record = self.preferences.read() if use_saved_password else {}
        if use_saved_password:
            password = self.preferences.password(username)
        if not username or not password:
            raise CredentialError("请输入用户名和密码。")
        response = httpx.post(
            f"{self.api_url}/auth/token",
            json={"username": username, "password": password},
            timeout=10.0,
        )
        if response.status_code in {401, 403} and use_saved_password:
            with self._lock:
                record = self.preferences.read()
                if generation == self._generation and record.get("username") == username:
                    record["auto_login"] = False
                    self.preferences.write(record)
        response.raise_for_status()
        payload = response.json()
        remember_password = remember_password or auto_login
        prepared = {}
        created_secret = False
        try:
            if remember_password:
                if saved_record:
                    # Reauthenticating with an existing secret does not need a
                    # second keyring write/unlock prompt on every App launch.
                    prepared = {**saved_record, "auto_login": auto_login,
                                "password_length": len(password)}
                else:
                    prepared = self.preferences.prepare(username, password, auto_login)
                    created_secret = True
            with self._lock:
                if generation != self._generation:
                    raise CredentialError("登录已取消，请重新登录。")
                previous = self.preferences.raw()
                if prepared:
                    self.preferences.write(prepared)
                else:
                    self.preferences.clear()
                self.store.write(
                    token=payload["access_token"],
                    expires_in=int(payload["expires_in"]),
                    user=payload["user"],
                )
        except Exception:
            with self._lock:
                if created_secret and self.preferences.raw().get("credential_id") == prepared.get("credential_id"):
                    self.preferences.clear()
            if created_secret:
                self.preferences.delete_secret(prepared)
            self._revoke(str(payload.get("access_token", "")))
            raise
        warning = (self.preferences.delete_secret(previous)
                   if previous.get("credential_id") != prepared.get("credential_id") else "")
        return {"authenticated": True, "user": payload["user"], "warning": warning}

    def logout(self) -> dict[str, object]:
        with self._lock:
            self._generation += 1
            token = self.store.token()
            self.store.clear()
            previous = self.preferences.clear()
        warning = self.preferences.delete_secret(previous)
        self._revoke(token)
        return {"ok": True, "warning": warning}

    def _revoke(self, token: str) -> None:
        if token:
            try:
                httpx.post(
                    f"{self.api_url}/auth/logout",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=2.0,
                )
            except httpx.HTTPError:
                # Local sign-out must still finish if the cloud is unreachable.
                pass


def current_platform_username(store: PlatformTokenStore | None = None) -> str:
    user = (store or PlatformTokenStore()).user()
    username = user.get("username") if user else None
    return username if isinstance(username, str) and username else "unknown"


def platform_session_available(store: PlatformTokenStore | None = None) -> bool:
    return bool((store or PlatformTokenStore()).token())


def platform_api_client(*, timeout=None, store: PlatformTokenStore | None = None) -> httpx.Client:
    token_store = store or PlatformTokenStore()
    return httpx.Client(timeout=timeout, auth=PlatformBearerAuth(token_store))
