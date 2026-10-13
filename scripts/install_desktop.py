#!/usr/bin/env python3
"""Register this checkout for the current user without developer-specific paths."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "xiaokai-lab-data-collection"
APP_NAME = "小凯实验室数采平台"


def desktop_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def desktop_argument(value: str) -> str:
    # Exec quoting is applied AFTER desktop-entry string unescaping.
    value = value.replace("%", "%%")
    for character in ("\\", '"', "$", chr(96)):
        value = value.replace(character, "\\" + character)
    return desktop_string('"' + value + '"')


def desktop_entry(launcher: Path) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "Comment=遥操作与 UMI 数据采集",
        f"Exec=/bin/sh {desktop_argument(str(launcher))}",
        f"Icon={APP_ID}",
        "Terminal=false",
        "StartupNotify=true",
        f"StartupWMClass={APP_ID}",
        "Categories=Utility;",
        "",
    ])


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def initialize_env(app_root: Path) -> bool:
    """Copy defaults only for a new installation; preserve cloud/user settings."""
    destination = app_root / ".env"
    if destination.exists():
        return False
    content = (app_root / ".env.example").read_bytes()
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    return True


def install_shortcuts(app_root: Path, *, user_home: Path, data_home: Path) -> dict[str, Path]:
    app_root = app_root.resolve()
    entrypoint = app_root / "scripts/desktop_app.py"
    icon_source = app_root / "public/xiaokai-data-collection.svg"
    if not entrypoint.is_file() or not icon_source.is_file():
        raise FileNotFoundError("请选择完整的 App 目录，必须包含启动器和图标。")
    launcher = user_home / ".local/bin" / APP_ID
    menu_entry = data_home / "applications" / f"{APP_ID}.desktop"
    icon = data_home / "icons/hicolor/scalable/apps" / f"{APP_ID}.svg"
    script = (
        "#!/bin/sh\n"
        "# Generated for this installation. Re-register after moving the App.\n"
        f"exec /bin/bash {shlex.quote(str(app_root / 'start-app.sh'))} \"$@\"\n"
    )
    atomic_write(launcher, script.encode(), 0o755)
    atomic_write(icon, icon_source.read_bytes(), 0o644)
    atomic_write(menu_entry, desktop_entry(launcher).encode(), 0o644)
    return {"launcher": launcher, "menu_entry": menu_entry, "icon": icon}


def main() -> None:
    parser = argparse.ArgumentParser(description="按当前用户和 App 实际目录安装/刷新快捷方式；不启动采集服务。")
    parser.add_argument("--app-root", type=Path, default=ROOT)
    parser.add_argument("--init-env-only", action="store_true")
    args = parser.parse_args()
    if args.init_env_only:
        print("已从模板创建 .env。" if initialize_env(args.app_root) else "保留现有 .env 配置。")
        return
    if os.geteuid() == 0:
        parser.error("请用实际使用 App 的普通用户运行，不要使用 sudo 安装用户快捷方式。")
    user_home = Path.home()
    data_home = Path(os.environ.get("XDG_DATA_HOME") or user_home / ".local/share")
    if not data_home.is_absolute():
        parser.error("XDG_DATA_HOME 必须是绝对路径。")
    paths = install_shortcuts(args.app_root, user_home=user_home, data_home=data_home)
    updater = shutil.which("update-desktop-database")
    if updater:
        subprocess.run([updater, str(paths["menu_entry"].parent)], check=False)
    print(f"快捷方式已指向当前安装目录：{args.app_root.resolve()}")
    print(f"应用菜单：{paths['menu_entry']}")


if __name__ == "__main__":
    main()
