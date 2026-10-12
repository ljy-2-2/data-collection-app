#!/usr/bin/env python3
"""Update both platform addresses atomically, preserving other settings."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import tempfile

from generate_runtime_config import ROOT, platform_urls


def configure(path: Path, api_url: str, web_url: str) -> dict[str, str]:
    urls = platform_urls({"PLATFORM_API_URL": api_url, "PLATFORM_WEB_URL": web_url})
    # Installed workspaces link .env to the persistent per-user configuration.
    path = path.expanduser().resolve()
    source = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = []
    written = set()
    for line in source.splitlines():
        match = re.match(r"\s*(PLATFORM_API_URL|PLATFORM_WEB_URL)\s*=", line)
        if not match:
            lines.append(line)
        elif match[1] not in written:
            key = match[1]
            lines.append(f"{key}={urls[key]}")
            written.add(key)
    lines.extend(f"{key}={value}" for key, value in urls.items() if key not in written)
    # A same-directory rename ensures readers see either the old pair or the new pair.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".platform-config-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return urls


def main() -> None:
    parser = argparse.ArgumentParser(description="同时切换平台登录/上传地址和网页跳转地址")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--web-url", required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    try:
        configure(args.env_file, args.api_url, args.web_url)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print("平台 API 和网页地址已同时更新，请重启应用生效。")


if __name__ == "__main__":
    main()
