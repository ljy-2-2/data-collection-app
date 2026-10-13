from teleop.backends.direct_joint import DirectJointBackend
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyError, SafetyGateway
from teleop.contracts.intent import JointJogIntent, JointPositionIntent
from teleop.contracts.schema import JointInfo, JointSchema
from teleop.robots.fake import FakeRobot


def main() -> None:
    now_ns = 1_000_000_000
    schema = JointSchema(
        joints=(
            JointInfo("left_arm.j1", -1.0, 1.0, max_step=0.10),
            JointInfo("left_arm.j2", -1.0, 1.0, max_step=0.10),
            JointInfo("right_arm.j1", -1.0, 1.0, max_step=0.10),
        )
    )
    robot = FakeRobot(schema)
    router = ControlRouter(DirectJointBackend())
    safety = SafetyGateway()

    before = robot.read_state(now_ns=now_ns)
    intent = JointJogIntent(deltas={"left_arm.j1": 0.01})
    command = router.route(intent, before, now_ns=now_ns)
    validated = safety.validate(command, before, schema, now_ns=now_ns)
    robot.send_joint_command(validated, now_ns=now_ns)

    print("VALID command accepted")
    print(f"before left_arm.j1={before.positions['left_arm.j1']:.2f}")
    print(f"after  left_arm.j1={robot.read_state().positions['left_arm.j1']:.2f}")

    state = robot.read_state(now_ns=now_ns + 10_000_000)
    unsafe = router.route(
        JointPositionIntent(targets={"left_arm.j1": 2.0}),
        state,
        now_ns=now_ns + 10_000_000,
    )
    try:
        safety.validate(unsafe, state, schema, now_ns=now_ns + 10_000_000)
    except SafetyError as error:
        print(f"UNSAFE command rejected: {error}")


if __name__ == "__main__":
    main()
