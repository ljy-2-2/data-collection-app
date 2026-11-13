#!/usr/bin/env python3
"""Build a complete amd64 release from current source and a private runtime.

Only immutable packaged dependencies are reused. Application code, launchers,
service definitions and the UMI image come from the new release inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import yaml

from build_deb import APP_ID, ROOT, ignore_private, package_defaults
from build_self_contained_deb import PREFIX, executable, write_private_container_runtime
from generate_runtime_config import read_env
from runtime_platform import native_target, validate_runtime_architecture, verify_elf
from sanitize_package import clear_secrets
from umi_distribution import prepare_umi_distribution


def verify_runtime(runtime: Path) -> int:
    target = native_target()
    if target.deb_arch != "amd64":
        raise ValueError("This reuse workflow requires an amd64 standalone runtime")
    for relative in (
        "python/bin/python3.12", "sysroot/usr/bin/python3.10",
        "sysroot/usr/bin/adb", "sysroot/usr/bin/v4l2-ctl",
        "sysroot/usr/bin/dockerd", "sysroot/usr/bin/runc",
        "sysroot/usr/libexec/docker/cli-plugins/docker-compose",
        "robot/bin/python3", "robot/manifest.json",
    ):
        if not (runtime / relative).is_file():
            raise ValueError(f"Incomplete standalone runtime: {relative}")
    count = 0
    for path in runtime.rglob("*"):
        if path.is_symlink():
            if not path.resolve().is_relative_to(runtime.resolve()):
                raise ValueError(f"Runtime symlink escapes package: {path}")
        elif path.is_file():
            with path.open("rb") as handle:
                elf = handle.read(4) == b"\x7fELF"
            if elf:
                verify_elf(path, target)
                count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-package", type=Path, required=True)
    parser.add_argument("--umi-resources", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform-url")
    parser.add_argument("--api-url")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+~]*", args.version):
        parser.error("Invalid Debian version")
    base = args.runtime_package.resolve(strict=True)
    if not (base / "self-contained.json").is_file():
        parser.error("A complete standalone package is required")
    count = verify_runtime(base / "runtime")
    resources = args.umi_resources.resolve(strict=True)
    manifest = json.loads((resources / "manifest.json").read_text())
    validate_runtime_architecture(manifest, native_target())
    if Path(manifest["archive"]).name != manifest["archive"]:
        parser.error("Invalid image archive name")
    with (resources / manifest["archive"]).open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != manifest["sha256"]:
            parser.error("UMI archive checksum mismatch")
    # Reuse only public service URLs; never copy the operator's .env or state.
    values = read_env(base / "defaults.env")
    if args.platform_url:
        values["PLATFORM_WEB_URL"] = args.platform_url
    if args.api_url:
        values["PLATFORM_API_URL"] = args.api_url
    output = ROOT / "dist-packages"
    output.mkdir(exist_ok=True)
    cache = ROOT / ".build-cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="full-update-", dir=cache) as temporary:
        stage = Path(temporary)
        package = stage / str(PREFIX).lstrip("/")
        app = package / "app"
        app.mkdir(parents=True)
        for name in ("scripts", "teleop_control", "umi_collection", "public",
                     "fusion_camera_stream", "fusion_data_collection"):
            shutil.copytree(ROOT / name, app / name, ignore=ignore_private, symlinks=True)
        for name in ("start-app.sh", "package.json", ".env.example"):
            shutil.copy2(ROOT / name, app / name)
        source_hashes = {
            str(path.relative_to(app)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(app.rglob("*")) if path.is_file()
        }
        (package / "source-sha256.json").write_text(
            json.dumps(source_hashes, ensure_ascii=False, indent=2) + "\n")
        # RK3588 binaries are not executable on an amd64 recipient.
        core = app / "umi_collection/vendor/galaxis/galaxis-core/galaxis-core/galaxis_core"
        shutil.rmtree(core / "native", ignore_errors=True)
        shutil.rmtree(app / "umi_collection/vendor/galaxis/third_party/usb_cam/native/arm64", ignore_errors=True)
        project = app / "umi_collection/vendor/galaxis"
        config = project / "galaxis_bringup/config/galaxis_config.yaml"
        config.write_text(yaml.safe_dump(clear_secrets(yaml.safe_load(config.read_text())),
                                         allow_unicode=True, sort_keys=False))
        prepare_umi_distribution(package, resources)
        compose = project / "docker-compose.yml"
        payload = yaml.safe_load(compose.read_text())
        payload["services"]["config-init"]["network_mode"] = "none"
        compose.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
        app_hashes = {
            str(path.relative_to(app)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(app.rglob("*")) if path.is_file()
        }
        (package / "app-sha256.json").write_text(
            json.dumps(app_hashes, ensure_ascii=False, indent=2) + "\n")
        print(f"Copying verified private runtime ({count} amd64 ELF files)", flush=True)
        shutil.copytree(base / "runtime", package / "runtime", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (package / "scripts").mkdir()
        for name in ("package_launcher.py", "setup_self_contained.py", "setup_umi_system.py"):
            shutil.copy2(ROOT / "scripts" / name, package / "scripts" / name)
        shutil.copy2(ROOT / "scripts/self_contained_env.sh", package / "runtime/env.sh")
        (package / "defaults.env").write_text(package_defaults(values))
        metadata = dict(version=args.version, build_id=args.version, architecture="amd64",
                        qt_version="6.8.3", collection_mode="all",
                        source_commit=subprocess.check_output(
                            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                        source_dirty=bool(subprocess.check_output(
                            ["git", "status", "--porcelain"], cwd=ROOT, text=True)),
                        runtime_base=json.loads((base / "package-build.json").read_text())["build_id"],
                        runtime_elf_count=count, umi_image_id=manifest["image_id"])
        (package / "package-build.json").write_text(json.dumps(metadata, indent=2) + "\n")
        standalone = json.loads((base / "self-contained.json").read_text())
        standalone["version"] = args.version
        (package / "self-contained.json").write_text(json.dumps(standalone, indent=2) + "\n")
        write_private_container_runtime(stage)
        units = stage / "usr/lib/systemd/system"
        (units / "xiaokai-collection-avahi.service").write_text(
            "[Unit]\nDescription=Xiaokai collection mDNS discovery\nAfter=network.target\n"
            "[Service]\nType=simple\n"
            f"Environment=PATH={PREFIX}/runtime/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
            f"ExecStart={PREFIX}/runtime/bin/avahi-daemon --no-chroot --file={PREFIX}/runtime/avahi-daemon.conf\n"
            "Restart=on-failure\n[Install]\nWantedBy=multi-user.target\n")
        executable(stage / "DEBIAN/postinst",
                   '#!/bin/sh\nset -eu\nif [ "$1" = configure ]; then\n'
                   f'. {PREFIX}/runtime/env.sh\n{PREFIX}/runtime/python/bin/python3.12 '
                   f'{PREFIX}/scripts/setup_self_contained.py\nfi\n')
        executable(stage / "usr/bin" / APP_ID,
                   f'#!/bin/sh\n. {PREFIX}/runtime/env.sh\nexec {PREFIX}/runtime/python/bin/python3.12 '
                   f'{PREFIX}/scripts/package_launcher.py "$@"\n')
        rules = stage / "usr/lib/udev/rules.d/70-xiaokai-android.rules"
        rules.parent.mkdir(parents=True)
        shutil.copy2(package / "runtime/sysroot/lib/udev/rules.d/51-android.rules", rules)
        desktop = stage / "usr/share/applications" / f"{APP_ID}.desktop"
        desktop.parent.mkdir(parents=True)
        desktop.write_text(
            "[Desktop Entry]\nType=Application\nName=小凯实验室数采平台\n"
            f"Exec={APP_ID}\nIcon={APP_ID}\nTerminal=false\nStartupNotify=true\n"
            f"StartupWMClass={APP_ID}\nCategories=Utility;\nActions=SoftwareRendering;\n\n"
            "[Desktop Action SoftwareRendering]\nName=软件渲染模式\n"
            f"Exec={APP_ID} --software-rendering\n")
        icon = stage / "usr/share/icons/hicolor/scalable/apps" / f"{APP_ID}.svg"
        icon.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "public/xiaokai-data-collection.svg", icon)
        docs = stage / "usr/share/doc" / APP_ID
        docs.mkdir(parents=True)
        shutil.copy2(ROOT / "public/lucide-LICENSE", docs / "lucide-LICENSE")
        (docs / "README").write_text(
            "Full amd64 release: UMI, Tianji, KUAVO and G2. Ubuntu 22.04/24.04.\n"
            "Dependency licenses remain in the private Python, Qt, robot and ROS runtimes.\n"
            "Device credentials, calibration and recorded data are not included.\n")
        size = sum(p.stat().st_size for p in package.rglob("*") if p.is_file()) // 1024
        (stage / "DEBIAN/control").write_text(
            f"Package: {APP_ID}\nVersion: {args.version}\nSection: utils\nPriority: optional\n"
            f"Architecture: amd64\nMaintainer: Xiaokai Lab <support@localhost>\nInstalled-Size: {size}\n"
            "Recommends: gnome-keyring, libpam-gnome-keyring, dbus-user-session\n"
            "Description: Xiaokai complete robot and UMI collection desktop\n"
            " Private amd64 Python, Qt, robot SDKs, device tools and ROS runtime.\n")
        artifact = output / f"{APP_ID}_{args.version}_amd64.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zzstd", "-z6",
                        "--build", str(stage), str(artifact)], check=True)
    with artifact.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    artifact.with_suffix(".deb.sha256").write_text(f"{digest}  {artifact.name}\n")
    print(artifact, flush=True)


if __name__ == "__main__":
    main()
