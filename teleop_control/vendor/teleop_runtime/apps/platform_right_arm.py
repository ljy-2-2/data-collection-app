"""Single-owner right-arm adapter shared by DAgger and HIL-SERL.

This module keeps their orchestration protocols intact while routing expert
teleoperation through the same runtime core as ordinary teleoperation.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from teleop.backends.direct_joint import DirectJointBackend
from teleop.contracts.intent import CartesianPose
from teleop.contracts.xr import ControllerButtons, ControllerState, XRFrame
from teleop.control.dual_arm_live import AxisAlignedEndEffectorWorkspace, DualArmLivePipeline
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.retargeting.dual_arm import DualArmRetargeter
from teleop.robots.g2 import (
    G2Adapter,
    G2GDKGripperCommandSender,
    G2GDKJointServoCommandSender,
    make_g2_joint_schema,
)
from teleop.robots.g2_urdf_ik import G2URDFIKSolver
from teleop.xr import XRoboToolkitInputAdapter

RIGHT_JOINTS = tuple(f"idx6{index}_arm_r_joint{index}" for index in range(1, 8))
RIGHT_GRIPPER = "idx71_gripper_r_inner_joint1"
GRIPPER_OPEN_RAD = -0.785
GRIPPER_CLOSED_RAD = 0.0
DEFAULT_URDF = Path(__file__).resolve().parents[1] / "assets" / "g2" / "G2_t2_crs.urdf"


def gripper_radians(fraction: float) -> float:
    return GRIPPER_OPEN_RAD + float(fraction) * (GRIPPER_CLOSED_RAD - GRIPPER_OPEN_RAD)


def frame_to_legacy(frame: XRFrame | None) -> dict | None:
    """Expose the old dictionary shape only to unchanged mode state machines."""
    if frame is None:
        return None
    left = frame.left_controller
    right = frame.right_controller
    return {
        "stale": frame.stale,
        "head": list(frame.head_pose.xyz_xyzw),
        "left": {
            "pose": list(left.pose.xyz_xyzw),
            "grip": left.grip,
            "trigger": left.trigger,
        },
        "right": {
            "pose": list(right.pose.xyz_xyzw),
            "grip": right.grip,
            "trigger": right.trigger,
        },
        "buttons": {
            "A": right.buttons.primary,
            "B": right.buttons.secondary,
            "X": left.buttons.primary,
            "Y": left.buttons.secondary,
        },
        "_xr_frame": frame,
    }


class LegacyXRInputAdapter:
    """Compatibility surface backed by the new strict XRFrame adapter."""

    def __init__(self) -> None:
        self._adapter = XRoboToolkitInputAdapter()

    def init(self) -> None:
        self._adapter.start()

    def read_frame(self) -> XRFrame | None:
        return self._adapter.read_frame()

    def read(self) -> dict | None:
        return frame_to_legacy(self.read_frame())

    def close(self) -> None:
        self._adapter.close()


def legacy_to_frame(sample: dict | None) -> XRFrame | None:
    if sample is None:
        return None
    frame = sample.get("_xr_frame")
    if not isinstance(frame, XRFrame):
        raise ValueError("sample was not produced by LegacyXRInputAdapter")
    return frame


def _right_only(frame: XRFrame) -> XRFrame:
    left = frame.left_controller
    neutral_left = ControllerState(
        pose=left.pose,
        axis=left.axis,
        grip=0.0,
        trigger=0.0,
        buttons=ControllerButtons(),
    )
    return replace(frame, left_controller=neutral_left)


class RightArmTeleopSession:
    """Right-arm view of the production dual-arm runtime pipeline."""

    def __init__(
        self,
        *,
        gdk: object,
        robot: object,
        scale: float = 0.5,
        control_hz: float = 100.0,
        max_joint_velocity_rad_s: float = 0.5,
        urdf: str | Path = DEFAULT_URDF,
        target_validator: Callable[[str, CartesianPose], None] | None = None,
        suppress_gripper_on_arm_rejection: bool = False,
    ) -> None:
        max_step_rad = max_joint_velocity_rad_s / control_hz
        if max_step_rad > 0.02:
            raise ValueError("right-arm velocity/control rate exceeds the 0.02 rad step limit")
        schema = make_g2_joint_schema(
            max_step_rad=max_step_rad,
            arm_velocity_rad_s=max_joint_velocity_rad_s,
        )
        solver = G2URDFIKSolver(
            urdf_path=Path(urdf),
            joint_schema=schema,
            max_joint_velocity_rad_s=max_joint_velocity_rad_s,
            control_period_s=1.0 / control_hz,
        )
        self.adapter = G2Adapter(
            dry_run=False,
            state_reader=robot,
            command_sender=G2GDKJointServoCommandSender(
                gdk=gdk,
                robot=robot,
                control_period_s=1.2 / control_hz,
                max_arm_velocity_rad_s=max_joint_velocity_rad_s,
            ),
            gripper_sender=G2GDKGripperCommandSender(gdk=gdk, robot=robot),
            allow_live_commands=True,
            max_step_rad=max_step_rad,
            default_velocity_rad_s=max_joint_velocity_rad_s,
            live_arm_velocity_cap_rad_s=max_joint_velocity_rad_s,
            continuous_servo_commands=True,
            ik_solver=solver,
        )
        self.pipeline = DualArmLivePipeline(
            robot=self.adapter,
            router=ControlRouter(DirectJointBackend(default_ttl_ms=100)),
            safety=SafetyGateway(max_state_age_ms=200),
            retargeter=DualArmRetargeter(scale=scale),
            workspace=AxisAlignedEndEffectorWorkspace.g2_conservative(),
            target_validator=target_validator,
            suppress_gripper_on_arm_rejection=suppress_gripper_on_arm_rejection,
            telemetry_period_s=0.1,
        )

    def reset(self) -> None:
        self.pipeline.reset()

    def declutch(self) -> None:
        """Compatibility name used by the unchanged DAgger/HIL mode machines."""
        self.reset()

    @staticmethod
    def _fresh(frame: XRFrame, now_ns: int) -> int:
        if frame.stale:
            raise ValueError("XR frame is stale")
        if now_ns < frame.received_monotonic_ns:
            raise ValueError("XR frame timestamp is in the future")
        if now_ns - frame.received_monotonic_ns > 300_000_000:
            raise ValueError("XR frame expired before robot send")
        return now_ns

    def execute(self, frame: XRFrame):
        frame = _right_only(frame)
        now_ns = time.monotonic_ns()
        self._fresh(frame, now_ns)
        return self.pipeline.execute(
            frame,
            now_ns=now_ns,
            before_send=lambda: self._fresh(frame, time.monotonic_ns()),
        )

    def snapshot_action(self) -> list[float]:
        state = self.adapter.read_state(now_ns=time.monotonic_ns())
        grippers = self.adapter.read_gripper_state()
        return [
            *(float(state.positions[name]) for name in RIGHT_JOINTS),
            gripper_radians(grippers["right"]),
        ]

    @staticmethod
    def update_action(action: list[float], result: object) -> list[float]:
        updated = list(action)
        if result.validated is not None:
            for index, name in enumerate(RIGHT_JOINTS):
                if name in result.validated.targets:
                    updated[index] = float(result.validated.targets[name])
        if result.gripper_validated is not None and "right" in result.gripper_sides:
            updated[-1] = gripper_radians(result.gripper_validated.targets["right"])
        return updated

    def stop(self) -> None:
        self.pipeline.reset()
        self.adapter.stop()
