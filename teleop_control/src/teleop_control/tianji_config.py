"""Shared Tianji connection settings for the robot worker and camera client."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path

from .runtime_paths import resolve_xr_service


DEFAULT_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "vendor/tianji/teleop/marvin_teleop/config.json"
)


def robot_configuration() -> dict:
    path = Path(os.environ.get("TIANJI_CONFIG") or DEFAULT_CONFIG).expanduser()
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("天机 Marvin M6配置必须为 JSON 对象")
    # Preserve named URDF TCPs when passing an in-memory config to the worker.
    # Resolve against the selected JSON, not the current working directory.
    tools = config.get("tool_tcp")
    if isinstance(tools, dict):
        for definition in tools.values():
            if isinstance(definition, dict) and isinstance(definition.get("urdf"), str):
                urdf = Path(definition["urdf"]).expanduser()
                if not urdf.is_absolute():
                    urdf = path.resolve().parent / urdf
                definition["urdf"] = str(urdf.resolve())
    address = os.environ.get("TIANJI_ROBOT_IP") or config.get("robot_ip")
    try:
        if not isinstance(address, str):
            raise TypeError("robot_ip must be a string")
        config["robot_ip"] = str(ipaddress.IPv4Address(address))
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise ValueError("天机 Marvin M6 robot_ip / TIANJI_ROBOT_IP 必须为有效 IPv4 地址") from exc
    service = (
        os.environ.get("TELEOP_XR_SERVICE")
        or os.environ.get("TELEOP_XR_LAUNCHER")
        or str(Path(config.get("service_dir", "/opt/apps/roboticsservice")) / "runService.sh")
    )
    config["service_dir"] = str(Path(resolve_xr_service(service)).parent)
    return config


def camera_host() -> str:
    # Some installations serve the cameras on a separate host; ordinarily
    # they belong to the configured robot and must follow its address.
    override = os.environ.get("TIANJI_CAMERA_HOST", "").strip()
    return override or robot_configuration()["robot_ip"]
