#!/usr/bin/env python3
"""Build a native Ubuntu 22.04+ desktop package without local secrets."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import tomllib

from generate_runtime_config import platform_urls, read_env
from runtime_platform import native_target, verify_elf, validate_runtime_architecture, validate_tianji_libraries

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "xiaokai-lab-data-collection"
EXCLUDED = {
    ".git",
    ".env",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "runtime",
    "_runtime",
    "local",
    "logs",
    "tests",
    ".DS_Store",
    "python3.10.INFO",
}


def ignore_private(directory, names):
    return [
        name
        for name in names
        if name in EXCLUDED
        or name.endswith((".pyc", ".log", ".pem", ".key"))
        or name.startswith((".env.", "recording-status", "platform-auth"))
        and name != ".env.example"
    ]


def package_defaults(values: dict[str, str]) -> str:
    defaults = read_env(ROOT / ".env.example")
    # Hardware settings and absolute paths belong to each recipient's computer.
    defaults.pop("TELEOP_ROBOT_INTERFACE", None)
    defaults.update(platform_urls(values))
    return "# 用户配置；升级不会覆盖。不要在分发包内保存账号密码。\n" + "".join(
        f"{key}={value}\n" for key, value in defaults.items()
    )


def relocate_python_links(source: Path, destination: Path) -> None:
    """uv may create absolute aliases into its cache; ship relative links only."""
    source, destination = source.resolve(), destination.resolve()
    for link in destination.rglob("*"):
        if not link.is_symlink():
            continue
        target = Path(os.readlink(link))
        if target.is_absolute():
            if not target.is_relative_to(source):
                raise ValueError(f"Python runtime link points outside the bundled runtime: {link}")
            relative = os.path.relpath(destination / target.relative_to(source), link.parent)
            link.unlink()
            link.symlink_to(relative)
    for link in destination.rglob("*"):
        if link.is_symlink() and not link.resolve().is_relative_to(destination):
            raise ValueError(f"Python runtime link escapes the bundled runtime: {link}")


def run(*args, **kwargs):
    print("执行：", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description="一键构建小凯实验室数采平台 .deb")
    parser.add_argument(
        "--version", help="Debian 版本号；默认代码版本+构建时间，便于升级"
    )
    parser.add_argument(
        "--platform-url", help="覆盖打包的平台网址（默认读取本机 .env）"
    )
    parser.add_argument("--api-url", help="覆盖打包的 API 地址（默认读取本机 .env）")
    parser.add_argument("--desktop-only", action="store_true", help="开发用途：不包含完整 UMI 运行环境")
    parser.add_argument("--pico-apk", type=Path, help="随完整安装包提供的 Galaxis PICO APK")
    parser.add_argument("--umi-resources", type=Path, help="已构建的同架构 UMI 镜像及 manifest 目录")
    parser.add_argument("--tianji-native", type=Path, help="本机架构的天机原生 SDK 构建目录")
    parser.add_argument(
        "--skip-build", action="store_true", help="兼容旧命令；Qt 界面无需前端构建"
    )
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("请在 Linux 上构建")
    try:
        target = native_target()
    except ValueError as exc:
        parser.error(str(exc))
    tianji_libraries = []
    if target.deb_arch == "arm64":
        try:
            tianji_libraries = validate_tianji_libraries(
                args.tianji_native or ROOT / ".build-cache/tianji-native-arm64", target)
        except (OSError, ValueError) as exc:
            parser.error(f"请先构建 ARM64 天机 SDK：python3 scripts/build_tianji_native.py；{exc}")
    for command in ("dpkg-deb", "uv"):
        if not shutil.which(command):
            parser.error(f"构建电脑缺少 {command}；接收方不需要安装此构建工具")
    version = args.version or (
        json.loads((ROOT / "package.json").read_text())["version"]
        + "+"
        + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    )
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+~]*", version):
        parser.error("版本只支持数字开头的字母、数字、点、+、~")
    values = read_env(ROOT / ".env")
    if args.platform_url:
        values["PLATFORM_WEB_URL"] = args.platform_url
    if args.api_url:
        values["PLATFORM_API_URL"] = args.api_url
    defaults = package_defaults(values)
    umi_resources = args.umi_resources or ROOT / ".build-cache" / (
        "umi-distribution" if target.deb_arch == "amd64" else "umi-distribution-arm64")
    if not args.desktop_only and not (umi_resources / "manifest.json").is_file():
        parser.error("请先运行 python3 scripts/build_umi_runtime.py 准备完整 UMI 运行环境")
    if not args.desktop_only and (not args.pico_apk or not args.pico_apk.is_file()):
        parser.error("完整安装包需要 --pico-apk 指定 Galaxis PICO 安装文件")
    if not args.desktop_only:
        try:
            validate_runtime_architecture(json.loads((umi_resources / "manifest.json").read_text()), target)
        except ValueError as exc:
            parser.error(str(exc))
    cache = ROOT / ".build-cache"
    cache.mkdir(exist_ok=True)
    run(
        "uv",
        "python",
        "install",
        "3.12",
        "--no-bin",
        "--install-dir",
        cache / "python",
        "--cache-dir",
        cache / "uv",
    )
    runtimes = sorted((cache / "python").glob(f"cpython-3.12.*-linux-{target.python_machine}-gnu"))
    if not runtimes:
        parser.error(f"未找到 {target.deb_arch} Python 3.12 运行环境")
    python_root = runtimes[-1]
    python_bin = python_root / "bin/python3.12"
    verify_elf(python_bin, target)
    requirements = tomllib.loads((ROOT / "teleop_control/pyproject.toml").read_text())[
        "project"
    ]["dependencies"]
    # This interpreter is private to .build-cache, never the operating system's Python.
    run(
        "uv",
        "pip",
        "install",
        "--python",
        python_bin,
        "--break-system-packages",
        "--cache-dir",
        cache / "uv",
        *requirements,
        "h5py>=3.11",
        "psutil>=6",
        "requests>=2.32",
        f"PySide6=={target.qt_version}",
    )
    run(python_bin, '-c', 'import PySide6, PySide6.QtQuick, PySide6.QtQuick3D; print("Qt runtime ready:", PySide6.__version__)')
    if target.deb_arch == "arm64":
        run(python_bin, '-c', 'import xrobotoolkit_sdk; print("ARM64 XR binding ready")')
        for library in (python_root / "lib/python3.12/site-packages").glob("xrobotoolkit_sdk*.so"):
            verify_elf(library, target)
    out = ROOT / "dist-packages"
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deb-stage-", dir=cache) as stage_name:
        stage = Path(stage_name)
        package = stage / "opt" / APP_ID
        app = package / "app"
        app.mkdir(parents=True)
        for name in ("scripts", "teleop_control", "umi_collection", "public"):
            shutil.copytree(ROOT / name, app / name, ignore=ignore_private)
        if target.deb_arch == "arm64":
            for library in tianji_libraries:
                shutil.copy2(library, app / "teleop_control/vendor/tianji/SDK_PYTHON" / library.name)
        # Reuse only the camera client's runtime modules; omit controller tools and sample data.
        camera_package = app / "fusion_camera_stream/fusion_camera"
        camera_package.mkdir(parents=True)
        for name in ("__init__.py", "client.py", "stream.py", "episode.py", "grippers.py"):
            shutil.copy2(ROOT / "fusion_camera_stream/fusion_camera" / name, camera_package / name)
        for relative in ("FusionDataCollector/__init__.py", "FusionDataCollector/collector.py",
                         "FusionDataCollector/camera_map.json", "teleop_patch/marvin_teleop/collection_telemetry.py"):
            destination = app / "fusion_data_collection" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "fusion_data_collection" / relative, destination)
        for name in ("start-app.sh", "package.json", ".env.example"):
            shutil.copy2(ROOT / name, app / name)
        run(python_bin, ROOT / "scripts/sanitize_package.py", app)
        if not args.desktop_only:
            from umi_distribution import prepare_umi_distribution
            prepare_umi_distribution(package, umi_resources)
            headset = app / "umi_collection/headset"
            headset.mkdir(exist_ok=True)
            shutil.copy2(args.pico_apk, headset / "galaxis-vr.apk")
        (app / "start-app.sh").chmod(0o755)
        (package / "defaults.env").write_text(defaults)
        runtime = package / "runtime"
        runtime.mkdir()
        if target.deb_arch == "arm64":
            graphics = runtime / "graphics"
            graphics.mkdir()
            library = graphics / "libxiaokai-mali-x11.so"
            run("cc", "-O2", "-shared", "-fPIC", ROOT / "scripts/native/mali_x11_compat.c", "-ldl", "-o", library)
            verify_elf(library, target)
        shutil.copytree(
            python_root,
            runtime / "python",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        relocate_python_links(python_root, runtime / "python")
        if target.deb_arch == "arm64":
            # The official ARM XR service omits ICU 73. Reuse only the matching
            # ICU libraries in the Qt wheel, not its entire Qt/plugin search path.
            support = runtime / "xr-support"
            support.mkdir()
            for name in ("libicuuc.so.73", "libicui18n.so.73", "libicudata.so.73"):
                library = runtime / "python/lib/python3.12/site-packages/PySide6/Qt/lib" / name
                verify_elf(library, target)
                (support / name).symlink_to(os.path.relpath(library, support))
        # No browser engine is used. Omit the unrelated WebEngine add-on and
        # development tools, retaining the Qt/PySide license files.
        qt_site = runtime / "python/lib/python3.12/site-packages/PySide6"
        for path in sorted(qt_site.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if any(marker in path.name.lower() for marker in ("webengine", "webview")):
                if path.is_dir(): shutil.rmtree(path)
                else: path.unlink()
        for name in ("qml", "qmllint", "qmlls", "qmlformat", "assistant", "designer", "linguist"):
            path = qt_site / name
            if path.is_file(): path.unlink()

        launchers = package / "scripts"
        launchers.mkdir()
        shutil.copy2(
            ROOT / "scripts/package_launcher.py", launchers / "package_launcher.py"
        )
        if not args.desktop_only:
            shutil.copy2(ROOT / "scripts/setup_umi_system.py", launchers / "setup_umi_system.py")
        build_id = version + "-" + datetime.now(timezone.utc).strftime("%f")
        (package / "package-build.json").write_text(
            json.dumps({"version": version, "build_id": build_id,
                        "architecture": target.deb_arch, "qt_version": target.qt_version,
                        "collection_mode": "umi-tianji" if target.deb_arch == "arm64" else "all"}, indent=2)
        )
        binary = stage / "usr/bin" / APP_ID
        binary.parent.mkdir(parents=True)
        binary.write_text(
            f'#!/bin/sh\nexec /usr/bin/python3 /opt/{APP_ID}/scripts/package_launcher.py "$@"\n'
        )
        binary.chmod(0o755)
        desktop = stage / "usr/share/applications" / f"{APP_ID}.desktop"
        desktop.parent.mkdir(parents=True)
        desktop.write_text(
            f"[Desktop Entry]\nType=Application\nName=小凯实验室数采平台\nComment=遥操作与 UMI 数据采集\nExec={APP_ID}\nIcon={APP_ID}\nTerminal=false\nStartupNotify=true\nStartupWMClass={APP_ID}\nCategories=Utility;\nActions=SoftwareRendering;\n\n[Desktop Action SoftwareRendering]\nName=软件渲染模式\nExec={APP_ID} --software-rendering\n"
        )
        icon = stage / "usr/share/icons/hicolor/scalable/apps" / f"{APP_ID}.svg"
        icon.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "public/xiaokai-data-collection.svg", icon)
        licenses = stage / "usr/share/doc" / APP_ID
        licenses.mkdir(parents=True)
        (licenses / "README").write_text(
            "Bundled Python, PySide6, Qt and other dependencies retain their licenses in /opt/"
            + APP_ID + "/runtime/python. Original lucide icon license is included here.\n"
            "UMI images retain upstream licenses and source in their filesystem. Robot-specific SDK setup is separate.\n"
        )
        license_source = ROOT / "public/lucide-LICENSE"
        if not license_source.is_file():
            license_source = ROOT / "node_modules/lucide-react/LICENSE"
        shutil.copy2(license_source, licenses / "lucide-LICENSE")
        control = stage / "DEBIAN/control"
        control.parent.mkdir()
        umi_dependencies = ", roboticsservice" if target.deb_arch == "arm64" else ""
        if not args.desktop_only:
            umi_dependencies += ", docker.io | docker-ce, docker-compose-v2 | docker-compose-plugin, adb, android-sdk-platform-tools-common, curl, usbutils, v4l-utils, avahi-daemon, udev, policykit-1, zenity, passwd, fonts-noto-cjk"
            postinst = control.parent / "postinst"
            postinst.write_text(
                '#!/bin/sh\nset -eu\nif [ "$1" = configure ]; then\n'
                f'  /usr/bin/python3 /opt/{APP_ID}/scripts/setup_umi_system.py\nfi\n'
            )
            postinst.chmod(0o755)
        installed_kb = (
            sum(path.stat().st_size for path in package.rglob("*") if path.is_file())
            // 1024
        )
        control.write_text(
            f"Package: {APP_ID}\nVersion: {version}\nSection: utils\nPriority: optional\nArchitecture: {target.deb_arch}\nMaintainer: Xiaokai Lab <support@localhost>\nInstalled-Size: {installed_kb}\nDepends: python3 (>= 3.10), python3-gi, gir1.2-secret-1, gnome-keyring, libpam-gnome-keyring, dbus-user-session, libegl1, libgl1, libgl1-mesa-dri, libwayland-client0, libwayland-cursor0, libwayland-egl1, libxcb-shm0, libxcb-util1, libzstd1, libpng16-16, zlib1g, libopengl0, libxkbcommon0, libxkbcommon-x11-0, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-render-util0, libxcb-xinerama0, libxcb-randr0, libxcb-shape0, libxcb-xfixes0, libxcb-sync1, libxcb-xkb1, libxcb-render0, libxcb1, libx11-6, libx11-xcb1, libfontconfig1, libfreetype6, libdbus-1-3, libsm6, libice6, libstdc++6, libgcc-s1, libc6 (>= 2.35), libglib2.0-0, libgomp1, ca-certificates, xdg-utils{umi_dependencies}\nRecommends: fonts-noto-cjk\nDescription: Xiaokai Lab robot data collection desktop app\n Bundled Python, PySide6/Qt Quick native UI and offline UMI runtime.\n"
        )
        artifact = out / f"{APP_ID}_{version}_{target.deb_arch}.deb"
        run(
            "dpkg-deb",
            "--root-owner-group",
            "-Zzstd",
            "-z6",
            "--build",
            stage,
            artifact,
        )
    digest = hashlib.file_digest(artifact.open("rb"), "sha256").hexdigest()
    artifact.with_suffix(".deb.sha256").write_text(f"{digest}  {artifact.name}\n")
    print(
        f"\n生成成功：{artifact}\n安装或升级：sudo apt install ./{artifact.name}\n仅打包平台地址，未打包个人密码、登录令牌或采集数据。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
