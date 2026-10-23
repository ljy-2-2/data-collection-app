from dataclasses import dataclass
from enum import Enum


class AdapterOperatingMode(str, Enum):
    """How an adapter handles commands in the current process."""

    READ_ONLY = "read_only"
    PREVIEW = "preview"
    SIMULATION = "simulation"
    LIVE = "live"


@dataclass(frozen=True)
class RobotCapabilities:
    """Capabilities that the generic core may rely on."""

    operating_mode: AdapterOperatingMode = AdapterOperatingMode.READ_ONLY
    joint_state_read: bool = False
    joint_position_control: bool = False
    supports_partial_joint_command: bool = False
    physical_joint_commands: bool = False
    urdf_ik: bool = False
    native_ik: bool = False
    mobile_base_control: bool = False
    end_effector_control: bool = False
    gripper_state_read: bool = False
    gripper_control: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.operating_mode, AdapterOperatingMode):
            raise ValueError("operating_mode must be AdapterOperatingMode")
        for name, value in vars(self).items():
            if name == "operating_mode":
                continue
            if not isinstance(value, bool):
                raise ValueError(f"capability {name} must be bool")
        if self.supports_partial_joint_command and not self.joint_position_control:
            raise ValueError(
                "partial joint commands require joint position control"
            )
        if self.physical_joint_commands and not self.joint_position_control:
            raise ValueError(
                "physical joint commands require joint position control"
            )
        if self.physical_joint_commands and self.operating_mode is not AdapterOperatingMode.LIVE:
            raise ValueError("physical joint commands require live operating mode")
        if self.operating_mode is AdapterOperatingMode.LIVE and not self.physical_joint_commands:
            raise ValueError("live operating mode requires physical joint commands")
        if self.operating_mode is AdapterOperatingMode.READ_ONLY and self.joint_position_control:
            raise ValueError("read-only operating mode cannot control joints")

    @property
    def supports_ik(self) -> bool:
        """Whether this adapter currently has any usable IK implementation."""
        return self.urdf_ik or self.native_ik
