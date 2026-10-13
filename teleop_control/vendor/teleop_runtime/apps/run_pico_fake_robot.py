import argparse
from dataclasses import dataclass
import math
import sys
import time

from teleop.backends.direct_joint import DirectJointBackend
from teleop.control.direct_joint_pipeline import (
    DirectJointExecution,
    DirectJointPipeline,
)
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyError, SafetyGateway
from teleop.contracts.schema import JointSchema
from teleop.contracts.xr import XRFrame
from teleop.retargeting.joint_jog import (
    JointJogMapper,
    JointJogStatus,
    TeleopInputMode,
)
from teleop.robots.fake import FakeRobot
from teleop.robots.g2 import make_g2_joint_schema
from teleop.xr import XRInputError, XRoboToolkitInputAdapter


@dataclass(frozen=True)
class PicoFakeFrameResult:
    mapping: JointJogStatus
    execution: DirectJointExecution | None
    rejection_reason: str | None


def positive_finite_float(value: str) -> float:
    try:
        converted = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(converted) or converted <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return converted


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run PICO joint-jog intents through the complete FakeRobot path."
    )
    parser.add_argument(
        "--mode",
        choices=(TeleopInputMode.OBSERVE.value, TeleopInputMode.JOINT_JOG.value),
        default=TeleopInputMode.OBSERVE.value,
        help="joint-jog explicitly enables simulated state updates",
    )
    parser.add_argument("--duration", type=positive_finite_float, default=10.0)
    parser.add_argument("--hz", type=positive_finite_float, default=50.0)
    return parser


def make_safe_fake_positions(
    schema: JointSchema,
    *,
    step_rad: float,
) -> dict[str, float]:
    if not isinstance(schema, JointSchema):
        raise TypeError("schema must be a JointSchema")
    if (
        isinstance(step_rad, bool)
        or not isinstance(step_rad, (int, float))
        or not math.isfinite(step_rad)
        or step_rad <= 0
    ):
        raise ValueError("step_rad must be finite and positive")

    positions = {}
    for joint in schema.joints:
        lower = joint.position_min + step_rad
        upper = joint.position_max - step_rad
        if lower > upper:
            raise ValueError(
                f"joint {joint.name} has no interior position for a {step_rad} rad jog"
            )
        if lower < 0.0 < upper:
            positions[joint.name] = 0.0
        else:
            positions[joint.name] = (joint.position_min + joint.position_max) / 2.0
    return positions


def process_frame(
    *,
    robot: FakeRobot,
    mapper: JointJogMapper,
    pipeline: DirectJointPipeline,
    frame: XRFrame,
    mode: TeleopInputMode,
    now_ns: int,
) -> PicoFakeFrameResult:
    if pipeline.robot is not robot:
        raise ValueError("pipeline and FakeRobot must refer to the same adapter")
    robot.read_state(now_ns=now_ns)
    mapping = mapper.update(frame, mode)
    execution = None
    rejection_reason = None
    if mapping.intent is not None:
        try:
            execution = pipeline.execute(mapping.intent, now_ns=now_ns)
        except SafetyError as error:
            rejection_reason = str(error)
    return PicoFakeFrameResult(
        mapping=mapping,
        execution=execution,
        rejection_reason=rejection_reason,
    )


def format_execution(execution: DirectJointExecution) -> str:
    updates = []
    for name, target in execution.validated.targets.items():
        updates.append(
            f"joint={name} before={execution.before.positions[name]:+.3f} "
            f"target={target:+.3f} after={execution.after.positions[name]:+.3f}"
        )
    return (
        "FAKE_COMMAND_APPLIED "
        + " ".join(updates)
        + " fake_robot_command_applied=True physical_robot_command_sent=False"
    )


def run_session(
    args: argparse.Namespace,
    *,
    adapter: XRoboToolkitInputAdapter,
    robot: FakeRobot | None = None,
) -> int:
    mode = TeleopInputMode(args.mode)
    step_rad = 0.01
    if robot is None:
        schema = make_g2_joint_schema()
        robot = FakeRobot(
            schema,
            initial_positions=make_safe_fake_positions(schema, step_rad=step_rad),
        )
    elif not isinstance(robot, FakeRobot):
        raise TypeError("robot must be a FakeRobot")
    else:
        schema = robot.get_joint_schema()
    mapper = JointJogMapper(robot.get_joint_schema(), step_rad=step_rad)
    pipeline = DirectJointPipeline(
        robot=robot,
        router=ControlRouter(DirectJointBackend()),
        safety=SafetyGateway(),
    )
    frame_count = 0
    intent_count = 0
    applied_count = 0
    rejected_count = 0
    last_frame_stale = False
    exit_code = 0

    print(
        f"FAKE_PIPELINE_PRECHECK mode={mode.value} adapter=FakeRobot "
        "physical_robot_command_sent=False",
        flush=True,
    )
    if mode is TeleopInputMode.JOINT_JOG:
        print(
            "X/A select a simulated joint; hold right Grip and deflect the right "
            "stick to update FakeRobot only.",
            flush=True,
        )

    try:
        adapter.start()
        deadline = time.monotonic() + args.duration
        sample_period = 1.0 / args.hz
        while time.monotonic() < deadline:
            frame = adapter.read_frame()
            if frame is not None:
                frame_count += 1
                last_frame_stale = frame.stale
                result = process_frame(
                    robot=robot,
                    mapper=mapper,
                    pipeline=pipeline,
                    frame=frame,
                    mode=mode,
                    now_ns=time.monotonic_ns(),
                )
                status = result.mapping
                if status.intent is not None:
                    intent_count += 1
                if result.rejection_reason is not None:
                    rejected_count += 1
                    print(
                        f"FAKE_COMMAND_REJECTED reason={result.rejection_reason} "
                        "fake_robot_state_changed=False "
                        "physical_robot_command_sent=False",
                        flush=True,
                    )
                else:
                    if status.reason in ("selected_previous", "selected_next"):
                        print(
                            f"FAKE_SELECTED joint={status.selected_joint} "
                            "physical_robot_command_sent=False",
                            flush=True,
                        )
                    if result.execution is not None:
                        applied_count += 1
                        print(format_execution(result.execution), flush=True)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(sample_period, remaining))
    except XRInputError as error:
        print(
            f"FAKE_PIPELINE_XR_ERROR: {error} physical_robot_command_sent=False",
            file=sys.stderr,
            flush=True,
        )
        exit_code = 2
    finally:
        try:
            adapter.close()
        except XRInputError as error:
            print(
                f"FAKE_PIPELINE_CLOSE_ERROR: {error} "
                "physical_robot_command_sent=False",
                file=sys.stderr,
                flush=True,
            )
            exit_code = 2
        robot.stop()

    print(
        f"FAKE_PIPELINE_SUMMARY frames={frame_count} intents={intent_count} "
        f"applied={applied_count} rejected={rejected_count} mode={mode.value} "
        "physical_robot_command_sent=False",
        flush=True,
    )
    if exit_code != 0:
        return exit_code
    if frame_count == 0:
        print(
            "FAKE_PIPELINE_FAILED: no valid XR frame was received "
            "physical_robot_command_sent=False",
            flush=True,
        )
        return 1
    if last_frame_stale:
        print(
            "FAKE_PIPELINE_FAILED: the final XR frame was stale "
            "physical_robot_command_sent=False",
            flush=True,
        )
        return 1
    return 0


def main() -> int:
    args = create_parser().parse_args()
    return run_session(args, adapter=XRoboToolkitInputAdapter())


if __name__ == "__main__":
    raise SystemExit(main())
