#!/usr/bin/env python3
"""Prepare the offline UMI runtime without running devices or collector services."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import re
import subprocess
import tarfile

import yaml

from build_deb import ROOT, ignore_private
from sanitize_package import clear_secrets
from runtime_platform import native_target

TAG = "xiaokai-umi-runtime:20261009.1"
BUILD_TAG = "xiaokai-umi-build:20261009.1"
CACHE = ROOT / ".build-cache/umi-distribution"


def run(*args, **kwargs):
    print("Running:", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def clean_rootfs(source: Path, destination: Path):
    """Flatten the image and omit local state, including deleted older layers."""
    removed = 0
    with tarfile.open(source, "r|") as src, tarfile.open(destination, "w|") as dst:
        for entry in src:
            path = PurePosixPath(entry.name)
            parts = path.parts
            if (any(p in {".git", ".ssh", ".aws", ".env", ".bash_history"} for p in parts)
                or str(path).startswith(("root/.cache/", "root/.config/", "root/.docker/", "opt/data/", "var/log/", "tmp/"))
                or ("ros2_ws" in parts and any(p in {"local", "_runtime", "log"} for p in parts))):
                removed += 1
                continue
            payload = src.extractfile(entry) if entry.isfile() else None
            if entry.isfile() and str(path).startswith(("root/ros2_ws/", "app/monitor/")) and path.suffix in {".json", ".yaml", ".yml"}:
                raw = payload.read()
                try:
                    value = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
                    cleaned = clear_secrets(value)
                    if cleaned != value:
                        raw = (json.dumps(cleaned, ensure_ascii=False) if path.suffix == ".json"
                               else yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False)).encode()
                except (ValueError, yaml.YAMLError, UnicodeError):
                    pass
                entry.size = len(raw)
                payload = io.BytesIO(raw)
            dst.addfile(entry, payload)
    print(f"Removed {removed} private/cache entries from flattened runtime", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", help="Reuse dependencies from a previously verified full UMI image; rebuild all application sources")
    parser.add_argument("--from-source", action="store_true", help="从官方 ROS 基础镜像构建本机架构的全部依赖，无需已有 galaxis 镜像")
    parser.add_argument("--jobs", type=int, default=4, help="源码构建并行度，默认 4")
    args = parser.parse_args()
    if args.from_source and args.base_image:
        parser.error("--from-source 与 --base-image 不能同时使用")
    if not 1 <= args.jobs <= 32:
        parser.error("--jobs 必须在 1～32 之间")
    target = native_target()
    cache = CACHE if target.deb_arch == "amd64" else ROOT / ".build-cache/umi-distribution-arm64"
    tag = TAG if target.deb_arch == "amd64" else TAG + "-arm64"
    build_tag = BUILD_TAG if target.deb_arch == "amd64" else BUILD_TAG + "-arm64"
    if args.base_image and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", args.base_image):
        parser.error("Invalid base image reference")
    cache.mkdir(parents=True, exist_ok=True)
    context = cache / "context"
    if context.exists():
        shutil.rmtree(context)
    project = context / "galaxis"
    shutil.copytree(ROOT / "umi_collection/vendor/galaxis", project, ignore=ignore_private)
    config = project / "galaxis_bringup/config/galaxis_config.yaml"
    config.write_text(yaml.safe_dump(clear_secrets(yaml.safe_load(config.read_text())), allow_unicode=True, sort_keys=False))
    if args.from_source:
        # Build dependencies natively. Never reuse this PC's amd64 ROS layers on ARM.
        core_tag = f"xiaokai-umi-core:source-{target.deb_arch}"
        source_tag = f"xiaokai-umi-devices:source-{target.deb_arch}"
        monitor_tag = f"xiaokai-umi-monitor:source-{target.deb_arch}"
        build_network = []
        if os.environ.get("http_proxy") or os.environ.get("https_proxy"):
            build_network = ["--network=host"]
            for key in ("http_proxy", "https_proxy", "no_proxy"):
                if os.environ.get(key):
                    build_network += ["--build-arg", f"{key}={os.environ[key]}"]
        core_dockerfile = context / "Dockerfile.core"
        core_dockerfile.write_text((project / "galaxis-core/docker/Dockerfile").read_text().replace(
            "colcon build --symlink-install", f"colcon build --parallel-workers {args.jobs} --symlink-install"))
        run("docker", "build", *build_network, "--platform", f"linux/{target.deb_arch}", "-f", core_dockerfile,
            "-t", core_tag, project)
        devices_dockerfile = context / "Dockerfile.devices"
        devices_dockerfile.write_text((project / "docker/Dockerfile").read_text().replace(
            "colcon build --symlink-install", f"colcon build --parallel-workers {args.jobs} --symlink-install"))
        run("docker", "build", *build_network, "--platform", f"linux/{target.deb_arch}", "-f", devices_dockerfile,
            "--build-arg", f"GALAXIS_CORE_IMAGE={core_tag}", "-t", source_tag, project)
        monitor_dockerfile = context / "Dockerfile.monitor"
        monitor_dockerfile.write_text((project / "monitor/docker/Dockerfile").read_text().replace(
            "FROM galaxis-core:latest", f"FROM {core_tag}"))
        run("docker", "build", *build_network, "--platform", f"linux/{target.deb_arch}", "-f", monitor_dockerfile,
            "-t", monitor_tag, project)
    else:
        core_tag, source_tag, monitor_tag = "galaxis-core:latest", "galaxis:latest", "galaxis_monitor:latest"
    for reference in ([args.base_image] if args.base_image else [core_tag, source_tag, monitor_tag]):
        architecture = subprocess.check_output(
            ["docker", "image", "inspect", "--format={{.Architecture}}", reference], text=True).strip()
        if architecture != target.deb_arch:
            parser.error(f"基础镜像 {reference} 架构为 {architecture}，当前目标为 {target.deb_arch}")
    base = f"FROM {args.base_image}\n" if args.base_image else '''FROM galaxis-core:latest AS core
FROM galaxis_monitor:latest AS monitor
FROM galaxis:latest
COPY --from=core /usr/local/lib/python3.12/dist-packages/ /usr/local/lib/python3.12/dist-packages/
COPY --from=monitor /usr/local/lib/python3.12/dist-packages/ /usr/local/lib/python3.12/dist-packages/
COPY --from=monitor /usr/local/bin/ /usr/local/bin/
'''
    if args.from_source:
        base = base.replace("galaxis-core:latest", core_tag).replace("galaxis_monitor:latest", monitor_tag).replace("galaxis:latest", source_tag)
    (context / "Dockerfile").write_text(base + '''RUN rm -rf /root/ros2_ws/src/galaxis-core /root/ros2_ws/src/galaxis_core /root/ros2_ws/src/galaxis_pico /root/ros2_ws/src/galaxis_bringup /root/ros2_ws/src/galaxis_description /app/monitor
COPY galaxis/galaxis-core/galaxis-core/ /root/ros2_ws/src/galaxis-core/
COPY galaxis/galaxis-core/airoa_data_msgs/ /root/ros2_ws/src/airoa_data_msgs/
COPY galaxis/galaxis_pico/ /root/ros2_ws/src/galaxis_pico/
COPY galaxis/galaxis_bringup/ /root/ros2_ws/src/galaxis_bringup/
COPY galaxis/third_party/usb_cam/ /root/ros2_ws/src/usb_cam/
COPY galaxis/galaxis_description/ /root/ros2_ws/src/galaxis_description/
COPY galaxis/monitor/ /app/monitor/
WORKDIR /root/ros2_ws
RUN rm -rf build/galaxis_bringup install/galaxis_bringup build/galaxis_description install/galaxis_description build/usb_cam install/usb_cam && /bin/bash -c 'source /opt/ros/jazzy/setup.bash && colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF --packages-select airoa_data_msgs galaxis_pico_msgs galaxis_core galaxis_pico_bridge galaxis_bringup galaxis_description usb_cam'
RUN python3 -c 'import fastapi,uvicorn,yaml,PIL,h5py,serial,mcap,airoa_metadata,minio'
'''.replace("colcon build --symlink-install", f"colcon build --parallel-workers {args.jobs} --symlink-install"))
    run("docker", "build", "--network=none", "-t", build_tag, context)
    container = subprocess.check_output(["docker", "create", "--network=none", build_tag, "/bin/true"], text=True).strip()
    raw, clean = cache / "rootfs.tar", cache / "clean-rootfs.tar"
    try:
        run("docker", "export", "-o", raw, container)
    finally:
        run("docker", "rm", container)
    clean_rootfs(raw, clean)
    run("docker", "import", "--platform", f"linux/{target.deb_arch}", "--change", "ENV ROS_DISTRO=jazzy LANG=C.UTF-8 LC_ALL=C.UTF-8",
        "--change", 'ENTRYPOINT ["/root/ros_entrypoint.sh"]',
        "--change", 'CMD ["/bin/bash"]', clean, tag)
    archive = cache / "umi-runtime.tar"
    run("docker", "save", "-o", archive, tag)
    identifier = subprocess.check_output(["docker", "image", "inspect", "--format={{.Id}}", tag], text=True).strip()
    digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
    (cache / "manifest.json").write_text(json.dumps({"image": tag, "image_id": identifier, "archive": archive.name,
        "sha256": digest, "architecture": target.deb_arch}, indent=2))
    raw.unlink()
    clean.unlink()
    print(f"Offline UMI runtime ready: {archive}", flush=True)


if __name__ == "__main__":
    main()
