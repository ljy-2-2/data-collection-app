import math

from teleop.contracts.command import JointCommand, ValidatedJointCommand
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointSchema


_FLOAT_COMPARISON_TOLERANCE = 1e-12


class SafetyError(ValueError):
    """A command was rejected before reaching a RobotAdapter."""


class SafetyGateway:
    """Shared minimum checks for every named-joint command path."""

    def __init__(self, max_state_age_ms: int = 200) -> None:
        if type(max_state_age_ms) is not int or max_state_age_ms <= 0:
            raise ValueError("max_state_age_ms must be a positive integer")
        self._max_state_age_ns = max_state_age_ms * 1_000_000

    def validate(
        self,
        command: JointCommand,
        state: RobotState,
        schema: JointSchema,
        *,
        now_ns: int,
    ) -> ValidatedJointCommand:
        if type(now_ns) is not int or now_ns < 0:
            raise SafetyError("now_ns must be a non-negative integer")
        state_age_ns = now_ns - state.timestamp_ns
        if state_age_ns < 0:
            raise SafetyError("robot state timestamp is in the future")
        if state_age_ns > self._max_state_age_ns:
            raise SafetyError("robot state is stale")

        command_age_ns = now_ns - command.created_time_ns
        if command_age_ns < 0:
            raise SafetyError("joint command timestamp is in the future")
        if command_age_ns > command.ttl_ms * 1_000_000:
            raise SafetyError("joint command has expired")

        for name, target in command.targets.items():
            try:
                joint = schema.get(name)
            except KeyError as error:
                raise SafetyError(f"unknown joint: {name}") from error

            if not joint.independently_commandable:
                raise SafetyError(f"joint {name} is not independently commandable")
            if not math.isfinite(target):
                raise SafetyError(f"joint {name} target must be finite")
            if not joint.position_min <= target <= joint.position_max:
                raise SafetyError(f"joint {name} target exceeds position limit")
            if name not in state.positions:
                raise SafetyError(f"joint {name} is missing from robot state")

            current = state.positions[name]
            if not math.isfinite(current):
                raise SafetyError(f"joint {name} current state must be finite")
            if (
                abs(target - current)
                > joint.max_step + _FLOAT_COMPARISON_TOLERANCE
            ):
                raise SafetyError(f"joint {name} target exceeds step limit")

        return ValidatedJointCommand(
            targets=command.targets,
            source=command.source,
            sequence_id=command.sequence_id,
            created_time_ns=command.created_time_ns,
            ttl_ms=command.ttl_ms,
        )
