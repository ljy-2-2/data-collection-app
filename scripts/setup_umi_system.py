#!/usr/bin/python3
"""Debian post-install setup. Never starts collection or changes Docker settings."""
from __future__ import annotations

import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess

PACKAGE = Path(__file__).resolve().parents[1]
GROUPS = ("docker", "dialout", "video", "plugdev")


def installer_user(environ):
    for key in ("PKEXEC_UID", "SUDO_UID"):
        raw = environ.get(key, "")
        if raw.isdecimal() and 1000 <= int(raw) < 65534:
            return pwd.getpwuid(int(raw)).pw_name
    name = environ.get("SUDO_USER", "")
    if name:
        user = pwd.getpwnam(name)
        if 1000 <= user.pw_uid < 65534:
            return user.pw_name
    return None


def setup(package=PACKAGE, *, run=subprocess.run):
    resources = package / "umi-runtime"
    manifest = json.loads((resources / "manifest.json").read_text())
    archive = resources / manifest["archive"]
    if archive.name != manifest["archive"]:
        raise ValueError("Invalid runtime archive name")
    with archive.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != manifest["sha256"]:
        raise RuntimeError("UMI 运行镜像校验失败，请重新下载安装包")
    run(["systemctl", "start", "docker"], check=True)
    result = run(["docker", "image", "inspect", "--format={{.Id}}", manifest["image"]], capture_output=True, text=True)
    if result.returncode or result.stdout.strip() != manifest["image_id"]:
        print("正在安装 UMI 采集运行环境，请稍候…", flush=True)
        run(["docker", "load", "--input", str(archive)], check=True)
    result = run(["docker", "image", "inspect", "--format={{.Id}}", manifest["image"]], capture_output=True, text=True, check=True)
    if result.stdout.strip() != manifest["image_id"]:
        raise RuntimeError("UMI 镜像版本验证失败")
    run(["docker", "compose", "version"], check=True)
    user = installer_user(os.environ)
    if user:
        groups = [name for name in GROUPS if _group_exists(name)]
        run(["usermod", "-a", "-G", ",".join(groups), user], check=True)
        print(f"已为安装用户 {user} 配置采集权限。", flush=True)
    else:
        print("未识别安装用户；其他桌面用户首次打开 App 时会请求管理员配置采集权限。", flush=True)
    print("UMI 运行环境安装完成；未启动任何采集服务。", flush=True)


def _group_exists(name):
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def configure_user():
    user = installer_user(os.environ)
    if not user:
        raise RuntimeError("无法识别需要配置的桌面用户")
    groups = [name for name in GROUPS if _group_exists(name)]
    subprocess.run(["usermod", "-a", "-G", ",".join(groups), user], check=True)


if __name__ == "__main__":
    import sys
    if os.geteuid() != 0:
        raise SystemExit("安装配置需要管理员权限")
    if sys.argv[1:] == ["--configure-user"]:
        configure_user()
    elif not sys.argv[1:]:
        setup()
    else:
        raise SystemExit("Invalid setup arguments")
