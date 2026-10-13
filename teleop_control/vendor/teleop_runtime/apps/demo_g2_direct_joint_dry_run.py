from teleop.backends.direct_joint import DirectJointBackend
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyGateway
from teleop.contracts.intent import JointJogIntent
from teleop.robots.g2 import G2Adapter


JOINT_NAME = "idx21_arm_l_joint1"


def main() -> None:
    now_ns = 3_000_000_000
    robot = G2Adapter(initial_positions={JOINT_NAME: 0.30})
    router = ControlRouter(DirectJointBackend(default_ttl_ms=100))
    safety = SafetyGateway()

    state = robot.read_state(now_ns=now_ns)
    intent = JointJogIntent(deltas={JOINT_NAME: 0.01}, source="pico_preview")
    command = router.route(intent, state, now_ns=now_ns)
    validated = safety.validate(
        command,
        state,
        robot.get_joint_schema(),
        now_ns=now_ns,
    )
    robot.send_joint_command(validated, now_ns=now_ns)
    request = robot.last_request
    if request is None:
        raise RuntimeError("dry-run request preview was not created")

    print("G2 DIRECT-JOINT DRY-RUN")
    print(f"available_joints={len(robot.get_joint_schema().commandable_names)}")
    print(f"selected_joint={JOINT_NAME}")
    print(f"current_position={state.positions[JOINT_NAME]:.2f} rad")
    print(f"requested_delta={intent.deltas[JOINT_NAME]:+.2f} rad")
    print(f"preview_joint_names={list(request.joint_names)}")
    print(f"preview_joint_positions={list(request.joint_positions)}")
    print(f"preview_joint_velocities={list(request.joint_velocities)}")
    print(f"preview_life_time={request.life_time_s:.2f} s")
    print("hardware_command_sent=False")


if __name__ == "__main__":
    main()
