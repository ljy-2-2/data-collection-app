"""Explicit commissioning values; no guessed mechanical travel in live mode."""

import json
import math
from pathlib import Path


def load(path, live=False):
    c = json.loads(Path(path).read_text())
    if c.get("transport") not in ("marvin_can", "socketcan"):
        raise ValueError("gripper: transport must be marvin_can or socketcan")
    if c["transport"] == "socketcan":
        if not isinstance(c.get("channel"), str) or not c["channel"]:
            raise ValueError("gripper: SocketCAN channel required")
    else:
        if type(c.get("tx_id_bytes")) is not int or c["tx_id_bytes"] not in (2, 4):
            raise ValueError("gripper: tx_id_bytes must be 2 or 4")
        if type(c.get("rx_id_bytes")) is not int or c["rx_id_bytes"] not in (0, 2, 4):
            raise ValueError("gripper: rx_id_bytes must be 0, 2 or 4")
        if live and c.get("endpoint_verified") is not True:
            raise ValueError(
                "gripper: verify each endpoint CAN/CANFD mode, bitrate, framing and power; set endpoint_verified=true"
            )
    motors = c.get("motors", {})
    if not motors or not set(motors) <= {"left", "right"}:
        raise ValueError("gripper: motors must contain left and/or right")
    ids, replies = set(), set()
    for side, m in motors.items():
        # Legacy labels never select a protocol or gate compatibility.
        m.pop("hardware_version", None)
        # Old per-side overrides must not follow a replacement motor.
        m.pop("verified_imax_a", None)
        m.pop("verified_imax_source", None)
        m.setdefault("control_mode", 4)
        m.setdefault("kp", 20.0)
        m.setdefault("kd", 1.0)
        m.setdefault("watchdog_ticks", 0)
        m.setdefault("accel_rad_s2", 20.0)
        if m["control_mode"] not in (1, 4):
            raise ValueError(f"{side}: control_mode must be 1 (MIT) or 4 (force-position)")
        for key, protocol_max in (("kp", 500), ("kd", 5)):
            value = m[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{side}.{key} must be a finite nonnegative value")
            # Force-position frames do not contain these MIT gains. Only
            # enforce their wire-format range when they will actually be sent.
            if m["control_mode"] == 1 and value > protocol_max:
                raise ValueError(
                    f"{side}.{key}: MIT protocol requires [0, {protocol_max}]"
                )
        if m["control_mode"] == 1 and m["kp"] > 0 and m["kd"] <= 0:
            raise ValueError(
                f"{side}: MIT position control requires kd>0 (manual: kd=0 oscillates)"
            )
        if type(m["watchdog_ticks"]) is not int or (
            m["watchdog_ticks"] != 0 and not 1000 <= m["watchdog_ticks"] <= 6000
        ):
            raise ValueError(f"{side}.watchdog_ticks must be 0 or 1000..6000")
        if "expected_can_bitrate_code" in m and (
            type(m["expected_can_bitrate_code"]) is not int
            or not 0 <= m["expected_can_bitrate_code"] <= 9
        ):
            raise ValueError(f"{side}: expected_can_bitrate_code must be 0..9")
        if type(m.get("motor_id")) is not int or not 1 <= m["motor_id"] <= 15:
            raise ValueError(f"{side}: motor_id must be 1..15 (status ID nibble)")
        if type(m.get("master_id")) is not int or not 0 <= m["master_id"] < 0x7FF:
            raise ValueError(f"{side}: master_id must be 0..2046")
        if c["transport"] == "socketcan" and (
            m["motor_id"] in ids or m["master_id"] in replies
        ):
            raise ValueError("gripper: motor IDs and master IDs must each be unique")
        ids.add(m["motor_id"])
        replies.add(m["master_id"])
        ranges = {
            # Offline inspection accepts legacy coordinates; live mode below
            # requires travel inside the documented cold-start single turn.
            # Driver.preflight independently enforces the measured PMAX.
            "open_rad": (-2 * math.pi, 2 * math.pi),
            "closed_rad": (-2 * math.pi, 2 * math.pi),
            # Mode 4 firmware saturates v_des at 10000 / 100 = 100 rad/s.
            "speed_rad_s": (0.01, 100),
            "accel_rad_s2": (0.1, 200),
            "current_fraction": (0.0001, 0.2),
            "feedback_timeout_s": (0.05, 0.3),
            "max_temperature_c": (20, 80),
        }
        for key, (low, high) in ranges.items():
            v = m.get(key)
            if (
                type(v) not in (int, float)
                or not math.isfinite(v)
                or not low <= v <= high
            ):
                raise ValueError(f"{side}.{key}: set a finite value in [{low}, {high}]")
        if abs(m["closed_rad"] - m["open_rad"]) < 0.01:
            raise ValueError(f"{side}: open/closed travel must differ by >=0.01 rad")
        travel_bounds(m)
        if "max_current_a" in m:
            value = m["max_current_a"]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 2.5:
                raise ValueError(f"{side}: max_current_a must be in (0, 2.5] A")
            if m["control_mode"] != 4:
                raise ValueError(f"{side}: max_current_a requires force-position mode 4")
        if live and max(abs(m["open_rad"]), abs(m["closed_rad"])) >= math.pi - 0.02:
            raise ValueError(f"{side}: travel must remain inside startup single-turn [-pi, pi]; recheck calibration")
        if live and m.get("commissioned") is not True:
            raise ValueError(
                f"{side}: calibrate travel/current and set commissioned=true first"
            )
    # Avoid a feedback ID being interpreted as another motor's control command.
    if any((r & 0xFF) in ids for r in replies):
        raise ValueError("gripper: Master ID low byte must not match any motor ID")
    return c


def travel_bounds(motor):
    """Inset command range; measured calibration endpoints remain unchanged."""
    lo, hi = sorted((motor["open_rad"], motor["closed_rad"]))
    margin = motor.get("travel_margin_rad", 0.02)
    if not math.isfinite(margin) or margin < 0.002 or 2 * margin >= hi - lo:
        raise ValueError("gripper: travel margin must be >=0.002 and less than half travel")
    return lo + margin, hi - margin


def bounded_position(motor, position):
    if not math.isfinite(position):
        raise ValueError("gripper: nonfinite position")
    lo, hi = travel_bounds(motor)
    return max(lo, min(hi, position))
