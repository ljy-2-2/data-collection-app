"""Robot adapters."""

from .base import RobotAdapter, UnsupportedCapabilityError
from .fake import FakeRobot
from .g2 import (
    G2Adapter,
    G2JointControlRequestPreview,
    G2StateReadError,
    make_g2_joint_schema,
)

__all__ = [
    "FakeRobot",
    "G2Adapter",
    "G2JointControlRequestPreview",
    "G2StateReadError",
    "RobotAdapter",
    "UnsupportedCapabilityError",
    "make_g2_joint_schema",
]
