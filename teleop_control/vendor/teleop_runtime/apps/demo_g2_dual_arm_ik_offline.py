#!/usr/bin/env python3
"""Exercise the migrated dual-arm pose route without XR, GDK, or robot I/O."""

import time

from teleop.backends.direct_joint import DirectJointBackend
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.contracts.xr import ControllerButtons, ControllerState, Pose, XRFrame
from teleop.retargeting.dual_arm import DualArmRetargeter
from teleop.robots.g2 import G2Adapter, make_g2_joint_schema
from teleop.robots.g2_urdf_ik import G2URDFIKSolver


def _controller(x: float, z: float) -> ControllerState:
    return ControllerState(
        pose=Pose((x, 1.2, z), (0.0, 0.0, 0.0, 1.0)),
        axis=(0.0, 0.0),
        grip=0.96,
        trigger=0.0,
        buttons=ControllerButtons(),
    )


def _frame(sequence_id: int, *, z: float, now_ns: int) -> XRFrame:
    return XRFrame(
        sequence_id=sequence_id,
        device_timestamp_ns=sequence_id,
        received_monotonic_ns=now_ns,
        stale=False,
        head_pose=Pose((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
        left_controller=_controller(-0.2, z),
        right_controller=_controller(0.2, z),
        source="offline_demo",
    )


def main() -> int:
    now_ns = time.monotonic_ns()
    schema = make_g2_joint_schema(max_step_rad=0.02)
    solver = G2URDFIKSolver(
        joint_schema=schema,
        max_joint_velocity_rad_s=0.2,
        control_period_s=0.01,
    )
    robot = G2Adapter(
        initial_positions={name: 0.0 for name in schema.names},
        ik_solver=solver,
    )
    state = robot.read_state(now_ns=now_ns)
    anchors = solver.forward_kinematics(state)
    retargeter = DualArmRetargeter(scale=1.0)
    retargeter.update(_frame(1, z=-0.30, now_ns=now_ns), anchors)
    result = retargeter.update(_frame(2, z=-0.31, now_ns=now_ns), anchors)
    if result.intent is None:
        raise RuntimeError("dual-arm retargeter did not emit an intent")

    command = ControlRouter(DirectJointBackend()).route(
        result.intent,
        state,
        now_ns=now_ns,
        robot=robot,
    )
    validated = SafetyGateway().validate(
        command,
        state,
        schema,
        now_ns=now_ns,
    )
    robot.send_joint_command(validated, now_ns=now_ns)
    preview = robot.last_request
    print(
        "G2_DUAL_ARM_IK_OFFLINE "
        f"sides={','.join(result.intent.targets)} "
        f"joint_targets={len(validated.targets)} "
        f"request_joints={len(preview.joint_names)} "
        "safety_validated=True physical_robot_command_sent=False"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
