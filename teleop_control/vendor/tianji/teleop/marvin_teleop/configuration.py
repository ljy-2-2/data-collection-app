"""Vendor configuration validation extracted from the CLI; no collection launcher."""
import json
import math
import ipaddress
from pathlib import Path

def configuration(path):
    c = dict(path) if isinstance(path, dict) else json.loads(Path(path).read_text())
    source_dir = Path(__file__).resolve().parent if isinstance(path, dict) else Path(path).resolve().parent
    retired = {"following_error_soft_deg", "max_following_error_deg"} & c.keys()
    if retired:
        raise ValueError(
            "已删除的跟随误差限流参数仍存在："
            + ", ".join(sorted(retired))
            + "；请从当前配置删除"
        )
    from .tool_frame import validate_tool_config

    c["tool_tcp_source"] = c.get("tool_tcp")
    c["tool_tcp"] = validate_tool_config(c, source_dir)
    # Reject old empirical reflections rather than silently reverse a physical axis.
    if (
        c.get("operator_translation_signs", [1, 1, 1]) != [1, 1, 1]
        or any(
            values != [1, 1, 1]
            for values in c.get("operator_rotation_signs", {}).values()
        )
        or any(
            value is not None for value in c.get("wrist_axis_correction", {}).values()
        )
    ):
        raise ValueError(
            "旧方向反转配置已停用；请删除 operator_translation_signs、"
            "operator_rotation_signs、wrist_axis_correction，使用同向相对映射"
        )
    ipaddress.IPv4Address(c["robot_ip"])
    from .mapping import arm_model_sides, arm_base_sides, arm_mount_orientations
    c['arm_models'] = arm_model_sides(c)
    c['arm_base_sides'] = arm_base_sides(c)
    arm_mount_orientations(c)
    horizontal_signs = c.setdefault('teleop_translation_xy_sign', {'left': 1, 'right': 1})
    if (not isinstance(horizontal_signs, dict) or set(horizontal_signs) != {'left', 'right'}
            or any(type(value) not in (int, float) or value not in (-1, 1)
                   for value in horizontal_signs.values())):
        raise ValueError('teleop_translation_xy_sign must map left/right to +1 or -1')
    c['teleop_translation_xy_sign'] = dict(horizontal_signs)
    vertical_signs = c.setdefault('teleop_translation_z_sign', {'left': 1, 'right': 1})
    if (not isinstance(vertical_signs, dict) or set(vertical_signs) != {'left', 'right'}
            or any(type(value) not in (int, float) or value not in (-1, 1)
                   for value in vertical_signs.values())):
        raise ValueError('teleop_translation_z_sign must map left/right to +1 or -1')
    c['teleop_translation_z_sign'] = dict(vertical_signs)
    if set(c["arm_channels"]) != {"left", "right"} or set(
        c["arm_channels"].values()
    ) != {"A", "B"}:
        raise ValueError("arm_channels must uniquely map left/right to A/B")
    c.setdefault("resume_gap_s", 0.6)
    for key, value in {
        "ik_rotation_weight_m": 0.35,
        "ik_shoulder_motion_weight": 1.0,
        "ik_singular_rotation_weight_m": 0.05,
        "ik_singular_sigma_low": 0.02,
        "ik_singular_sigma_high": 0.08,
        "elbow_branch_margin_deg": 8.0,
        "elbow_posture_offset_deg": 58.0,
        "elbow_posture_gain_s": 2.0,
        "joint_limit_soft_margin_deg": 30.0,
        "joint_limit_reserve_deg": 3.0,
        "joint_limit_escape_speed_deg_s": 20.0,
    }.items():
        c.setdefault(key, value)
    ranges = {
        "scale": (0.1, 2),
        "resume_gap_s": (0.0, 1.0),
        "rate_hz": (10, 100),
        "vr_timeout_s": (0.05, 0.3),
        "robot_timeout_s": (0.05, 0.3),
        # All Marvin M6 joints are rated at 180 deg/s in PNVA. Controller
        # construction below still checks the loaded limits for both arms.
        "max_joint_speed_deg_s": (0.1, 180),
        "max_joint_accel_deg_s2": (1, 450),
        "ik_rotation_weight_m": (0.05, 1.0),
        "ik_shoulder_motion_weight": (1.0, 10.0),
        "ik_singular_rotation_weight_m": (0.05, 1.0),
        "ik_singular_sigma_low": (0.001, 0.2),
        "ik_singular_sigma_high": (0.001, 0.3),
        "elbow_branch_margin_deg": (0.1, 20.0),
        "elbow_posture_offset_deg": (10.0, 65.0),
        "elbow_posture_gain_s": (0.0, 5.0),
        "joint_limit_soft_margin_deg": (0.0, 45.0),
        "joint_limit_reserve_deg": (0.0, 10.0),
        "joint_limit_escape_speed_deg_s": (0.0, 30.0),
        "max_hand_step_m": (0.01, 0.2),
        "max_hand_step_rad": (0.01, 1),
        "collision_margin_m": (0.005, 0.1),
    }
    for key, (lo, hi) in ranges.items():
        if (
            type(c[key]) not in (int, float)
            or not math.isfinite(c[key])
            or not lo <= c[key] <= hi
        ):
            raise ValueError(f"{key} must be in [{lo}, {hi}]")
    if c["ik_singular_rotation_weight_m"] > c["ik_rotation_weight_m"]:
        raise ValueError("singular rotation weight must not exceed normal weight")
    if c["ik_singular_sigma_low"] >= c["ik_singular_sigma_high"]:
        raise ValueError("singular sigma low must be below high")
    if c["joint_limit_reserve_deg"] >= c["joint_limit_soft_margin_deg"]:
        raise ValueError("joint limit reserve must be below soft margin")
    for key in ("port", "status_port"):
        if type(c[key]) is not int or not 1024 <= c[key] <= 65535:
            raise ValueError(f"invalid {key}")
    return c
