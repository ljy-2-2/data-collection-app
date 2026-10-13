"""Local login preferences; passwords live only in the desktop Secret Service."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


class CredentialError(RuntimeError):
    pass


class DesktopKeyring:
    def call(self, operation: str, scope: str, key: str, password: str = "") -> str:
        try:
            result = subprocess.run(
                [os.environ.get("XIAOKAI_SECRET_PYTHON", "/usr/bin/python3"), "-I", str(Path(__file__).with_name("keyring_helper.py")),
                 operation, scope, key],
                input=json.dumps({"password": password}),
                text=True, capture_output=True, timeout=15, check=False,
            )
            if result.returncode:
                raise CredentialError("系统钥匙环不可用或未解锁，请解锁后重试，或取消记住账号密码。")
            return json.loads(result.stdout).get("password", "")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            # Never include subprocess input/output or the password in errors.
            raise CredentialError("无法访问系统钥匙环，请解锁后重试，或取消记住账号密码。") from None


class LoginPreferences:
    def __init__(self, token_path: Path, api_url: str, keyring=None) -> None:
        self.path = token_path.with_name(token_path.stem + "-preferences.json")
        self.scope = hashlib.sha256(
            f"{token_path.resolve()}\n{api_url.rstrip('/')}".encode()
        ).hexdigest()
        self.keyring = keyring or DesktopKeyring()

    def raw(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def read(self) -> dict:
        value = self.raw()
        if value.get("scope") != self.scope or not value.get("credential_id"):
            return {}
        return value

    def public(self) -> dict:
        value = self.read()
        length = value.get("password_length", 0)
        if value and "password_length" not in value:
            # Older preferences have no length metadata. Only return the length,
            # never the secret, and avoid rewriting a concurrently updated record.
            try:
                length = len(self.password(value["username"]))
            except CredentialError:
                length = 0
        return {
            "username": value.get("username", ""),
            "remember_password": bool(value),
            "auto_login": bool(value.get("auto_login")),
            "has_saved_password": bool(value),
            "password_length": length,
        }

    def password(self, username: str) -> str:
        value = self.read()
        if not value or value.get("username") != username:
            raise CredentialError("没有该账号已保存的密码，请重新输入。")
        password = self.keyring.call("lookup", self.scope, value["credential_id"])
        if not password:
            raise CredentialError("已保存的密码不可用，请重新输入。")
        return password

    def prepare(self, username: str, password: str, auto_login: bool) -> dict:
        record = {"scope": self.scope, "credential_id": uuid.uuid4().hex,
                  "username": username, "auto_login": auto_login,
                  "password_length": len(password)}
        self.keyring.call("store", self.scope, record["credential_id"], password)
        return record

    def write(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".login-preferences-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def clear(self) -> dict:
        record = self.raw()
        self.path.unlink(missing_ok=True)
        return record

    def delete_secret(self, record: dict) -> str:
        if record.get("scope") and record.get("credential_id"):
            try:
                self.keyring.call("clear", record["scope"], record["credential_id"])
            except CredentialError:
                return "已取消记忆和自动登录，但钥匙环中的旧密码未能删除，请在系统“密码和密钥”中清理本 App 条目。"
        return ""
