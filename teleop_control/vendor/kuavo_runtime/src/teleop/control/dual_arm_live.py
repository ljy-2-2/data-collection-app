"""Live dual-arm pipeline with a final freshness gate before robot output."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math
import time
from types import MappingProxyType
from typing import Literal

from teleop.control.router import ControlRouter
from teleop.control.gripper_safety import GripperSafetyGateway
from teleop.control.safety import SafetyGateway
from teleop.contracts.command import (
    GripperCommand,
    JointCommand,
    ValidatedGripperCommand,
    ValidatedJointCommand,
)
from teleop.contracts.intent import CartesianPose, EndEffectorPoseIntent, GripperIntent
from teleop.contracts.robot_state import RobotState
from teleop.contracts.xr import XRFrame
from teleop.retargeting.dual_arm import (
    ENGAGE_THRESHOLD,
    RELEASE_THRESHOLD,
    DualArmRetargeter,
    DualArmRetargetingResult,
)
from teleop.retargeting.gripper import GripperMapper, TRIGGER_NEUTRAL_THRESHOLD
from teleop.robots.base import RobotAdapter


class PartialPhysicalSendError(RuntimeError):
    """A later action failed after at least one physical call completed."""

    def __init__(self, completed: int, error: Exception) -> None:
        self.completed = completed
        self.original_error = error
        super().__init__(
            f"physical command sequence failed after completed={completed}: {error}"
        )


@dataclass(frozen=True)
class ArmTargetRejection:
    """One arm target rejected before any physical arm command was sent."""

    side: Literal["left", "right"]
    reason: str


@dataclass(frozen=True)
class WorkspaceClamp:
    """One Cartesian coordinate saturated at an adapter-selected safe bound."""

    side: Literal["left", "right"]
    axis: Literal["x", "y", "z"]
    requested: float
    clamped: float
    lower: float
    upper: float


@dataclass(frozen=True)
class GripperSendFailure:
    """One gripper lane failed without stopping either arm lane."""

    side: Literal["left", "right"]
    reason: str
    physical_outcome: Literal["not_sent", "unknown"]


@dataclass(frozen=True)
class DualArmCycleTimings:
    """Wall-clock timing of the synchronous stages in one control cycle."""

    state_read_ms: float = 0.0
    mapping_ms: float = 0.0
    arm_ms: float = 0.0
    gripper_ms: float = 0.0
    total_ms: float = 0.0


@dataclass(frozen=True)
class AxisAlignedEndEffectorWorkspace:
    """Small Cartesian safety box checked before IK and joint commands."""

    frame_id: str
    bounds_by_side: Mapping[
        str,
        tuple[
            tuple[float, float],
            tuple[float, float],
            tuple[float, float],
        ],
    ]

    def __post_init__(self) -> None:
        if not isinstance(self.frame_id, str) or not self.frame_id:
            raise ValueError("workspace frame_id must be a non-empty string")
        bounds = dict(self.bounds_by_side)
        if set(bounds) != {"left", "right"}:
            raise ValueError("workspace must define left and right bounds")
        for side, axes in bounds.items():
            if len(axes) != 3:
                raise ValueError(f"{side} workspace must define x/y/z bounds")
            for lower, upper in axes:
                if not all(math.isfinite(value) for value in (lower, upper)):
                    raise ValueError(f"{side} workspace bounds must be finite")
                if lower >= upper:
                    raise ValueError(f"{side} workspace lower bound must be below upper")
        object.__setattr__(self, "bounds_by_side", MappingProxyType(bounds))

    @classmethod
    def g2_conservative(cls) -> "AxisAlignedEndEffectorWorkspace":
        """Conservative G2 hand boxes in base_link, pending collision modelling."""
        return cls(
            frame_id="base_link",
            bounds_by_side={
                "left": ((0.10, 0.85), (0.05, 0.75), (0.35, 1.35)),
                "right": ((0.10, 0.85), (-0.75, -0.05), (0.35, 1.35)),
            },
        )

    def validate(self, targets: Mapping[str, CartesianPose]) -> None:
        unknown = set(targets) - {"left", "right"}
        if unknown:
            raise ValueError(f"unknown end-effector workspace sides: {sorted(unknown)}")
        for side, pose in targets.items():
            if not isinstance(pose, CartesianPose):
                raise TypeError(f"{side} target must be a CartesianPose")
            if pose.frame_id != self.frame_id:
                raise ValueError(
                    f"{side} target frame must be {self.frame_id!r} for workspace check"
                )
            axes = self.bounds_by_side[side]
            target_xyz = tuple(float(value) for value in pose.position_m)
            for axis_name, coordinate, (lower, upper) in zip(
                ("x", "y", "z"),
                target_xyz,
                axes,
                strict=True,
            ):
                if coordinate < lower or coordinate > upper:
                    formatted_target = ", ".join(
                        f"{value:.6f}" for value in target_xyz
                    )
                    raise ValueError(
                        f"{side} end-effector target is outside the configured "
                        f"workspace: {axis_name}={coordinate:.6f} not in "
                        f"[{lower:.6f}, {upper:.6f}] m; "
                        f"target_xyz=({formatted_target})"
                    )

    def constrain(
        self,
        targets: Mapping[str, CartesianPose],
    ) -> tuple[Mapping[str, CartesianPose], tuple[WorkspaceClamp, ...]]:
        """Saturate positions at safe bounds while preserving pose orientation."""
        unknown = set(targets) - {"left", "right"}
        if unknown:
            raise ValueError(f"unknown end-effector workspace sides: {sorted(unknown)}")
        constrained: dict[str, CartesianPose] = {}
        clamps: list[WorkspaceClamp] = []
        axis_names: tuple[Literal["x", "y", "z"], ...] = ("x", "y", "z")
        for side, pose in targets.items():
            if not isinstance(pose, CartesianPose):
                raise TypeError(f"{side} target must be a CartesianPose")
            if pose.frame_id != self.frame_id:
                raise ValueError(
                    f"{side} target frame must be {self.frame_id!r} for workspace check"
                )
            coordinates: list[float] = []
            for axis, requested, (lower, upper) in zip(
                axis_names,
                pose.position_m,
                self.bounds_by_side[side],
                strict=True,
            ):
                requested = float(requested)
                clamped = min(max(requested, lower), upper)
                coordinates.append(clamped)
                if clamped != requested:
                    clamps.append(
                        WorkspaceClamp(
                            side=side,
                            axis=axis,
                            requested=requested,
                            clamped=clamped,
                            lower=lower,
                            upper=upper,
                        )
                    )
            constrained[side] = CartesianPose(
                frame_id=pose.frame_id,
                position_m=tuple(coordinates),
                orientation_xyzw=pose.orientation_xyzw,
            )
        return MappingProxyType(constrained), tuple(clamps)


@dataclass(frozen=True)
class DualArmLiveResult:
    status: Literal["stale", "idle", "sent"]
    frame_sequence_id: int
    active_sides: tuple[str, ...]
    retargeting: DualArmRetargetingResult
    state: RobotState | None = None
    command: JointCommand | None = None
    validated: ValidatedJointCommand | None = None
    gripper_sides: tuple[str, ...] = ()
    gripper_command: GripperCommand | None = None
    gripper_validated: ValidatedGripperCommand | None = None
    arm_rejections: tuple[ArmTargetRejection, ...] = ()
    workspace_clamps: tuple[WorkspaceClamp, ...] = ()
    gripper_failures: tuple[GripperSendFailure, ...] = ()
    telemetry_refreshed: bool = False
    timings: DualArmCycleTimings = DualArmCycleTimings()


class DualArmLivePipeline:
    """Turn one current XR frame into at most one validated robot send."""

    _SIDES = ("left", "right")

    def __init__(
        self,
        *,
        robot: RobotAdapter,
        router: ControlRouter,
        safety: SafetyGateway,
        retargeter: DualArmRetargeter,
        workspace: AxisAlignedEndEffectorWorkspace,
        gripper_mapper: GripperMapper | None = None,
        gripper_safety: GripperSafetyGateway | None = None,
        telemetry_period_s: float = 0.1,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
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
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        if (
            isinstance(telemetry_period_s, bool)
            or not isinstance(telemetry_period_s, (int, float))
            or not math.isfinite(telemetry_period_s)
            or telemetry_period_s <= 0.0
        ):
            raise ValueError("telemetry_period_s must be finite and positive")
        capabilities = robot.get_capabilities()
        if not capabilities.physical_joint_commands:
            raise ValueError("live dual-arm pipeline requires an armed RobotAdapter")
        self._robot = robot
        self._router = router
        self._safety = safety
        self._retargeter = retargeter
        self._workspace = workspace
        self._gripper_mapper = gripper_mapper or GripperMapper()
        self._gripper_safety = gripper_safety or GripperSafetyGateway()
        self._telemetry_period_ns = int(telemetry_period_s * 1_000_000_000)
        if self._telemetry_period_ns <= 0:
            raise ValueError("telemetry_period_s must be at least one nanosecond")
        self._clock_ns = clock_ns
        self._gripper_enabled = (
            capabilities.gripper_state_read and capabilities.gripper_control
        )
        self._active_sides: tuple[str, ...] = ()
        self._blocked_grippers: set[str] = set()
        self._cached_state: RobotState | None = None
        self._last_state_attempt_ns: int | None = None
        self._last_state_error: Exception | None = None
        self._engage_refresh_attempted: set[str] = set()

    def reset(self) -> None:
        self.reset_arm_control()
        self._gripper_mapper.reset()
        self._blocked_grippers.clear()

    def reset_arm_control(self) -> None:
        """Re-anchor both arms without disturbing independent gripper lanes."""
        self._retargeter.declutch()
        self._robot.reset_ik(self._SIDES)
        self._active_sides = ()
        self._cached_state = None
        self._last_state_attempt_ns = None
        self._last_state_error = None
        self._engage_refresh_attempted.clear()

    def _read_control_state(
        self,
        frame: XRFrame,
        *,
        now_ns: int,
    ) -> tuple[RobotState, bool]:
        """Refresh telemetry at its own rate, and always on a new arm engage."""
        controllers = {
            "left": frame.left_controller,
            "right": frame.right_controller,
        }
        for side, controller in controllers.items():
            if controller.grip < RELEASE_THRESHOLD:
                self._engage_refresh_attempted.discard(side)

        newly_engaging_sides = tuple(
            side
            for side, controller in controllers.items()
            if side not in self._active_sides
            and side not in self._engage_refresh_attempted
            and controller.grip > ENGAGE_THRESHOLD
        )
        if newly_engaging_sides:
            # Count the attempt before calling the SDK. If that call fails, a held
            # Grip must not turn the next 100 Hz frame into another forced read.
            self._engage_refresh_attempted.update(newly_engaging_sides)

        if (
            self._last_state_attempt_ns is not None
            and now_ns < self._last_state_attempt_ns
        ):
            raise ValueError("control clock moved backwards since telemetry attempt")
        refresh_due = (
            self._last_state_attempt_ns is None
            or now_ns - self._last_state_attempt_ns >= self._telemetry_period_ns
        )
        if newly_engaging_sides or refresh_due:
            self._last_state_attempt_ns = now_ns
            try:
                state = self._robot.read_state(now_ns=now_ns)
            except Exception as error:
                self._last_state_error = error
                raise
            self._cached_state = state
            self._last_state_error = None
            return state, True

        if self._cached_state is not None:
            return self._cached_state, False
        if self._last_state_error is not None:
            raise self._last_state_error
        raise RuntimeError("telemetry cache is unavailable before its retry period")

    def execute(
        self,
        frame: XRFrame,
        *,
        now_ns: int,
        before_send: Callable[[], int],
    ) -> DualArmLiveResult:
        """Prepare, validate, recheck freshness, and send one command."""
        if not isinstance(frame, XRFrame):
            raise TypeError("frame must be an XRFrame")
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a non-negative integer")
        if not callable(before_send):
            raise TypeError("before_send must be callable")

        cycle_started_ns = self._clock_ns()

        if frame.stale:
            retargeting = self._retargeter.update(frame, {})
            # Stale input must immediately release arm integration, but it has
            # not made a physical gripper outcome unknown.  Preserve the two
            # independently mapped trigger lanes so a fresh frame can resume.
            self.reset_arm_control()
            return DualArmLiveResult(
                "stale", frame.sequence_id, (), retargeting
            )

        state_started_ns = self._clock_ns()
        state, telemetry_refreshed = self._read_control_state(
            frame,
            now_ns=now_ns,
        )
        state_finished_ns = self._clock_ns()
        anchors = None

        def get_anchor(side: str):
            nonlocal anchors
            if anchors is None:
                anchors = self._robot.get_end_effector_poses(state)
            return anchors.get(side)

        mapping_started_ns = self._clock_ns()
        retargeting = self._retargeter.update(frame, get_anchor)
        gripper_failures: list[GripperSendFailure] = []
        gripper_intent = None
        if self._gripper_enabled:
            if not self._gripper_mapper.initialized:
                try:
                    self._gripper_mapper.prime(self._robot.read_gripper_state())
                except Exception as error:
                    self._blocked_grippers.update(self._SIDES)
                    gripper_failures.extend(
                        GripperSendFailure(side, str(error), "not_sent")
                        for side in self._SIDES
                    )
            else:
                recovering = tuple(
                    side
                    for side in self._blocked_grippers
                    if retargeting.gripper_fractions[side]
                    <= TRIGGER_NEUTRAL_THRESHOLD
                )
                if recovering:
                    try:
                        feedback = self._robot.read_gripper_state()
                    except Exception as error:
                        gripper_failures.extend(
                            GripperSendFailure(side, str(error), "not_sent")
                            for side in recovering
                        )
                    else:
                        for side in recovering:
                            self._gripper_mapper.prime_side(side, feedback[side])
                            self._blocked_grippers.discard(side)
            if self._gripper_mapper.initialized:
                gripper_intent = self._gripper_mapper.propose(
                    retargeting.gripper_fractions,
                    now_ns=now_ns,
                )
        active_sides = (
            tuple(retargeting.intent.targets)
            if retargeting.intent is not None
            else ()
        )
        released_sides = tuple(
            side for side in self._active_sides if side not in active_sides
        )
        if released_sides:
            self._robot.reset_ik(released_sides)
        self._active_sides = active_sides

        arm_rejections: list[ArmTargetRejection] = []
        workspace_clamps: list[WorkspaceClamp] = []
        arm_intent = None
        if retargeting.intent is not None:
            accepted_targets = {}
            for side, target in retargeting.intent.targets.items():
                try:
                    constrained, clamps = self._workspace.constrain({side: target})
                except (TypeError, ValueError) as error:
                    arm_rejections.append(ArmTargetRejection(side, str(error)))
                    self._robot.reset_ik((side,))
                else:
                    accepted_targets[side] = constrained[side]
                    workspace_clamps.extend(clamps)
            if accepted_targets:
                arm_intent = EndEffectorPoseIntent(
                    accepted_targets,
                    source=retargeting.intent.source,
                )
        mapping_finished_ns = self._clock_ns()

        if arm_intent is None and gripper_intent is None:
            cycle_finished_ns = self._clock_ns()
            return DualArmLiveResult(
                "idle",
                frame.sequence_id,
                active_sides,
                retargeting,
                state=state,
                arm_rejections=tuple(arm_rejections),
                workspace_clamps=tuple(workspace_clamps),
                gripper_failures=tuple(gripper_failures),
                telemetry_refreshed=telemetry_refreshed,
                timings=DualArmCycleTimings(
                    state_read_ms=_milliseconds(
                        state_started_ns,
                        state_finished_ns,
                    ),
                    mapping_ms=_milliseconds(
                        mapping_started_ns,
                        mapping_finished_ns,
                    ),
                    total_ms=_milliseconds(cycle_started_ns, cycle_finished_ns),
                ),
            )

        command = None
        validated = None
        gripper_command = None
        gripper_validated = None
        command_state = state
        arm_sent = False
        arm_started_ns = self._clock_ns()
        try:
            if arm_intent is not None:
                command_state = self._robot.get_safety_reference_state(
                    state,
                    now_ns=now_ns,
                )
                routed = self._router.route(
                    arm_intent,
                    command_state,
                    now_ns=now_ns,
                    robot=self._robot,
                )
                if not isinstance(routed, JointCommand):
                    raise TypeError("end-effector route must produce JointCommand")
                command = routed
                validated = self._safety.validate(
                    command,
                    command_state,
                    self._robot.get_joint_schema(),
                    now_ns=now_ns,
                )
                send_now_ns = _before_send_time(before_send)
                self._robot.send_joint_command(validated, now_ns=send_now_ns)
                arm_sent = True
        except Exception:
            # Clear integrated targets before the worker classifies the failure.
            # Pre-send rejections may resume automatically; an unknown vendor
            # outcome still activates the worker's conservative manual rearm.
            self.reset_arm_control()
            raise
        arm_finished_ns = self._clock_ns()

        gripper_started_ns = self._clock_ns()
        if gripper_intent is not None:
            try:
                routed = self._router.route(
                    gripper_intent,
                    state,
                    now_ns=now_ns,
                    robot=self._robot,
                )
                if not isinstance(routed, GripperCommand):
                    raise TypeError("gripper route must produce GripperCommand")
                gripper_command = routed
                gripper_validated = self._gripper_safety.validate(
                    gripper_command,
                    now_ns=now_ns,
                )
            except Exception as error:
                gripper_failures.extend(
                    GripperSendFailure(side, str(error), "not_sent")
                    for side in gripper_intent.targets
                )
                gripper_validated = None

        successful_gripper_sides: list[str] = []
        if gripper_intent is not None and gripper_validated is not None:
            attempted_sides = tuple(gripper_validated.targets)
            try:
                gripper_send_now_ns = _before_send_time(before_send)
            except Exception as error:
                gripper_failures.extend(
                    GripperSendFailure(side, str(error), "not_sent")
                    for side in attempted_sides
                )
            else:
                try:
                    # RobotAdapter owns vendor batching.  For G2 this becomes
                    # one atomic dual_tool call when both sides are present.
                    self._robot.send_gripper_command(
                        gripper_validated,
                        now_ns=gripper_send_now_ns,
                    )
                except Exception as error:
                    for side in attempted_sides:
                        self._gripper_mapper.block(side)
                        self._blocked_grippers.add(side)
                        gripper_failures.append(
                            GripperSendFailure(side, str(error), "unknown")
                        )
                else:
                    self._gripper_mapper.commit(
                        GripperIntent(
                            gripper_validated.targets,
                            source=gripper_intent.source,
                        ),
                        now_ns=gripper_send_now_ns,
                    )
                    successful_gripper_sides.extend(attempted_sides)
        gripper_finished_ns = self._clock_ns()
        cycle_finished_ns = self._clock_ns()

        return DualArmLiveResult(
            "sent" if arm_sent or successful_gripper_sides else "idle",
            frame.sequence_id,
            active_sides,
            retargeting,
            state=command_state,
            command=command,
            validated=validated,
            gripper_sides=tuple(successful_gripper_sides),
            gripper_command=gripper_command,
            gripper_validated=gripper_validated,
            arm_rejections=tuple(arm_rejections),
            workspace_clamps=tuple(workspace_clamps),
            gripper_failures=tuple(gripper_failures),
            telemetry_refreshed=telemetry_refreshed,
            timings=DualArmCycleTimings(
                state_read_ms=_milliseconds(state_started_ns, state_finished_ns),
                mapping_ms=_milliseconds(mapping_started_ns, mapping_finished_ns),
                arm_ms=_milliseconds(arm_started_ns, arm_finished_ns),
                gripper_ms=_milliseconds(
                    gripper_started_ns,
                    gripper_finished_ns,
                ),
                total_ms=_milliseconds(cycle_started_ns, cycle_finished_ns),
            ),
        )


def _before_send_time(before_send: Callable[[], int]) -> int:
    send_now_ns = before_send()
    if type(send_now_ns) is not int or send_now_ns < 0:
        raise ValueError("before_send must return a non-negative integer")
    return send_now_ns


def _milliseconds(start_ns: int, end_ns: int) -> float:
    return max(0, end_ns - start_ns) / 1_000_000.0
