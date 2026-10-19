"""RK3588 compatibility is limited to the desktop and preserves user settings."""
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import board_graphics


@pytest.fixture(autouse=True)
def clean_graphics_environment(monkeypatch):
    for key in ("XIAOKAI_MALI_ACTIVE", "XIAOKAI_PRELOAD_BEFORE_GRAPHICS", "LD_PRELOAD",
                "APP_SOFTWARE_RENDERING", "QT_QPA_PLATFORM", "QT_XCB_GL_INTEGRATION",
                "WAYLAND_DISPLAY"):
        monkeypatch.delenv(key, raising=False)


def test_mali_requires_matching_board_driver_and_packaged_shim(tmp_path, monkeypatch):
    device_tree = Path("/sys/firmware/devicetree/base/compatible")
    driver = Path("/usr/lib/aarch64-linux-gnu/libmali.so.1")
    original_bytes, original_file = Path.read_bytes, Path.is_file
    identity = {"compatible": b"other,board", "driver": True}
    monkeypatch.setattr(Path, "read_bytes", lambda path: identity["compatible"] if path == device_tree else original_bytes(path))
    monkeypatch.setattr(Path, "is_file", lambda path: identity["driver"] if path == driver else original_file(path))
    shim = tmp_path / ".runtime/graphics/libxiaokai-mali-x11.so"
    shim.parent.mkdir(parents=True)
    shim.write_text("test shim")
    assert board_graphics.mali_preloads(tmp_path) == []
    identity["compatible"] = b"xunlong,orangepi-5-plus\x00rockchip,rk3588\x00"
    assert board_graphics.mali_preloads(tmp_path) == [str(shim.resolve()), str(driver)]
    identity["driver"] = False
    assert board_graphics.mali_preloads(tmp_path) == []


@pytest.mark.parametrize("setting", [
    ("APP_SOFTWARE_RENDERING", "1"), ("QT_QPA_PLATFORM", "offscreen"),
    ("WAYLAND_DISPLAY", "wayland-0"), ("QT_XCB_GL_INTEGRATION", "xcb_glx"),
    ("XIAOKAI_MALI_ACTIVE", "1"),
])
def test_explicit_graphics_modes_do_not_trigger_mali_restart(monkeypatch, setting):
    monkeypatch.setenv(*setting)
    monkeypatch.setattr(board_graphics, "mali_preloads", lambda: ["/private/shim.so", "/system/mali.so"])
    monkeypatch.setattr(board_graphics.os, "execve", lambda *_: pytest.fail("unexpected graphics restart"))
    board_graphics.prepare_desktop({})


def test_desktop_restart_adds_only_local_compatibility_and_preserves_user_preload(monkeypatch):
    monkeypatch.setenv("LD_PRELOAD", "/user/original.so")
    monkeypatch.setattr(board_graphics, "mali_preloads", lambda: ["/private/shim.so", "/system/mali.so"])
    calls = []
    monkeypatch.setattr(board_graphics.os, "execve", lambda *args: calls.append(args))
    board_graphics.prepare_desktop({})
    assert len(calls) == 1
    executable, argv, env = calls[0]
    assert executable == sys.executable and argv[0] == sys.executable
    assert env["LD_PRELOAD"] == "/private/shim.so:/system/mali.so:/user/original.so"
    assert env["XIAOKAI_PRELOAD_BEFORE_GRAPHICS"] == "/user/original.so"
    assert env["QT_XCB_GL_INTEGRATION"] == "xcb_egl"
    assert env["XIAOKAI_MALI_ACTIVE"] == "1"
    assert os.environ["LD_PRELOAD"] == "/user/original.so"


@pytest.mark.parametrize("original", ["", "/user/original.so"])
def test_backend_gets_original_environment_without_desktop_driver(monkeypatch, original):
    monkeypatch.setenv("XIAOKAI_MALI_ACTIVE", "1")
    monkeypatch.setenv("XIAOKAI_PRELOAD_BEFORE_GRAPHICS", original)
    monkeypatch.setenv("LD_PRELOAD", "/private/shim.so:/system/mali.so")
    monkeypatch.setenv("QT_XCB_GL_INTEGRATION", "xcb_egl")
    env = board_graphics.backend_environment()
    assert "XIAOKAI_MALI_ACTIVE" not in env
    assert "XIAOKAI_PRELOAD_BEFORE_GRAPHICS" not in env
    assert "QT_XCB_GL_INTEGRATION" not in env
    assert env.get("LD_PRELOAD", "") == original
    assert os.environ["XIAOKAI_MALI_ACTIVE"] == "1"


def test_normal_pc_backend_environment_is_not_changed(monkeypatch):
    monkeypatch.setenv("LD_PRELOAD", "/user/original.so")
    monkeypatch.setenv("QT_XCB_GL_INTEGRATION", "xcb_glx")
    assert board_graphics.backend_environment() == dict(os.environ)
