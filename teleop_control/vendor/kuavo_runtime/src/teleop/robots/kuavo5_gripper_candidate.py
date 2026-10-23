"""Offline migration proposal, not a motor configuration writer or live profile.

Reads an explicit JSON file only. All physical gates remain false even when
known-ID checks pass. Device inventories do not cover all on-wire CAN IDs.
"""
import json
from pathlib import Path
from types import SimpleNamespace

from teleop.robots.damiao_gripper import validate_routes
from teleop.robots.leju_can_capture import MotorRoute


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def load_candidate(path):
    # Reject duplicated keys, rather than silently overriding a safety field.
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate configuration key: " + key)
            result[key] = value
        return result
    config = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)
    assess_candidate(config)
    return config


def assess_candidate(config):
    if not isinstance(config, dict):
        raise ValueError("candidate must be a JSON object")
    if type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        raise ValueError("unsupported candidate schema")
    if config.get("purpose") != "offline_migration_candidate":
        raise ValueError("offline migration candidate required")
    for field in ("physical_send_enabled", "commissioned", "inventory_complete"):
        if config.get(field) is not False:
            raise ValueError(field + " must remain false; this is not a commissioning interface")
    if config.get("frame_format") != "classic_standard":
        raise ValueError("only classic standard frames in this candidate")
    for field, expected in (("control_mode", 4), ("can_bitrate_code", 4),
                            ("can_bitrate_bps", 1000000), ("target_control_rate_hz", 100)):
        if type(config.get(field)) is not int or config[field] != expected:
            raise ValueError(f"{field} must be {expected} for the reviewed proposal")
    motors, known = config.get("motors"), config.get("known_device_ids")
    if not isinstance(motors, dict) or set(motors) != {"left", "right"}:
        raise ValueError("explicit left and right routes required")
    if not isinstance(known, dict) or set(known) != {"canbus0", "canbus1"}:
        raise ValueError("both known bus inventories required")
    occupied = {}
    for bus, ids in known.items():
        if not isinstance(ids, list) or not ids:
            raise ValueError("nonempty partial device inventory required")
        for value in ids:
            _integer(value, "known device ID", 1, 255)
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate known device ID")
        # Known Revo ordinary commands/replies and multi-turn-zero ID offset.
        # This expansion is partial evidence, NEVER a complete bus inventory.
        occupied[bus] = set(ids) | {value + 0x600 for value in ids}
    profiles, routes = {}, {}
    for side, expected_bus in (("left", "canbus0"), ("right", "canbus1")):
        motor = motors[side]
        if not isinstance(motor, dict) or set(motor) != {
                "bus", "motor_id", "master_id", "device_readback", "calibration", "current_limits"}:
            raise ValueError(side + ": unexpected or missing candidate fields")
        if motor["bus"] != expected_bus:
            raise ValueError(side + ": bus differs from the reviewed proposal")
        for field in ("device_readback", "calibration", "current_limits"):
            if motor[field] is not None:
                raise ValueError(side + ": " + field + " must remain null in this uncommissioned proposal")
        route = MotorRoute(motor["bus"], motor["master_id"], motor["motor_id"])
        if route.master_id != 0:
            raise ValueError(side + ": feedback ID must be 0 in the reviewed proposal")
        profiles[side] = SimpleNamespace(bus=route.bus, motor_id=route.motor_id, master_id=route.master_id)
        routes[side] = dict(bus=route.bus, motor_id=route.motor_id,
                            feedback_can_id=route.master_id,
                            control_can_id=0x300 + route.motor_id,
                            control_can_id_hex=f"0x{0x300 + route.motor_id:03X}")
    validate_routes(profiles, occupied)
    return dict(status="offline_candidate_valid", routes=routes,
                known_device_id_check="pass_for_partial_inventory_only",
                inventory_complete=False, physical_send_enabled=False,
                physical_configuration_verified=False, physical_motor_communication_verified=False,
                mechanical_calibration_verified=False,
                can_bitrate_bps=config["can_bitrate_bps"], can_bitrate_code=config["can_bitrate_code"],
                control_mode=config["control_mode"], frame_format=config["frame_format"],
                target_control_rate_hz=config["target_control_rate_hz"],
                hardware_blockers=["complete_bus_id_and_protocol_audit",
                    "physical_left_right_wiring_and_motor_identity",
                    "motor_parameter_readback_after_separate_commissioning",
                    "owner_receive_export_deployment_and_real_feedback",
                    "owner_send_deadline_and_revoke_implementation",
                    "measured_motor_limits_and_mechanical_calibration",
                    "operator_hardware_acceptance"])
