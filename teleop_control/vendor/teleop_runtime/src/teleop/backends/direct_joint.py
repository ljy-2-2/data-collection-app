from teleop.contracts.command import JointCommand
from teleop.contracts.intent import JointJogIntent, JointPositionIntent
from teleop.contracts.robot_state import RobotState


class DirectJointBackend:
    """Convert direct joint intents into commands without using IK."""

    def __init__(self, default_ttl_ms: int = 100) -> None:
        if default_ttl_ms <= 0:
            raise ValueError("default_ttl_ms must be positive")
        self._default_ttl_ms = default_ttl_ms

    def build_command(
        self,
        intent: JointPositionIntent | JointJogIntent,
        state: RobotState,
        *,
        sequence_id: int,
        now_ns: int,
    ) -> JointCommand:
        if isinstance(intent, JointPositionIntent):
            targets = dict(intent.targets)
        elif isinstance(intent, JointJogIntent):
            targets = {}
            for name, delta in intent.deltas.items():
                if name not in state.positions:
                    raise ValueError(
                        f"joint {name!r} requires a fresh state before jogging"
                    )
                targets[name] = state.positions[name] + delta
        else:
            raise TypeError(f"unsupported direct joint intent: {type(intent).__name__}")

        return JointCommand(
            targets=targets,
            source=intent.source,
            sequence_id=sequence_id,
            created_time_ns=now_ns,
            ttl_ms=self._default_ttl_ms,
        )
