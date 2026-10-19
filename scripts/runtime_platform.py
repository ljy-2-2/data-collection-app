"""Native desktop/runtime targets. An ARM package must contain ARM binaries."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import platform
import struct
import hashlib
import json


@dataclass(frozen=True)
class RuntimeTarget:
    deb_arch: str
    python_machine: str
    qt_version: str
    elf_machine: int


def native_target(machine: str | None = None) -> RuntimeTarget:
    machine = (machine or platform.machine()).lower()
    if machine in {"x86_64", "amd64"}:
        return RuntimeTarget("amd64", "x86_64", "6.8.3", 62)
    if machine in {"aarch64", "arm64"}:
        # Qt 6.8 ARM wheels require glibc 2.39; Orange Pi Jammy has 2.35.
        # Qt 6.7.3 ships manylinux_2_31 ARM wheels, including Qt Quick 3D.
        return RuntimeTarget("arm64", "aarch64", "6.7.3", 183)
    raise ValueError(f"不支持的 CPU 架构：{machine}；需要 Linux x86_64 或 ARM64")


def verify_elf(path: Path, target: RuntimeTarget) -> None:
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01":
        raise ValueError(f"不是受支持的 64 位 Linux ELF 程序：{path}")
    actual = struct.unpack_from("<H", header, 18)[0]
    if actual != target.elf_machine:
        raise ValueError(f"运行库架构错误：{path}，目标 {target.deb_arch}，ELF machine={actual}")


def validate_runtime_architecture(manifest: dict, target: RuntimeTarget) -> None:
    # Historical releases without architecture metadata were amd64 only.
    actual = manifest.get("architecture", "amd64")
    if actual != target.deb_arch:
        raise ValueError(f"UMI 镜像架构为 {actual}，不能打入 {target.deb_arch} 安装包；请重新构建采集镜像")


def validate_tianji_libraries(directory: Path, target: RuntimeTarget) -> list[Path]:
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("architecture") != target.deb_arch:
        raise ValueError("天机 SDK 架构与安装包不一致")
    libraries = []
    for name in ("libMarvinSDK.so", "libKine.so"):
        path = directory / name
        verify_elf(path, target)
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get("libraries", {}).get(name):
            raise ValueError(f"天机 SDK 校验失败：{name}")
        libraries.append(path)
    return libraries
