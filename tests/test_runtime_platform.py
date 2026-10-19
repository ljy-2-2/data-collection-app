import json
import hashlib
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from runtime_platform import native_target, validate_runtime_architecture, verify_elf, validate_tianji_libraries


def test_jammy_arm_uses_compatible_qt_without_changing_pc_release():
    assert native_target("aarch64").qt_version == "6.7.3"
    assert native_target("x86_64").qt_version == "6.8.3"
    assert native_target("arm64") == native_target("aarch64")
    with pytest.raises(ValueError):
        native_target("armv7l")


def test_arm_package_rejects_existing_pc_runtime_and_legacy_manifest():
    target = native_target("aarch64")
    for manifest in ({}, {"architecture": "amd64"}):
        with pytest.raises(ValueError, match="不能打入"):
            validate_runtime_architecture(manifest, target)
    validate_runtime_architecture({"architecture": "arm64"}, target)
    validate_runtime_architecture({}, native_target("x86_64"))


def test_binary_check_rejects_mislabeled_python(tmp_path):
    binary = tmp_path / "python3.12"
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, 62)
    binary.write_bytes(header)
    verify_elf(binary, native_target("x86_64"))
    with pytest.raises(ValueError, match="架构错误"):
        verify_elf(binary, native_target("aarch64"))
    struct.pack_into("<H", header, 18, 183)
    binary.write_bytes(header)
    verify_elf(binary, native_target("aarch64"))
    binary.write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(ValueError, match="ELF"):
        verify_elf(binary, native_target("aarch64"))


def test_tianji_bundle_requires_both_correctly_hashed_arm_libraries(tmp_path):
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, 183)
    libraries = {}
    for name in ("libMarvinSDK.so", "libKine.so"):
        (tmp_path / name).write_bytes(header)
        libraries[name] = hashlib.sha256(header).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"architecture": "arm64", "libraries": libraries}))
    assert len(validate_tianji_libraries(tmp_path, native_target("aarch64"))) == 2
    with pytest.raises(ValueError, match="架构"):
        validate_tianji_libraries(tmp_path, native_target("x86_64"))
    (tmp_path / "libKine.so").write_bytes(header + b"changed")
    with pytest.raises(ValueError, match="校验失败"):
        validate_tianji_libraries(tmp_path, native_target("aarch64"))
