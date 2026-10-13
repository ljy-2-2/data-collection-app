#!/usr/bin/env python3
"""Build the official XR binding into the app's private Python; no service is started."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

from runtime_platform import native_target, verify_elf

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "teleop_control/vendor/xr_pybind"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--service-root", type=Path, default=Path("/opt/apps/roboticsservice"))
    args = parser.parse_args()
    target = native_target()
    if target.deb_arch != "arm64":
        parser.error("此入口用于 ARM64；不会替换现有电脑的 XR 环境")
    verify_elf(args.python, target)
    library = args.service_root / "SDK/arm64/libPXREARobotSDK.so"
    verify_elf(library, target)
    verify_elf(args.service_root / "RoboticsServiceProcess", target)
    for name, expected in json.loads((SOURCE / "SOURCE.json").read_text())["sha256"].items():
        if hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"XR 绑定源码校验失败：{name}")
    subprocess.run(["uv", "pip", "install", "--python", str(args.python),
                    "--break-system-packages", "pybind11==2.13.6"], check=True)
    query = "import sysconfig,json; print(json.dumps([sysconfig.get_path('platlib'),sysconfig.get_config_var('EXT_SUFFIX')]))"
    site, suffix = json.loads(subprocess.check_output([str(args.python), "-c", query], text=True))
    site = Path(site)
    includes = shlex.split(subprocess.check_output([str(args.python), "-m", "pybind11", "--includes"], text=True))
    output = site / ("xrobotoolkit_sdk" + suffix)
    # The binding and its sole SDK dependency are adjacent, so relocation works.
    shutil.copy2(library, site / library.name)
    subprocess.run(["patchelf", "--remove-rpath", str(site / library.name)], check=True)
    subprocess.run(["g++", "-O2", "-shared", "-std=c++17", "-fPIC", *includes,
                    "-I" + str(SOURCE / "include"), str(SOURCE / "bindings/py_bindings.cpp"),
                    "-L" + str(site), "-lPXREARobotSDK", "-Wl,-rpath,$ORIGIN", "-pthread",
                    "-o", str(output)], check=True)
    verify_elf(output, target)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    subprocess.run([str(args.python), "-c", "import xrobotoolkit_sdk; print('XR binding import passed; hardware not started')"],
                   check=True, env=env)
    shutil.copy2(SOURCE / "LICENSE", site / "xrobotoolkit_sdk-LICENSE")


if __name__ == "__main__":
    main()
