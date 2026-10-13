#!/usr/bin/env python3
"""Create a portable, sanitized native-build input, not a ready-to-install app."""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

from build_deb import ROOT, ignore_private, package_defaults
from generate_runtime_config import read_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pico-apk", type=Path, required=True)
    parser.add_argument("--xr-deb", type=Path)
    parser.add_argument("--platform-url", help="分发默认平台网页地址，不包含登录凭据")
    parser.add_argument("--api-url", help="分发默认平台 API 地址，不包含登录凭据")
    args = parser.parse_args()
    if not args.pico_apk.is_file():
        parser.error("缺少 PICO APK")
    output = ROOT / "dist-packages/orangepi-build-source.tar.gz"
    output.parent.mkdir(exist_ok=True)
    (ROOT / ".build-cache").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="orangepi-source-", dir=ROOT / ".build-cache") as temporary:
        stage = Path(temporary) / "data-collection-app"
        stage.mkdir()
        for name in ("scripts", "teleop_control", "umi_collection", "public", "tests"):
            shutil.copytree(ROOT / name, stage / name, ignore=ignore_private)
        for name in ("start-app.sh", "package.json", ".env.example"):
            shutil.copy2(ROOT / name, stage / name)
        values = read_env(ROOT / ".env")
        if args.platform_url:
            values["PLATFORM_WEB_URL"] = args.platform_url
        if args.api_url:
            values["PLATFORM_API_URL"] = args.api_url
        (stage / ".env").write_text(package_defaults(values))
        for name in ("__init__.py", "client.py", "stream.py", "episode.py", "grippers.py"):
            destination = stage / "fusion_camera_stream/fusion_camera" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "fusion_camera_stream/fusion_camera" / name, destination)
        for name in ("FusionDataCollector/__init__.py", "FusionDataCollector/collector.py",
                     "FusionDataCollector/camera_map.json", "teleop_patch/marvin_teleop/collection_telemetry.py"):
            destination = stage / "fusion_data_collection" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "fusion_data_collection" / name, destination)
        headset = stage / "umi_collection/headset"
        headset.mkdir(exist_ok=True)
        shutil.copy2(args.pico_apk, headset / "galaxis-vr.apk")
        if args.xr_deb:
            cache = stage / ".build-cache/orangepi-deps"
            cache.mkdir(parents=True)
            shutil.copy2(args.xr_deb, cache / "xr-service-arm64.deb")
        subprocess.run([sys.executable, str(ROOT / "scripts/sanitize_package.py"), str(stage)], check=True)
        with tarfile.open(output, "w:gz", compresslevel=3) as archive:
            archive.add(stage, arcname=stage.name)
    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n")
    print(f"Source/build inputs: {output} ({output.stat().st_size} bytes)\nSHA256 {digest}")


if __name__ == "__main__":
    main()
