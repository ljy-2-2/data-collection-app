from abc import ABC, abstractmethod
from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, Protocol

from teleop.contracts.capabilities import RobotCapabilities
from teleop.contracts.command import (
    JointCommand,
    ValidatedGripperCommand,
    ValidatedJointCommand,
)
from teleop.contracts.intent import CartesianPose, EndEffectorPoseIntent
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointInfo, JointSchema


class UnsupportedCapabilityError(RuntimeError):
    """The selected adapter does not provide a requested capability."""


class IKSolver(Protocol):
    kind: Literal["urdf", "native"]

    def forward_kinematics(
        self,
        state: RobotState,
    ) -> Mapping[str, CartesianPose]: ...

    def reset(self, sides: tuple[str, ...] = ("left", "right")) -> None: ...

    def solve(
        self,
        intent: EndEffectorPoseIntent,
        state: RobotState,
        *,
        sequence_id: int,
        now_ns: int,
        ttl_ms: int,
    ) -> JointCommand: ...


class RobotAdapter(ABC):
    """Uniform boundary around one robot or simulator."""

    def __init__(self, ik_solver: IKSolver | None = None) -> None:
        self._ik_solver = ik_solver

    def solve_ik(
        self,
        intent: EndEffectorPoseIntent,
        state: RobotState,
        *,
        sequence_id: int,
        now_ns: int,
        ttl_ms: int = 100,
    ) -> JointCommand:
        """Use the adapter-owned IK solver and return a standard joint command."""
        if self._ik_solver is None:
            raise UnsupportedCapabilityError("this adapter has no configured IK solver")
        return self._ik_solver.solve(
            intent,
            state,
            sequence_id=sequence_id,
            now_ns=now_ns,
            ttl_ms=ttl_ms,
        )

    @property
    def has_ik_solver(self) -> bool:
        return self._ik_solver is not None

    def get_end_effector_poses(
        self,
        state: RobotState,
    ) -> Mapping[str, CartesianPose]:
        """Return current left/right EE poses using this adapter's IK model."""
        if not isinstance(state, RobotState):
            raise TypeError("state must be a RobotState")
        forward = getattr(self._ik_solver, "forward_kinematics", None)
        if not callable(forward):
            raise UnsupportedCapabilityError(
                "this adapter cannot provide end-effector poses"
            )
        poses = dict(forward(state))
        if not poses or set(poses) - {"left", "right"}:
            raise ValueError("end-effector poses must contain left and/or right")
        if any(not isinstance(pose, CartesianPose) for pose in poses.values()):
            raise ValueError("end-effector poses must be CartesianPose values")
        return MappingProxyType(poses)

    def reset_ik(self, sides: tuple[str, ...] = ("left", "right")) -> None:
        """Clear adapter-owned IK integration after release or stale input."""
        sides = tuple(sides)
        if not sides or len(set(sides)) != len(sides):
            raise ValueError("IK reset sides must be unique and non-empty")
        if set(sides) - {"left", "right"}:
            raise ValueError("IK reset supports only left and right")
        reset = getattr(self._ik_solver, "reset", None)
        if not callable(reset):
            raise UnsupportedCapabilityError("this adapter cannot reset IK")
        reset(sides)

    @abstractmethod
    def get_capabilities(self) -> RobotCapabilities: ...

    @abstractmethod
    def get_joint_schema(self) -> JointSchema: ...

    def list_joints(self, *, commandable_only: bool = True) -> tuple[str, ...]:
        """Return the adapter-owned ordered joint menu."""
        if not isinstance(commandable_only, bool):
            raise ValueError("commandable_only must be bool")
        schema = self.get_joint_schema()
        return schema.commandable_names if commandable_only else schema.names

    def get_joint_info(self, name: str) -> JointInfo:
        """Return one joint's limits and control metadata."""
        return self.get_joint_schema().get(name)

    @abstractmethod
    def read_state(self, *, now_ns: int | None = None) -> RobotState: ...

    def get_safety_reference_state(
        self,
        state: RobotState,
        *,
        now_ns: int,
    ) -> RobotState:
        """Return the state used for one command's incremental safety checks.

        Most adapters use fresh telemetry directly. A streaming-servo adapter may
        override this with its last accepted target, provided it also bounds the
        target-to-telemetry tracking error.
        """
        if not isinstance(state, RobotState):
            raise TypeError("state must be a RobotState")
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a non-negative integer")
        return state

    @abstractmethod
    def send_joint_command(
        self,
        command: ValidatedJointCommand,
        *,
        now_ns: int | None = None,
    ) -> None: ...

    def send_gripper_command(
        self,
        command: ValidatedGripperCommand,
        *,
        now_ns: int | None = None,
    ) -> None:
        """Send normalized left/right gripper targets when supported."""
        raise UnsupportedCapabilityError("this adapter has no gripper control")

    def read_gripper_state(self) -> Mapping[str, float]:
        """Return normalized left/right closure fractions when supported."""
        raise UnsupportedCapabilityError("this adapter has no gripper state feedback")

    @abstractmethod
    def stop(self) -> None: ...
