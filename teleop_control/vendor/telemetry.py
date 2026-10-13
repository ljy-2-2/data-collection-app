"""Fail-closed helpers for robot joint telemetry used by teleoperation."""
import math


def joint_positions(robot):
    positions = {}
    states = robot.get_joint_states().get("states", [])
    for state in states if isinstance(states, list) else []:
        if not isinstance(state, dict):
            continue
        name = state.get("name", state.get("joint_name"))
        raw_position = state.get("motor_position")
        if name is None or raw_position is None:
            continue
        try:
            position = float(raw_position)
        except (TypeError, ValueError):
            continue
        if math.isfinite(position):
            positions[str(name)] = position
    return positions


def complete_joint_positions(names, positions):
    missing = [name for name in names if name not in positions]
    if missing:
        raise ValueError(f"missing joint telemetry: {', '.join(missing)}")
    values = [float(positions[name]) for name in names]
    invalid = [name for name, value in zip(names, values) if not math.isfinite(value)]
    if invalid:
        raise ValueError(f"non-finite joint telemetry: {', '.join(invalid)}")
    return values


def complete_action(names, action_state):
    try:
        return complete_joint_positions(names, action_state)
    except (TypeError, ValueError):
        return None


def backfill_action_state(action_state, names, measured_positions):
    for name in names:
        if name in action_state or name not in measured_positions:
            continue
        try:
            position = float(measured_positions[name])
        except (TypeError, ValueError):
            continue
        if math.isfinite(position):
            action_state[name] = position


def end_joint_positions(robot, sides):
    positions = {}
    all_end_states = robot.get_end_state()
    for side in sides:
        end_state = all_end_states.get(f"{side}_end_state", {})
        for name, row in zip(end_state.get("names", []), end_state.get("end_states", [])):
            try:
                position = float(row["position"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(position):
                positions[str(name)] = position
    return positions
