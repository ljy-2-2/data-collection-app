"""Per-user camera authorization, independent of versioned App workspaces."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import ssl
import tempfile

APP_ID = "xiaokai-lab-data-collection"


def credentials_path() -> Path:
    override = os.environ.get("TIANJI_CAMERA_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return config / APP_ID / "tianji-camera" / "client.json"


def _atomic_private(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".camera-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def import_credentials(source: Path) -> Path:
    source = Path(source).expanduser()
    try:
        if source.stat().st_size > 65536:
            raise ValueError("相机配置文件过大")
        settings = json.loads(source.read_text())
        if (not isinstance(settings, dict) or not isinstance(settings.get("token"), str)
                or not settings["token"].strip() or not isinstance(settings.get("certificate"), str)
                or not settings["certificate"].strip()):
            raise ValueError("相机配置需包含授权令牌和证书路径")
        port = settings.get("port", 57491)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("相机配置端口无效")
        certificate = source.parent / settings["certificate"]
        if certificate.stat().st_size > 1024 * 1024:
            raise ValueError("相机证书过大")
        pem = certificate.read_bytes()
        if b"PRIVATE KEY" in pem:
            raise ValueError("请选择相机服务证书，不要使用私钥文件")
        ssl.create_default_context(cadata=pem.decode("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError, ssl.SSLError) as exc:
        raise ValueError("相机配置或证书无法读取，请选择有效的 client.json，并保留其对应证书") from exc
    destination = credentials_path()
    certificate_name = "server-" + hashlib.sha256(pem).hexdigest()[:24] + ".pem"
    # Copy the public certificate first, then atomically switch the credential
    # file. Never persist an absolute path to the source computer or SDK folder.
    _atomic_private(destination.parent / certificate_name, pem)
    _atomic_private(destination, json.dumps(
        dict(token=settings["token"], port=port, certificate=certificate_name),
        ensure_ascii=False, indent=2).encode())
    return destination


def resolve_credentials(runtime: Path) -> Path:
    destination = credentials_path()
    if destination.exists() or os.environ.get("TIANJI_CAMERA_CREDENTIALS"):
        return destination
    # Migrate only this user's old App workspaces. Credentials never belong in
    # the distributable package and are never inferred from another user's SDK.
    legacy = Path(runtime) / "camera-client.json"
    if legacy.is_file():
        return import_credentials(legacy)
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    candidates = list((data / APP_ID / "workspaces").glob(
        "*/teleop_control/runtime/tianji/camera-client.json"))
    if candidates:
        return import_credentials(max(candidates, key=lambda path: path.stat().st_mtime_ns))
    return destination
