"""Rigid flange-to-TCP geometry. No gripper actuation or vendor tool mutation."""

import numpy as np
from .geometry import transform


def parse_tool_frame(value):
    if value is None:
        return np.eye(4), "bare_flange"
    if not isinstance(value, dict) or set(value) != {"name", "xyz_mm", "rpy_deg"}:
        raise ValueError("tool_tcp 每侧需包含 name、xyz_mm、rpy_deg；未确定时使用 null")
    if not isinstance(value["name"], str) or not value["name"].strip():
        raise ValueError("tool_tcp.name must identify the physical control point")
    for key in ("xyz_mm", "rpy_deg"):
        a = value[key]
        if (
            not isinstance(a, list)
            or len(a) != 3
            or any(type(x) not in (int, float) or not np.isfinite(x) for x in a)
        ):
            raise ValueError(f"tool_tcp.{key} must contain three finite numbers")
    if (
        np.linalg.norm(value["xyz_mm"]) > 500
        or max(abs(x) for x in value["rpy_deg"]) > 180
    ):
        raise ValueError("tool_tcp offset must be <=500 mm and rpy within ±180 degrees")
    return transform(
        np.array(value["xyz_mm"]) / 1000, np.radians(value["rpy_deg"])
    ), value["name"]


def validate_tool_config(config):
    value = config.get("tool_tcp", {"left": None, "right": None})
    if not isinstance(value, dict) or set(value) != {"left", "right"}:
        raise ValueError("tool_tcp must explicitly contain left and right")
    for definition in value.values():
        parse_tool_frame(definition)
    return value
