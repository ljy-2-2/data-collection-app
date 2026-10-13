#!/usr/bin/python3
"""Installed packages are read-only; each desktop user gets private mutable state."""

from __future__ import annotations

import fcntl
import grp
import json
import os
from pathlib import Path
import shutil
import shlex
import pwd
import subprocess
import sys
import tempfile

APP_ID = "xiaokai-lab-data-collection"


def ensure_collection_permissions(package: Path) -> None:
    if not (package / "umi-runtime/manifest.json").is_file() or not shutil.which("docker"):
        return
    identity = pwd.getpwuid(os.getuid())
    user = identity.pw_name
    standalone = (package / "self-contained.json").is_file()
    names = (("xiaokai-collection", "dialout", "video", "plugdev") if standalone
             else ("docker", "dialout", "video", "plugdev"))
    python = str(package / "runtime/python/bin/python3.12") if standalone else "/usr/bin/python3"
    setup_script = "setup_self_contained.py" if standalone else "setup_umi_system.py"
    groups = [grp.getgrnam(name) for name in names]
    if any(user not in group.gr_mem and group.gr_gid != os.getgid() for group in groups):
        # Usually handled by apt's postinst. For graphical installers with no
        # caller identity (or another desktop account), ask the OS once.
        subprocess.run([
            "pkexec", python, str(package / "scripts" / setup_script),
            "--configure-user",
        ], check=True)
        groups = [grp.getgrnam(name) for name in names]
        if any(user not in group.gr_mem and group.gr_gid != os.getgid() for group in groups):
            raise RuntimeError("采集权限配置未完成，请重新安装 App")
    current = {*os.getgroups(), os.getegid()}
    missing = [group.gr_name for group in groups if group.gr_gid not in current]
    if not missing:
        return
    if os.environ.get("XIAOKAI_GROUP_REFRESHED"):
        raise RuntimeError("采集权限已安装，请注销桌面后重新登录一次再打开 App")
    # sg reads the updated group database, so a newly installed App normally
    # works immediately without logging the desktop user out.
    command = [python, str(package / "scripts/package_launcher.py"), *sys.argv[1:]]
    # Return to the desktop user's primary group after adding device groups.
    # Leaving the final sg group (usually video) as the primary GID hides
    # /proc/<pid>/exe for existing processes with the user's original GID.
    # That prevented the collector from identifying/stopping the old PICO
    # bridge, which kept TCP 63901 occupied on the first run after install.
    primary_group = grp.getgrgid(identity.pw_gid).gr_name
    command = ["/usr/bin/sg", primary_group, "-c", "exec " + shlex.join(command)]
    for group in reversed(missing):
        command = ["/usr/bin/sg", group, "-c", "exec " + shlex.join(command)]
    os.environ["XIAOKAI_GROUP_REFRESHED"] = "1"
    os.execv(command[0], command)


def prepare(package: Path, data: Path, config: Path) -> Path:
    metadata = json.loads((package / "package-build.json").read_text())
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    env_path = config / ".env"
    try:
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as handle:
            handle.write((package / "defaults.env").read_text())
    workspaces = data / "workspaces"
    workspaces.mkdir(exist_ok=True, mode=0o700)
    work = workspaces / metadata["build_id"]
    if not work.exists():
        temporary = Path(tempfile.mkdtemp(prefix=".prepare-", dir=workspaces))
        try:
            shutil.copytree(package / "app", temporary, dirs_exist_ok=True)
            (temporary / ".runtime").symlink_to(
                package / "runtime", target_is_directory=True
            )
            (temporary / ".env").symlink_to(env_path)
            temporary.rename(work)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return work


def main() -> int:
    if os.getuid() == 0:
        print("请以普通桌面用户打开 App，不要使用 sudo。", file=sys.stderr)
        return 1
    package = Path(__file__).resolve().parents[1]
    if "--headless" not in sys.argv:
        try:
            ensure_collection_permissions(package)
        except (OSError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
            # Login and the Qt interface must remain available even if UMI
            # setup is incomplete; its device check reports the missing tools.
            print(f"UMI 采集权限未就绪：{exc}", file=sys.stderr)
    data = (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        / APP_ID
    )
    config = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / APP_ID
    )
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(data / "app.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("App 已在运行，请切换到已有窗口。", file=sys.stderr)
        return 1
    os.set_inheritable(lock, True)
    work = prepare(package, data, config)
    os.chdir(work)
    os.environ["PLATFORM_TOKEN_FILE"] = str(config / "platform-auth.json")
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("PYTHONPATH", None)
    if "--headless" in sys.argv:
        args = [arg for arg in sys.argv[1:] if arg != "--headless"]
        os.execv(
            str(work / "start-app.sh"),
            [str(work / "start-app.sh"), "--backend-only", "--no-browser", *args],
        )
    os.execv(
        str(work / ".runtime/python/bin/python3.12"),
        [str(work / ".runtime/python/bin/python3.12"), str(work / "scripts/desktop_app.py"), *sys.argv[1:]],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
