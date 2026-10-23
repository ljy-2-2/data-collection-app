"""Replay recorded XR frames through upper-body IK without robot output."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
import time
from types import MappingProxyType

from teleop.control.dual_arm_live import AxisAlignedEndEffectorWorkspace
from teleop.control.router import ControlRouter
from teleop.control.safety import SafetyError, SafetyGateway
from teleop.contracts.intent import EndEffectorPoseIntent
from teleop.contracts.robot_state import RobotState
from teleop.contracts.xr import XRFrame
from teleop.retargeting.dual_arm import DualArmRetargeter
from teleop.robots.base import RobotAdapter


@dataclass(frozen=True)
class UpperBodyReplaySummary:
    """Aggregate evidence from one deterministic offline replay."""

    frames_read: int
    frames_solved: int
    stale_frames: int
    idle_frames: int
    safety_rejections: int
    workspace_clamps: int
    max_body_deviation_rad: float
    max_body_step_rad: float
    max_arm_step_rad: float
    min_joint_limit_margin_rad: float
    final_translation_error_m: Mapping[str, float]
    mean_solve_ms: float
    p95_solve_ms: float
    max_solve_ms: float
    physical_robot_command_sent: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "final_translation_error_m",
            MappingProxyType(dict(self.final_translation_error_m)),
        )


def _percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = math.ceil(0.95 * len(ordered)) - 1
    return ordered[max(0, index)]


class UpperBodyReplayEvaluator:
    """Evaluate XR -> retargeting -> IK -> safety using in-memory state only."""

    _SIDES = ("left", "right")

    def __init__(
        self,
        *,
        robot: RobotAdapter,
        router: ControlRouter,
        safety: SafetyGateway,
        retargeter: DualArmRetargeter,
        workspace: AxisAlignedEndEffectorWorkspace,
        control_period_s: float,
        clock_ns=time.perf_counter_ns,
    ) -> None:
        if not isinstance(robot, RobotAdapter):
            raise TypeError("robot must be a RobotAdapter")
        if not isinstance(router, ControlRouter):
            raise TypeError("router must be a ControlRouter")
        if not isinstance(safety, SafetyGateway):
            raise TypeError("safety must be a SafetyGateway")
        if not isinstance(retargeter, DualArmRetargeter):
            raise TypeError("retargeter must be a DualArmRetargeter")
        if not isinstance(workspace, AxisAlignedEndEffectorWorkspace):
            raise TypeError("workspace must be an AxisAlignedEndEffectorWorkspace")
        if (
            isinstance(control_period_s, bool)
            or not isinstance(control_period_s, (int, float))
            or not math.isfinite(control_period_s)
            or control_period_s <= 0
        ):
            raise ValueError("control_period_s must be finite and positive")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        if robot.get_capabilities().physical_joint_commands:
            raise ValueError("offline replay refuses a physical-command RobotAdapter")
        if not robot.has_ik_solver:
            raise ValueError("offline replay requires an IK-enabled RobotAdapter")

        self._robot = robot
        self._router = router
        self._safety = safety
        self._retargeter = retargeter
        self._workspace = workspace
        self._period_ns = int(float(control_period_s) * 1_000_000_000)
        if self._period_ns <= 0:
            raise ValueError("control_period_s must be at least one nanosecond")
        self._clock_ns = clock_ns

    def _validate_initial_state(self, state: RobotState) -> None:
        if not isinstance(state, RobotState):
            raise TypeError("initial_state must be a RobotState")
        schema = self._robot.get_joint_schema()
        if set(state.positions) != set(schema.names):
            raise ValueError("initial state joints must exactly match the robot schema")
        for info in schema.joints:
            value = state.positions[info.name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not info.position_min <= value <= info.position_max
            ):
                raise ValueError(
                    f"initial state joint {info.name} is invalid or outside limits"
                )

    def run(
        self,
        frames: Iterable[XRFrame],
        *,
        initial_state: RobotState,
    ) -> UpperBodyReplaySummary:
        """Replay frames at the configured logical rate; never call robot send."""

        self._validate_initial_state(initial_state)
        schema = self._robot.get_joint_schema()
        initial_positions = {
            name: float(value) for name, value in initial_state.positions.items()
        }
        state = RobotState(initial_positions, 0)
        body_names = tuple(
            name for name in schema.names if schema.get(name).group == "body"
        )
        arm_names = tuple(
            name
            for name in schema.names
            if schema.get(name).group in {"left_arm", "right_arm"}
        )

        frames_read = 0
        frames_solved = 0
        stale_frames = 0
        idle_frames = 0
        safety_rejections = 0
        workspace_clamps = 0
        max_body_deviation = 0.0
        max_body_step = 0.0
        max_arm_step = 0.0
        min_limit_margin = math.inf
        solve_times_ms: list[float] = []
        final_errors: dict[str, float] = {}
        previous_sequence = None
        previous_device_timestamp = None
        active_sides: tuple[str, ...] = ()

        self._retargeter.declutch()
        self._robot.reset_ik(self._SIDES)
        try:
            for frame in frames:
                if not isinstance(frame, XRFrame):
                    raise TypeError("replay frames must be XRFrame values")
                if previous_sequence is not None and frame.sequence_id <= previous_sequence:
                    raise ValueError("XR frame sequence must be strictly increasing")
                if (
                    previous_device_timestamp is not None
                    and frame.device_timestamp_ns <= previous_device_timestamp
                ):
                    raise ValueError(
                        "XR frame device timestamp must be strictly increasing"
                    )
                previous_sequence = frame.sequence_id
                previous_device_timestamp = frame.device_timestamp_ns
                frames_read += 1
                now_ns = frames_read * self._period_ns

                if frame.stale:
                    stale_frames += 1
                    self._retargeter.update(frame, {})
                    self._robot.reset_ik(self._SIDES)
                    active_sides = ()
                    continue

                anchors = None

                def get_anchor(side: str):
                    nonlocal anchors
                    if anchors is None:
                        anchors = self._robot.get_end_effector_poses(state)
                    return anchors.get(side)

                retargeting = self._retargeter.update(frame, get_anchor)
                next_active_sides = (
                    tuple(retargeting.intent.targets)
                    if retargeting.intent is not None
                    else ()
                )
                released_sides = tuple(
                    side for side in active_sides if side not in next_active_sides
                )
                if released_sides:
                    self._robot.reset_ik(released_sides)
                active_sides = next_active_sides
                if retargeting.intent is None:
                    idle_frames += 1
                    continue

                constrained, clamps = self._workspace.constrain(
                    retargeting.intent.targets
                )
                workspace_clamps += len(clamps)
                intent = EndEffectorPoseIntent(
                    constrained,
                    source=retargeting.intent.source,
                )
                solve_started = self._clock_ns()
                command = self._router.route(
                    intent,
                    state,
                    now_ns=now_ns,
                    robot=self._robot,
                )
                solve_finished = self._clock_ns()
                solve_times_ms.append((solve_finished - solve_started) / 1_000_000)
                try:
                    validated = self._safety.validate(
                        command,
                        state,
                        schema,
                        now_ns=now_ns,
                    )
                except SafetyError:
                    safety_rejections += 1
                    self._robot.reset_ik(active_sides)
                    continue

                previous_positions = state.positions
                updated_positions = dict(previous_positions)
                updated_positions.update(validated.targets)
                state = RobotState(updated_positions, now_ns)
                frames_solved += 1

                for name in body_names:
                    max_body_deviation = max(
                        max_body_deviation,
                        abs(state.positions[name] - initial_positions[name]),
                    )
                    if name in validated.targets:
                        max_body_step = max(
                            max_body_step,
                            abs(validated.targets[name] - previous_positions[name]),
                        )
                for name in arm_names:
                    if name in validated.targets:
                        max_arm_step = max(
                            max_arm_step,
                            abs(validated.targets[name] - previous_positions[name]),
                        )
                for info in schema.joints:
                    position = state.positions[info.name]
                    min_limit_margin = min(
                        min_limit_margin,
                        position - info.position_min,
                        info.position_max - position,
                    )

                actual_poses = self._robot.get_end_effector_poses(state)
                for side, target in constrained.items():
                    actual = actual_poses[side]
                    final_errors[side] = math.sqrt(
                        sum(
                            (observed - requested) ** 2
                            for observed, requested in zip(
                                actual.position_m,
                                target.position_m,
                                strict=True,
                            )
                        )
                    )
        finally:
            self._retargeter.declutch()
            self._robot.reset_ik(self._SIDES)

        if math.isinf(min_limit_margin):
            min_limit_margin = min(
                min(
                    initial_positions[info.name] - info.position_min,
                    info.position_max - initial_positions[info.name],
                )
                for info in schema.joints
            )
        mean_solve_ms = (
            sum(solve_times_ms) / len(solve_times_ms) if solve_times_ms else 0.0
        )
        return UpperBodyReplaySummary(
            frames_read=frames_read,
            frames_solved=frames_solved,
            stale_frames=stale_frames,
            idle_frames=idle_frames,
            safety_rejections=safety_rejections,
            workspace_clamps=workspace_clamps,
            max_body_deviation_rad=max_body_deviation,
            max_body_step_rad=max_body_step,
            max_arm_step_rad=max_arm_step,
            min_joint_limit_margin_rad=min_limit_margin,
            final_translation_error_m=final_errors,
            mean_solve_ms=mean_solve_ms,
            p95_solve_ms=_percentile_95(solve_times_ms),
            max_solve_ms=max(solve_times_ms, default=0.0),
        )
