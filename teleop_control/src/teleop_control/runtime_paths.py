"""Locate bundled robot dependencies without referring to the builder's home."""
from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
LEGACY_XR_SERVICE = "/opt/apps/roboticsservice/runService.sh"


def robot_runtime() -> Path | None:
    for path in (APP_ROOT / ".runtime/robot", APP_ROOT.parent / "runtime/robot"):
        if path.is_dir():
            return path.resolve()
    return None


def resolve_robot_python(configured: str | None = None) -> str | None:
    if configured:
        return str(Path(configured).expanduser())
    runtime = robot_runtime()
    if runtime and (runtime / "bin/python3").is_file():
        return str(runtime / "bin/python3")
    return None


def resolve_gdk_root(configured: str | None = None) -> str:
    runtime = robot_runtime()
    if runtime and (not configured or (
        configured == "~/.cache/agibot/app" and not Path(configured).expanduser().exists()
    )):
        return str(runtime / "gdk")
    return str(Path(configured or "~/.cache/agibot/app").expanduser())


def resolve_xr_service(configured: str | None = None) -> str:
    selected = configured or os.environ.get("TELEOP_XR_SERVICE") or os.environ.get("TELEOP_XR_LAUNCHER")
    runtime = robot_runtime()
    if runtime and (not selected or (
        selected == LEGACY_XR_SERVICE and not Path(selected).exists()
    )):
        return str(runtime / "xr/runService.sh")
    return str(Path(selected or LEGACY_XR_SERVICE).expanduser())
