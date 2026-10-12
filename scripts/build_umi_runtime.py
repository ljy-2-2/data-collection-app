#!/usr/bin/env python3
"""Prepare the offline UMI runtime without running devices or collector services."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import re
import subprocess
import tarfile

import yaml

from build_deb import ROOT, ignore_private
from sanitize_package import clear_secrets

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
    args = parser.parse_args()
    if args.base_image and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", args.base_image):
        parser.error("Invalid base image reference")
    CACHE.mkdir(parents=True, exist_ok=True)
    context = CACHE / "context"
    if context.exists():
        shutil.rmtree(context)
    project = context / "galaxis"
    shutil.copytree(ROOT / "umi_collection/vendor/galaxis", project, ignore=ignore_private)
    config = project / "galaxis_bringup/config/galaxis_config.yaml"
    config.write_text(yaml.safe_dump(clear_secrets(yaml.safe_load(config.read_text())), allow_unicode=True, sort_keys=False))
    base = f"FROM {args.base_image}\n" if args.base_image else '''FROM galaxis-core:latest AS core
FROM galaxis_monitor:latest AS monitor
FROM galaxis:latest
COPY --from=core /usr/local/lib/python3.12/dist-packages/ /usr/local/lib/python3.12/dist-packages/
COPY --from=monitor /usr/local/lib/python3.12/dist-packages/ /usr/local/lib/python3.12/dist-packages/
COPY --from=monitor /usr/local/bin/ /usr/local/bin/
'''
    (context / "Dockerfile").write_text(base + '''RUN rm -rf /root/ros2_ws/src/galaxis-core /root/ros2_ws/src/galaxis_core /root/ros2_ws/src/galaxis_pico /root/ros2_ws/src/galaxis_bringup /root/ros2_ws/src/galaxis_description /app/monitor
COPY galaxis/galaxis-core/galaxis-core/ /root/ros2_ws/src/galaxis-core/
COPY galaxis/galaxis-core/airoa_data_msgs/ /root/ros2_ws/src/airoa_data_msgs/
COPY galaxis/galaxis_pico/ /root/ros2_ws/src/galaxis_pico/
COPY galaxis/galaxis_bringup/ /root/ros2_ws/src/galaxis_bringup/
COPY galaxis/galaxis_description/ /root/ros2_ws/src/galaxis_description/
COPY galaxis/monitor/ /app/monitor/
WORKDIR /root/ros2_ws
RUN rm -rf build/galaxis_bringup install/galaxis_bringup build/galaxis_description install/galaxis_description && /bin/bash -c 'source /opt/ros/jazzy/setup.bash && colcon build --symlink-install --packages-select airoa_data_msgs galaxis_pico_msgs galaxis_core galaxis_pico_bridge galaxis_bringup galaxis_description'
RUN python3 -c 'import fastapi,uvicorn,yaml,PIL,h5py,serial,mcap,airoa_metadata,minio'
''')
    run("docker", "build", "--network=none", "-t", BUILD_TAG, context)
    container = subprocess.check_output(["docker", "create", "--network=none", BUILD_TAG, "/bin/true"], text=True).strip()
    raw, clean = CACHE / "rootfs.tar", CACHE / "clean-rootfs.tar"
    try:
        run("docker", "export", "-o", raw, container)
    finally:
        run("docker", "rm", container)
    clean_rootfs(raw, clean)
    run("docker", "import", "--change", "ENV ROS_DISTRO=jazzy LANG=C.UTF-8 LC_ALL=C.UTF-8",
        "--change", 'ENTRYPOINT ["/root/ros_entrypoint.sh"]',
        "--change", 'CMD ["/bin/bash"]', clean, TAG)
    archive = CACHE / "umi-runtime.tar"
    run("docker", "save", "-o", archive, TAG)
    identifier = subprocess.check_output(["docker", "image", "inspect", "--format={{.Id}}", TAG], text=True).strip()
    digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
    (CACHE / "manifest.json").write_text(json.dumps({"image": TAG, "image_id": identifier, "archive": archive.name, "sha256": digest}, indent=2))
    raw.unlink()
    clean.unlink()
    print(f"Offline UMI runtime ready: {archive}", flush=True)


if __name__ == "__main__":
    main()
