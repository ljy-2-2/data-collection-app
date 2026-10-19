#!/usr/bin/env python3
"""Build Tianji libraries for this CPU; never connect to robot hardware."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import importlib.util
from pathlib import Path
import subprocess

from runtime_platform import native_target, verify_elf

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "teleop_control/vendor/tianji/native"


def verify_abi(destination: Path) -> None:
    spec = importlib.util.spec_from_file_location("tianji_abi", ROOT / "teleop_control/vendor/tianji/SDK_PYTHON/fx_robot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lines = ['#include <cstdio>', '#include <cstddef>', '#include "FxRtCSDef.h"', 'int main(){']
    expected = []
    for name in ("StateCtr", "RT_IN", "RT_OUT", "DCSS"):
        cls = getattr(module, name)
        expected.append(ctypes.sizeof(cls))
        lines.append(f'printf("%zu\\n",sizeof({name}));')
        for field, *_ in cls._fields_:
            expected.append(getattr(cls, field).offset)
            lines.append(f'printf("%zu\\n",offsetof({name},{field}));')
    source = destination / "abi.cpp"
    source.write_text("\n".join(lines + ['}']))
    binary = destination / "abi"
    subprocess.run(["g++", "-I" + str(SOURCE / "contrlSDK100343"), str(source), "-o", str(binary)], check=True)
    actual = list(map(int, subprocess.check_output([str(binary.resolve())], text=True).split()))
    if actual != expected:
        raise ValueError("天机 C++/Python 结构大小或字段偏移不一致，不能用于采集")


def build(destination: Path) -> None:
    target = native_target()
    destination.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((SOURCE / "SOURCE.json").read_text())
    for name, expected in manifest["sha256"].items():
        if hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"SDK 源码已变更，需审查并更新源码记录：{name}")
    verify_abi(destination)
    libraries = {}
    for directory, name, defines, symbols in (
        ("contrlSDK100343", "libMarvinSDK.so", ["-DCMPL_LIN"], ["OnGetBuf", "CheckSDKTypeCompat"]),
        ("kinematicsSDK", "libKine.so", [], ["FX_Robot_Kine_FK", "FX_Robot_Kine_IK"]),
    ):
        output = destination / name
        # Sequential compilation avoids multiplying peak RAM by all eight RK3588 cores.
        command = ["g++", *map(str, sorted((SOURCE / directory).glob("*.cpp"))),
                   "-O2", "-fPIC", "-shared", "-o", str(output), "-lpthread", "-lrt", *defines]
        subprocess.run(command, check=True)
        verify_elf(output, target)
        # Loading and resolving symbols only: no SDK initialization or network calls.
        library = ctypes.CDLL(str(output.resolve()))
        for symbol in symbols:
            getattr(library, symbol)
        libraries[name] = hashlib.sha256(output.read_bytes()).hexdigest()
    (destination / "manifest.json").write_text(json.dumps({
        "architecture": target.deb_arch, "libraries": libraries,
        "validation": "C++/Python ABI (4 structs, 63 fields), ELF architecture, dlopen and symbols; hardware not tested",
    }, indent=2) + "\n")
    print(f"Tianji native libraries built ({target.deb_arch}): {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    build(args.output or ROOT / ".build-cache" / f"tianji-native-{native_target().deb_arch}")
