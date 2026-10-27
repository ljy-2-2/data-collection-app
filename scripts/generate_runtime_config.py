#!/usr/bin/env python3
"""Generate the browser-safe runtime config from the shared .env file."""

from __future__ import annotations

import json
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def platform_urls(values: dict[str, str]) -> dict[str, str]:
    """Resolve the API and web addresses together; never mix cloud and loopback."""
    defaults = {"PLATFORM_API_URL": "http://127.0.0.1:8000",
                "PLATFORM_WEB_URL": "http://127.0.0.1:5173"}
    # A standalone recorder must not depend on obsolete platform URLs.
    if values.get('DATA_COLLECTION_STORAGE_MODE') == 'raw-only':
        return defaults
    result = {}
    local = []
    for key, default in defaults.items():
        value = values.get(key, default).strip().rstrip("/")
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(character.isspace() for character in value)):
            raise ValueError(f"{key} 必须是不含凭据、查询参数的 http(s) 地址")
        _ = parsed.port  # Reject malformed ports before saving configuration.
        host = parsed.hostname.lower()
        try:
            local.append(ip_address(host).is_loopback)
        except ValueError:
            local.append(host == "localhost")
        result[key] = value
    if local[0] != local[1]:
        raise ValueError("平台地址不匹配：切换本地或云端时，请同时设置 PLATFORM_API_URL 和 PLATFORM_WEB_URL")
    return result


def browser_config(values: dict[str, str]) -> dict[str, str]:
    urls = platform_urls(values)
    return {
        "appName": "Robot Data Collection Console",
        "localControlUrl": values.get("LOCAL_CONTROL_URL", "http://127.0.0.1:8010"),
        "platformApiUrl": urls["PLATFORM_API_URL"],
        "platformWebUrl": urls["PLATFORM_WEB_URL"],
    }


def main() -> None:
    payload = json.dumps(browser_config(read_env(ROOT / ".env")), ensure_ascii=False, indent=2)
    output = ROOT / "public" / "runtime-config.js"
    content = f"window.__COLLECTION_APP_CONFIG__ = {payload};\n"
    output.write_text(content, encoding="utf-8")
    built_output = ROOT / "dist" / "client" / "runtime-config.js"
    if built_output.parent.is_dir():
        built_output.write_text(content, encoding="utf-8")
    print(f"generated {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
