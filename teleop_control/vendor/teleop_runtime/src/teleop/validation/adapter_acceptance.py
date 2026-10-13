from dataclasses import dataclass, field
import math

from teleop.contracts.capabilities import RobotCapabilities
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointSchema


@dataclass(frozen=True)
class JointAcceptance:
    name: str
    group: str
    current_position: float | None
    position_min: float
    position_max: float
    max_step: float
    max_velocity: float | None
    passed: bool
    reason: str


@dataclass(frozen=True)
class AdapterAcceptanceReport:
    capabilities: RobotCapabilities
    expected_joint_count: int
    joints: tuple[JointAcceptance, ...]
    issues: tuple[str, ...]
    physical_robot_command_sent: bool = field(default=False, init=False)

    @property
    def passed(self) -> bool:
        return not self.issues and all(joint.passed for joint in self.joints)


def inspect_adapter_snapshot(
    *,
    capabilities: RobotCapabilities,
    schema: JointSchema,
    state: RobotState,
    expected_joint_count: int,
) -> AdapterAcceptanceReport:
    """Check one adapter-owned schema and state snapshot without sending commands."""
    if not isinstance(capabilities, RobotCapabilities):
        raise TypeError("capabilities must be RobotCapabilities")
    if not isinstance(schema, JointSchema):
        raise TypeError("schema must be JointSchema")
    if not isinstance(state, RobotState):
        raise TypeError("state must be RobotState")
    if type(expected_joint_count) is not int or expected_joint_count <= 0:
        raise ValueError("expected_joint_count must be a positive integer")

    issues: list[str] = []
    if not capabilities.joint_state_read:
        issues.append("adapter does not advertise joint-state read capability")
    if len(schema.joints) != expected_joint_count:
        issues.append(
            f"expected {expected_joint_count} joints but schema exposes {len(schema.joints)}"
        )

    rows: list[JointAcceptance] = []
    for joint in schema.joints:
        current = state.positions.get(joint.name)
        normalized_current: float | None = None
        if current is None:
            passed = False
            reason = "missing from robot state"
        elif isinstance(current, bool) or not isinstance(current, (int, float)):
            passed = False
            reason = "state is not numeric"
        elif not math.isfinite(current):
            passed = False
            reason = "state is not finite"
        elif not joint.position_min <= current <= joint.position_max:
            passed = False
            reason = "state is outside position limits"
        else:
            passed = True
            reason = "ok"
            normalized_current = float(current)
        rows.append(
            JointAcceptance(
                name=joint.name,
                group=joint.group,
                current_position=normalized_current,
                position_min=joint.position_min,
                position_max=joint.position_max,
                max_step=joint.max_step,
                max_velocity=joint.max_velocity,
                passed=passed,
                reason=reason,
            )
        )

    return AdapterAcceptanceReport(
        capabilities=capabilities,
        expected_joint_count=expected_joint_count,
        joints=tuple(rows),
        issues=tuple(issues),
    )
