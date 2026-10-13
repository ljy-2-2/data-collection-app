from teleop.contracts.command import GripperCommand
from teleop.contracts.intent import GripperIntent


class GripperBackend:
    """Convert a normalized gripper intent into a timestamped command."""

    def __init__(self, default_ttl_ms: int = 100) -> None:
        if type(default_ttl_ms) is not int or default_ttl_ms <= 0:
            raise ValueError("default_ttl_ms must be a positive integer")
        self._default_ttl_ms = default_ttl_ms

    def build_command(
        self,
        intent: GripperIntent,
        *,
        sequence_id: int,
        now_ns: int,
    ) -> GripperCommand:
        if not isinstance(intent, GripperIntent):
            raise TypeError("intent must be GripperIntent")
        return GripperCommand(
            targets=intent.targets,
            source=intent.source,
            sequence_id=sequence_id,
            created_time_ns=now_ns,
            ttl_ms=self._default_ttl_ms,
        )
