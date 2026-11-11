"""Reject dependencies that cannot travel with an amd64 release."""
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_runtime_update import verify_runtime
from runtime_platform import native_target


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "runtime"
    for name in ("python/bin/python3.12", "sysroot/usr/bin/python3.10",
                 "sysroot/usr/bin/adb", "sysroot/usr/bin/v4l2-ctl",
                 "sysroot/usr/bin/dockerd", "sysroot/usr/bin/runc",
                 "sysroot/usr/libexec/docker/cli-plugins/docker-compose",
                 "robot/bin/python3", "robot/manifest.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return root


def test_amd64_is_shared_by_intel_and_amd_not_an_arm_alias():
    assert native_target("AMD64") == native_target("x86_64")
    assert native_target("AMD64").elf_machine != native_target("arm64").elf_machine


def test_full_update_rejects_a_foreign_native_dependency(runtime):
    library = runtime / "robot/library.so"
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, 183)
    library.write_bytes(header)
    with pytest.raises(ValueError, match="架构错误"):
        verify_runtime(runtime)


def test_full_update_cannot_depend_on_builder_home(runtime, tmp_path):
    external = tmp_path / "developer-python"
    external.write_text("private dependency")
    (runtime / "external-library").symlink_to(external)
    with pytest.raises(ValueError, match="escapes"):
        verify_runtime(runtime)


def test_full_update_requires_robot_runtime(runtime):
    (runtime / "robot/manifest.json").unlink()
    with pytest.raises(ValueError, match="Incomplete"):
        verify_runtime(runtime)
