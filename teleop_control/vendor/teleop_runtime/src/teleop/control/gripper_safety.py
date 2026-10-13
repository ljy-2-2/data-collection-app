from teleop.contracts.command import GripperCommand, ValidatedGripperCommand


class GripperSafetyGateway:
    """Validate normalized gripper bounds and command freshness."""

    def validate(
        self,
        command: GripperCommand,
        *,
        now_ns: int,
    ) -> ValidatedGripperCommand:
        if not isinstance(command, GripperCommand):
            raise TypeError("command must be a GripperCommand")
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a non-negative integer")
        age_ns = now_ns - command.created_time_ns
        if age_ns < 0:
            raise ValueError("gripper command timestamp is in the future")
        if age_ns > command.ttl_ms * 1_000_000:
            raise ValueError("gripper command has expired")
        # Reconstructing the validated type deliberately repeats all contract
        # checks, so a forged marker cannot bypass this boundary.
        return ValidatedGripperCommand(
            targets=command.targets,
            source=command.source,
            sequence_id=command.sequence_id,
            created_time_ns=command.created_time_ns,
            ttl_ms=command.ttl_ms,
        )
