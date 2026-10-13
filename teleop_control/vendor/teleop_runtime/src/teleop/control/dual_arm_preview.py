"""Read-only dual-arm preview pipeline; it never sends a robot command."""

from dataclasses import dataclass
from typing import Literal

from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.contracts.command import JointCommand, ValidatedJointCommand
from teleop.contracts.robot_state import RobotState
from teleop.contracts.xr import XRFrame
from teleop.retargeting.dual_arm import (
    DualArmRetargeter,
    DualArmRetargetingResult,
)
from teleop.robots.base import RobotAdapter


@dataclass(frozen=True)
class DualArmPreviewResult:
    status: Literal["stale", "idle", "preview"]
    frame_sequence_id: int
    active_sides: tuple[str, ...]
    retargeting: DualArmRetargetingResult
    state: RobotState | None = None
    command: JointCommand | None = None
    validated: ValidatedJointCommand | None = None


class DualArmPreviewPipeline:
    """Preview one XR frame against fresh telemetry without applying its command."""

    _SIDES = ("left", "right")

    def __init__(
        self,
        *,
        robot: RobotAdapter,
        router: ControlRouter,
        safety: SafetyGateway,
        retargeter: DualArmRetargeter,
    ) -> None:
        if not isinstance(robot, RobotAdapter):
            raise TypeError("robot must be a RobotAdapter")
        if not isinstance(router, ControlRouter):
            raise TypeError("router must be a ControlRouter")
        if not isinstance(safety, SafetyGateway):
            raise TypeError("safety must be a SafetyGateway")
        if not isinstance(retargeter, DualArmRetargeter):
            raise TypeError("retargeter must be a DualArmRetargeter")
        self._robot = robot
        self._router = router
        self._safety = safety
        self._retargeter = retargeter

    def reset(self) -> None:
        """Drop controller anchors and all adapter-owned IK integration."""
        self._retargeter.declutch()
        self._robot.reset_ik(self._SIDES)

    def preview(self, frame: XRFrame, *, now_ns: int) -> DualArmPreviewResult:
        if not isinstance(frame, XRFrame):
            raise TypeError("frame must be an XRFrame")
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a non-negative integer")

        if frame.stale:
            retargeting = self._retargeter.update(frame, {})
            self.reset()
            return DualArmPreviewResult(
                status="stale",
                frame_sequence_id=frame.sequence_id,
                active_sides=(),
                retargeting=retargeting,
            )

        state = self._robot.read_state(now_ns=now_ns)
        anchors = self._robot.get_end_effector_poses(state)
        retargeting = self._retargeter.update(frame, anchors)
        if retargeting.intent is None:
            self.reset()
            return DualArmPreviewResult(
                status="idle",
                frame_sequence_id=frame.sequence_id,
                active_sides=(),
                retargeting=retargeting,
                state=state,
            )

        active_sides = tuple(retargeting.intent.targets)
        # A read-only robot does not follow the candidate command. Re-seeding from
        # fresh telemetry on every frame keeps this preview honest and bounded.
        self._robot.reset_ik(self._SIDES)
        try:
            command = self._router.route(
                retargeting.intent,
                state,
                now_ns=now_ns,
                robot=self._robot,
            )
            validated = self._safety.validate(
                command,
                state,
                self._robot.get_joint_schema(),
                now_ns=now_ns,
            )
        finally:
            self._robot.reset_ik(self._SIDES)
        return DualArmPreviewResult(
            status="preview",
            frame_sequence_id=frame.sequence_id,
            active_sides=active_sides,
            retargeting=retargeting,
            state=state,
            command=command,
            validated=validated,
        )
