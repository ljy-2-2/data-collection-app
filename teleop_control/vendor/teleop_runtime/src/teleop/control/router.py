from itertools import count

from teleop.backends.direct_joint import DirectJointBackend
from teleop.backends.gripper import GripperBackend
from teleop.contracts.command import GripperCommand, JointCommand
from teleop.contracts.intent import (
    EndEffectorPoseIntent,
    GripperIntent,
    JointJogIntent,
    JointPositionIntent,
)
from teleop.contracts.robot_state import RobotState
from teleop.robots.base import RobotAdapter


class ControlRouter:
    """Route intents to a backend without knowing a robot vendor."""

    def __init__(
        self,
        direct_joint_backend: DirectJointBackend,
        gripper_backend: GripperBackend | None = None,
    ) -> None:
        self._direct_joint_backend = direct_joint_backend
        self._gripper_backend = gripper_backend or GripperBackend()
        self._sequence_ids = count(1)

    def route(
        self,
        intent: JointPositionIntent | JointJogIntent | EndEffectorPoseIntent | GripperIntent,
        state: RobotState,
        *,
        now_ns: int,
        robot: RobotAdapter | None = None,
    ) -> JointCommand | GripperCommand:
        if isinstance(intent, GripperIntent):
            return self._gripper_backend.build_command(
                intent,
                sequence_id=next(self._sequence_ids),
                now_ns=now_ns,
            )
        if isinstance(intent, EndEffectorPoseIntent):
            if robot is None:
                raise ValueError("end-effector routing requires a RobotAdapter")
            if not isinstance(robot, RobotAdapter):
                raise TypeError("robot must be a RobotAdapter")
            return robot.solve_ik(
                intent,
                state,
                sequence_id=next(self._sequence_ids),
                now_ns=now_ns,
            )
        if isinstance(intent, (JointPositionIntent, JointJogIntent)):
            return self._direct_joint_backend.build_command(
                intent,
                state,
                sequence_id=next(self._sequence_ids),
                now_ns=now_ns,
            )
        raise TypeError(f"unsupported intent: {type(intent).__name__}")
