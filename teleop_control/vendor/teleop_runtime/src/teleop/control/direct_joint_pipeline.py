from dataclasses import dataclass

from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.contracts.command import JointCommand, ValidatedJointCommand
from teleop.contracts.intent import JointJogIntent, JointPositionIntent
from teleop.contracts.robot_state import RobotState
from teleop.robots.base import RobotAdapter


@dataclass(frozen=True)
class DirectJointExecution:
    """Observed result of one intent after the complete safe command path."""

    before: RobotState
    command: JointCommand
    validated: ValidatedJointCommand
    after: RobotState


class DirectJointPipeline:
    """Run one direct-joint intent through routing, safety, and an adapter."""

    def __init__(
        self,
        *,
        robot: RobotAdapter,
        router: ControlRouter,
        safety: SafetyGateway,
    ) -> None:
        if not isinstance(robot, RobotAdapter):
            raise TypeError("robot must be a RobotAdapter")
        if not isinstance(router, ControlRouter):
            raise TypeError("router must be a ControlRouter")
        if not isinstance(safety, SafetyGateway):
            raise TypeError("safety must be a SafetyGateway")
        self._robot = robot
        self._router = router
        self._safety = safety

    @property
    def robot(self) -> RobotAdapter:
        return self._robot

    def execute(
        self,
        intent: JointPositionIntent | JointJogIntent,
        *,
        now_ns: int,
    ) -> DirectJointExecution:
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a non-negative integer")

        before = self._robot.read_state()
        command = self._router.route(intent, before, now_ns=now_ns)
        validated = self._safety.validate(
            command,
            before,
            self._robot.get_joint_schema(),
            now_ns=now_ns,
        )
        self._robot.send_joint_command(validated, now_ns=now_ns)
        after = self._robot.read_state()
        return DirectJointExecution(
            before=before,
            command=command,
            validated=validated,
            after=after,
        )
