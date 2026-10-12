import time

from teleop.contracts.capabilities import AdapterOperatingMode, RobotCapabilities
from teleop.contracts.command import ValidatedJointCommand
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointSchema
from teleop.robots.base import RobotAdapter


class FakeRobot(RobotAdapter):
    """In-memory adapter used to verify the core without physical hardware."""

    def __init__(
        self,
        schema: JointSchema,
        initial_positions: dict[str, float] | None = None,
    ) -> None:
        super().__init__(ik_solver=None)
        self._schema = schema
        self._positions = {joint.name: 0.0 for joint in schema.joints}
        if initial_positions:
            unknown = set(initial_positions) - set(self._positions)
            if unknown:
                raise ValueError(f"unknown initial joints: {sorted(unknown)}")
            self._positions.update(initial_positions)
        self._timestamp_ns = time.monotonic_ns()
        self._stopped = False

    def get_capabilities(self) -> RobotCapabilities:
        return RobotCapabilities(
            operating_mode=AdapterOperatingMode.SIMULATION,
            joint_state_read=True,
            joint_position_control=True,
            supports_partial_joint_command=True,
            physical_joint_commands=False,
            urdf_ik=False,
            native_ik=False,
            mobile_base_control=False,
            end_effector_control=False,
        )

    def get_joint_schema(self) -> JointSchema:
        return self._schema

    def read_state(self, *, now_ns: int | None = None) -> RobotState:
        if now_ns is not None:
            self._timestamp_ns = now_ns
        return RobotState(self._positions, self._timestamp_ns)

    def send_joint_command(
        self,
        command: ValidatedJointCommand,
        *,
        now_ns: int | None = None,
    ) -> None:
        if not isinstance(command, ValidatedJointCommand):
            raise TypeError("FakeRobot accepts only ValidatedJointCommand")
        if self._stopped:
            raise RuntimeError("FakeRobot is stopped")
        unknown = set(command.targets) - set(self._positions)
        if unknown:
            raise ValueError(f"unknown command joints: {sorted(unknown)}")
        self._positions.update(command.targets)
        self._timestamp_ns = now_ns if now_ns is not None else time.monotonic_ns()

    def stop(self) -> None:
        self._stopped = True
