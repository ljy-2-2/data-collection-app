#!/usr/bin/env python3
"""Configure only this application's private runtime; never invoke APT/dpkg."""
from __future__ import annotations
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[1]
GROUPS = ("xiaokai-collection", "dialout", "video", "plugdev")
SERVICES = ("xiaokai-collection-docker.service", "xiaokai-collection-avahi.service")


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def installer_user():
    for key in ("PKEXEC_UID", "SUDO_UID"):
        value = os.environ.get(key, "")
        if value.isdecimal() and 1000 <= int(value) < 65534:
            return pwd.getpwuid(int(value)).pw_name
    value = os.environ.get("SUDO_USER")
    if value and 1000 <= pwd.getpwnam(value).pw_uid < 65534:
        return value
    return None


def configure_user():
    for name in GROUPS:
        try:
            grp.getgrnam(name)
        except KeyError:
            run("/usr/sbin/groupadd", "--system", name)
    user = installer_user()
    if user:
        run("/usr/sbin/usermod", "-a", "-G", ",".join(GROUPS), user)
        print(f"已配置 {user} 的采集设备权限。", flush=True)


def verified_manifest():
    folder = PACKAGE / "umi-runtime"
    manifest = json.loads((folder / "manifest.json").read_text())
    assert Path(manifest["archive"]).name == manifest["archive"]
    with (folder / manifest["archive"]).open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != manifest["sha256"]:
            raise RuntimeError("采集镜像校验失败，请重新复制安装包")
    return manifest


def load_image():
    manifest = verified_manifest()
    docker = PACKAGE / "runtime/bin/docker"
    ready = Path("/var/lib/xiaokai-lab-data-collection/image-ready")
    imported = ready.exists() and ready.read_text().strip() == manifest["image_id"]
    result = subprocess.run([str(docker), "image", "inspect", "--format={{.Id}}", manifest["image"]], capture_output=True, text=True)
    if not imported or result.returncode or result.stdout.strip() != manifest["image_id"]:
        print("正在导入独立采集运行环境…", flush=True)
        loaded = run(docker, "load", "--input", PACKAGE / "umi-runtime" / manifest["archive"], capture_output=True, text=True)
        output = loaded.stdout + loaded.stderr
        print(output, end="", flush=True)
        # Docker 29 may return zero even when containerd failed to unpack.
        # Never cache an image as ready after that partial success.
        if "Error unpacking image" in output:
            raise RuntimeError("采集镜像解包失败，请检查安装环境的容器挂载权限")
    result = run(docker, "image", "inspect", "--format={{.Id}}", manifest["image"], capture_output=True, text=True)
    if result.stdout.strip() != manifest["image_id"]:
        raise RuntimeError("采集镜像版本不匹配")
    ready.parent.mkdir(parents=True, exist_ok=True)
    temporary = ready.with_suffix(".tmp")
    temporary.write_text(manifest["image_id"] + "\n")
    temporary.replace(ready)


def setup():
    verified_manifest()
    configure_user()
    try:
        pwd.getpwnam("avahi")
    except KeyError:
        try:
            grp.getgrnam("avahi")
            group_option = ["--gid", "avahi"]
        except KeyError:
            group_option = ["--user-group"]
        run("/usr/sbin/useradd", "--system", *group_option, "--no-create-home",
            "--home-dir", "/run/avahi-daemon", "--shell", "/usr/sbin/nologin", "avahi")
    wants = Path("/etc/systemd/system/multi-user.target.wants")
    wants.mkdir(parents=True, exist_ok=True)
    selected = [SERVICES[0]]
    # Reuse an existing host Avahi instance when present.
    if not Path("/run/avahi-daemon/socket").exists():
        selected.append(SERVICES[1])
    for service in selected:
        link = wants / service
        if not link.exists() and not link.is_symlink():
            link.symlink_to("/usr/lib/systemd/system/" + service)
    run(PACKAGE / "runtime/bin/docker", "compose", "version")
    run(PACKAGE / "runtime/bin/adb", "version")
    if Path("/run/systemd/system").is_dir():
        run("/usr/bin/systemctl", "daemon-reload")
        for service in reversed(selected):
            run("/usr/bin/systemctl", "restart", service)
        subprocess.run([str(PACKAGE / "runtime/bin/udevadm"), "control", "--reload-rules"], check=False)
    else:
        print("当前不是 systemd 桌面环境；采集服务将在正常系统启动时自动初始化。", flush=True)
    print("独立运行组件配置完成，无需 apt 或其他依赖安装命令。", flush=True)


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("安装配置需要管理员权限")
    if sys.argv[1:] == ["--load-image"]:
        load_image()
    elif sys.argv[1:] == ["--configure-user"]:
        configure_user()
    elif not sys.argv[1:]:
        setup()
    else:
        raise SystemExit("Invalid setup arguments")
