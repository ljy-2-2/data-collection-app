"""Pure Kuavo arm-joint unit/order encoder; this module never publishes ROS."""

from dataclasses import dataclass
import math
from numbers import Real

from teleop.contracts.command import JointCommand
from teleop.robots.kuavo5_native_ik import ARM_NAMES


class KuavoArmJointEncodingError(ValueError):
    """The candidate cannot be represented by the audited Kuavo arm topic."""


@dataclass(frozen=True)
class KuavoArmJointEncoding:
    names: tuple[str, ...]
    positions_deg: tuple[float, ...]
    sequence_id: int
    physical_robot_command_sent: bool = False


def encode_kuavo_arm_joint_command(command: JointCommand) -> KuavoArmJointEncoding:
    """Encode 14 internal radian targets into Kuavo's documented degree order."""
    if not isinstance(command, JointCommand):
        raise KuavoArmJointEncodingError("JointCommand is required")
    if set(command.targets) != set(ARM_NAMES):
        raise KuavoArmJointEncodingError("command must contain exactly 14 Kuavo arm joints")
    values = tuple(command.targets[name] for name in ARM_NAMES)
    if any(isinstance(value, bool) or not isinstance(value, Real)
           or not math.isfinite(value) for value in values):
        raise KuavoArmJointEncodingError("joint targets must be finite numeric radians")
    return KuavoArmJointEncoding(
        ARM_NAMES,
        tuple(math.degrees(float(value)) for value in values),
        command.sequence_id,
    )
