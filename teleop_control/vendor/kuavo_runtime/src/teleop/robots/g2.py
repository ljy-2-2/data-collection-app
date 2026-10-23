from dataclasses import dataclass
import math
import time
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Protocol

from teleop.contracts.capabilities import AdapterOperatingMode, RobotCapabilities
from teleop.contracts.command import ValidatedGripperCommand, ValidatedJointCommand
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointInfo, JointSchema
from teleop.robots.base import IKSolver, RobotAdapter, UnsupportedCapabilityError


# Position limits copied from the installed GDK mc_impl_config.json on 2026-08-27.
# They are used only for offline validation in this phase, not as live-motion approval.
_G2_JOINT_LIMITS = (
    ("idx01_body_joint1", -1.082104, 0.000174),
    ("idx02_body_joint2", -0.000174, 2.652900),
    ("idx03_body_joint3", -1.919862, 1.570970),
    ("idx04_body_joint4", -0.436332, 0.436332),
    ("idx05_body_joint5", -3.045599, 3.045599),
    ("idx11_head_joint1", -1.570970, 1.570970),
    ("idx12_head_joint2", -0.349240, 0.349240),
    ("idx13_head_joint3", -0.534773, 0.534773),
    ("idx21_arm_l_joint1", -3.071796, 3.071796),
    ("idx22_arm_l_joint2", -2.059505, 2.059505),
    ("idx23_arm_l_joint3", -3.071796, 3.071796),
    ("idx24_arm_l_joint4", -2.495838, 1.012308),
    ("idx25_arm_l_joint5", -3.071796, 3.071796),
    ("idx26_arm_l_joint6", -1.012308, 1.012308),
    ("idx27_arm_l_joint7", -1.535907, 1.535907),
    ("idx61_arm_r_joint1", -3.071796, 3.071796),
    ("idx62_arm_r_joint2", -2.059505, 2.059505),
    ("idx63_arm_r_joint3", -3.071796, 3.071796),
    ("idx64_arm_r_joint4", -2.495838, 1.012308),
    ("idx65_arm_r_joint5", -3.071796, 3.071796),
    ("idx66_arm_r_joint6", -1.012308, 1.012308),
    ("idx67_arm_r_joint7", -1.535907, 1.535907),
)

_MAX_LIVE_STEP_RAD = 0.02
_MAX_LIVE_VELOCITY_RAD_S = 0.2
_MAX_EXPLICIT_DUAL_ARM_VELOCITY_RAD_S = 0.5
_MAX_CONFIGURABLE_ARM_VELOCITY_RAD_S = 0.65
_MAX_EXPLICIT_UPPER_BODY_VELOCITY_RAD_S = 0.25
_MAX_LIVE_TTL_MS = 100
_FLOAT_TOLERANCE = 1e-12
_G2_COUPLED_CONSTRAINT_MARGIN_RAD = 0.001
_G2_GRIPPER_OPEN_RAD = -0.785
_G2_GRIPPER_CLOSED_RAD = 0.0
_G2_GRIPPER_GROUPS = {"left": "left_tool", "right": "right_tool"}
_G2_GRIPPER_NAMES = {
    "left": "idx31_gripper_l_inner_joint1",
    "right": "idx71_gripper_r_inner_joint1",
}
_G2_GRIPPER_FEEDBACK_TOLERANCE_RAD = 1e-5
# GDK ``move_ee_pos`` already plans the physical gripper motion.  The runtime
# therefore forwards the latest normalized absolute target in one call, as the
# previously validated legacy teleop did, while retaining range/rate/TTL checks.
_G2_GRIPPER_MAX_STEP_FRACTION = 1.0
_G2_GRIPPER_MIN_PERIOD_NS = 50_000_000


@dataclass(frozen=True)
class _G2LinearCoupledConstraint:
    name: str
    coefficients: tuple[tuple[str, float], ...]
    vendor_upper_limit: float

    @property
    def joint_names(self) -> tuple[str, ...]:
        return tuple(name for name, _coefficient in self.coefficients)

    @property
    def software_upper_limit(self) -> float:
        return self.vendor_upper_limit - _G2_COUPLED_CONSTRAINT_MARGIN_RAD

    def evaluate(self, positions: Mapping[str, float]) -> float:
        return sum(
            coefficient * positions[name]
            for name, coefficient in self.coefficients
        )


# Confirmed by a physical G2 GDK rejection on 2026-08-31. The installed public
# mc_impl_config.json contains per-joint limits but not the coupled constraint table.
_G2_CONFIRMED_COUPLED_CONSTRAINTS = (
    _G2LinearCoupledConstraint(
        name="left_wrist_joint6_joint7_upper",
        coefficients=(
            ("idx26_arm_l_joint6", -1.0),
            ("idx27_arm_l_joint7", 1.0),
        ),
        vendor_upper_limit=1.92,
    ),
)


class G2StateReadError(ValueError):
    """Raised when live GDK telemetry cannot form a complete safe snapshot."""


class G2TelemetryUnavailableError(G2StateReadError):
    """One GDK telemetry sample is missing or malformed before control work."""


class G2TrackingError(G2StateReadError):
    """The measured robot pose is too far from the last accepted servo target."""

    def __init__(
        self,
        joint_name: str,
        *,
        error_rad: float,
        soft_limit_rad: float,
    ) -> None:
        self.joint_name = joint_name
        self.error_rad = float(error_rad)
        self.soft_limit_rad = float(soft_limit_rad)
        super().__init__(
            f"G2 joint {joint_name} tracking error {self.error_rad:.6f} exceeds "
            f"{self.soft_limit_rad:.6f} rad"
        )


class G2CoupledConstraintError(ValueError):
    """Raised before GDK when a candidate G2 pose violates a known coupling."""

    def __init__(
        self,
        constraint: _G2LinearCoupledConstraint,
        *,
        value: float,
    ) -> None:
        self.constraint_name = constraint.name
        self.joint_names = constraint.joint_names
        self.value = value
        self.vendor_limit = constraint.vendor_upper_limit
        self.software_limit = constraint.software_upper_limit
        super().__init__(
            f"G2 coupled constraint {constraint.name} value {value:.6f} "
            f"exceeds software limit {self.software_limit:.6f}"
        )


class G2CommandSendError(RuntimeError):
    """Raised when a validated request fails inside the vendor command call."""


class G2JointStateReader(Protocol):
    """Narrow read-only surface consumed from an injected GDK Robot."""

    def get_joint_states(self) -> Mapping[str, object]: ...


class G2JointCommandSender(Protocol):
    """Narrow write surface injected only for an explicitly armed G2 adapter."""

    def send(self, request: "G2JointControlRequestPreview") -> None: ...


class G2GripperCommandSender(Protocol):
    """Narrow G2 end-effector write surface injected only when armed."""

    def send(self, request: "G2GripperRequestPreview") -> None: ...

    def send_batch(
        self,
        requests: Sequence["G2GripperRequestPreview"],
    ) -> None: ...


def _require_now_ns(now_ns: int) -> int:
    if type(now_ns) is not int or now_ns < 0:
        raise ValueError("now_ns must be a non-negative integer")
    return now_ns


def make_g2_joint_schema(
    *,
    max_step_rad: float = 0.02,
    arm_velocity_rad_s: float = _MAX_LIVE_VELOCITY_RAD_S,
    body_velocity_rad_s: float = 0.1,
) -> JointSchema:
    """Build the 22-joint G2 schema used by the robot-independent core."""
    if not math.isfinite(max_step_rad) or max_step_rad <= 0:
        raise ValueError("max_step_rad must be finite and positive")
    if (
        not math.isfinite(arm_velocity_rad_s)
        or arm_velocity_rad_s <= 0
        or arm_velocity_rad_s > _MAX_CONFIGURABLE_ARM_VELOCITY_RAD_S
    ):
        raise ValueError("arm_velocity_rad_s must be within (0, 0.65]")
    if (
        isinstance(body_velocity_rad_s, bool)
        or not isinstance(body_velocity_rad_s, (int, float))
        or not math.isfinite(body_velocity_rad_s)
        or body_velocity_rad_s <= 0
        or body_velocity_rad_s > _MAX_EXPLICIT_UPPER_BODY_VELOCITY_RAD_S
    ):
        raise ValueError("body_velocity_rad_s must be within (0, 0.25]")
    return JointSchema(
        joints=tuple(
            JointInfo(
                name,
                position_min,
                position_max,
                max_step=min(max_step_rad, g2_live_step_rad(name)),
                max_velocity=(
                    arm_velocity_rad_s
                    if "_arm_" in name
                    else (
                        body_velocity_rad_s
                        if "_body_" in name
                        else g2_live_velocity_rad_s(name)
                    )
                ),
                group=_g2_joint_group(name),
            )
            for name, position_min, position_max in _G2_JOINT_LIMITS
        )
    )


def _g2_joint_group(joint_name: str) -> str:
    if "_body_" in joint_name:
        return "body"
    if "_head_" in joint_name:
        return "head"
    if "_arm_l_" in joint_name:
        return "left_arm"
    if "_arm_r_" in joint_name:
        return "right_arm"
    raise ValueError(f"unknown G2 joint group: {joint_name!r}")


def g2_live_step_rad(joint_name: str) -> float:
    """Return the 10 Hz live-control step for one configured G2 joint."""
    if joint_name not in {name for name, _, _ in _G2_JOINT_LIMITS}:
        raise ValueError(f"unknown G2 joint: {joint_name!r}")
    return 0.02 if "_arm_" in joint_name else 0.01


def g2_live_velocity_rad_s(joint_name: str) -> float:
    """Return the final live velocity cap for one configured G2 joint."""
    if joint_name not in {name for name, _, _ in _G2_JOINT_LIMITS}:
        raise ValueError(f"unknown G2 joint: {joint_name!r}")
    return 0.2 if "_arm_" in joint_name else 0.1


def _make_g2_live_joint_schema(
    *,
    max_step_rad: float,
    arm_velocity_rad_s: float,
    body_velocity_rad_s: float = 0.1,
) -> JointSchema:
    return make_g2_joint_schema(
        max_step_rad=max_step_rad,
        arm_velocity_rad_s=arm_velocity_rad_s,
        body_velocity_rad_s=body_velocity_rad_s,
    )


def _validate_g2_coupled_constraints(positions: Mapping[str, float]) -> None:
    for constraint in _G2_CONFIRMED_COUPLED_CONSTRAINTS:
        value = constraint.evaluate(positions)
        if value > constraint.software_upper_limit + _FLOAT_TOLERANCE:
            raise G2CoupledConstraintError(constraint, value=value)


@dataclass(frozen=True)
class G2JointControlRequestPreview:
    """Plain data shaped like the future GDK JointControlReq."""

    life_time_s: float
    joint_names: tuple[str, ...]
    joint_positions: tuple[float, ...]
    joint_velocities: tuple[float, ...]


@dataclass(frozen=True)
class G2GripperRequestPreview:
    """Plain representation of one GDK omnipicker request."""

    side: str
    group: str
    target_type: str
    position_rad: float


class G2GDKGripperCommandSender:
    """Translate one or both G2 grippers into one GDK ``move_ee_pos``."""

    def __init__(self, *, gdk: object, robot: object) -> None:
        self._gdk = gdk
        self._robot = robot

    def send(self, request: G2GripperRequestPreview) -> None:
        self._send_requests((request,))

    def send_batch(
        self,
        requests: Sequence[G2GripperRequestPreview],
    ) -> None:
        """Send left and right together through GDK's atomic ``dual_tool`` group."""
        normalized = tuple(requests)
        if len(normalized) != 2:
            raise ValueError("G2 gripper batch must contain exactly two sides")
        self._send_requests(normalized)

    def _send_requests(
        self,
        requests: tuple[G2GripperRequestPreview, ...],
    ) -> None:
        if not requests:
            raise ValueError("G2 gripper request must not be empty")
        if any(not isinstance(request, G2GripperRequestPreview) for request in requests):
            raise TypeError("request must be G2GripperRequestPreview")
        sides = tuple(request.side for request in requests)
        if len(set(sides)) != len(sides):
            raise ValueError("G2 gripper request contains duplicate sides")
        if len(requests) == 2 and sides != ("left", "right"):
            raise ValueError("G2 dual gripper order must be left then right")
        for request in requests:
            expected_group = _G2_GRIPPER_GROUPS.get(request.side)
            if expected_group is None or request.group != expected_group:
                raise ValueError("G2 gripper side and group do not match")
            if request.target_type != "omnipicker":
                raise ValueError("G2 gripper target_type must be omnipicker")
            if (
                isinstance(request.position_rad, bool)
                or not isinstance(request.position_rad, (int, float))
                or not math.isfinite(request.position_rad)
                or not _G2_GRIPPER_OPEN_RAD
                <= request.position_rad
                <= _G2_GRIPPER_CLOSED_RAD
            ):
                raise ValueError("G2 gripper position exceeds omnipicker limits")
        try:
            rows = []
            for request in requests:
                row = self._gdk.JointState()  # type: ignore[attr-defined]
                row.position = float(request.position_rad)
                rows.append(row)
            vendor_request = self._gdk.JointStates()  # type: ignore[attr-defined]
            vendor_request.group = (
                "dual_tool" if len(requests) == 2 else requests[0].group
            )
            vendor_request.target_type = "omnipicker"
            vendor_request.states = rows
            vendor_request.nums = len(rows)
            result = self._robot.move_ee_pos(vendor_request)  # type: ignore[attr-defined]
            success = self._gdk.GDKRes.kSuccess  # type: ignore[attr-defined]
            if result not in (None, 0) and result != success:
                raise RuntimeError(f"GDK rejected gripper target: {result!r}")
        except Exception as error:
            raise G2CommandSendError(f"GDK move_ee_pos failed: {error}") from error


class G2GDKJointCommandSender:
    """Translate one validated adapter request into the installed GDK API."""

    def __init__(self, *, gdk: object, robot: object) -> None:
        self._gdk = gdk
        self._robot = robot

    def send(self, request: G2JointControlRequestPreview) -> None:
        if not isinstance(request, G2JointControlRequestPreview):
            raise TypeError("request must be G2JointControlRequestPreview")
        if (
            isinstance(request.life_time_s, bool)
            or not isinstance(request.life_time_s, (int, float))
            or not math.isfinite(request.life_time_s)
            or request.life_time_s <= 0
        ):
            raise ValueError("G2 live request TTL must be finite and positive")
        if request.life_time_s > _MAX_LIVE_TTL_MS / 1000.0 + _FLOAT_TOLERANCE:
            raise ValueError("G2 live request exceeds 100 ms TTL cap")
        request_size = len(request.joint_names)
        if request_size == 0:
            raise ValueError("G2 live request must contain at least one joint")
        if not (
            len(request.joint_positions) == request_size
            and len(request.joint_velocities) == request_size
        ):
            raise ValueError("G2 live request joint fields must have equal lengths")
        if any(
            not isinstance(name, str) or not name
            for name in request.joint_names
        ):
            raise ValueError("G2 live request joint names must be non-empty strings")
        if len(set(request.joint_names)) != request_size:
            raise ValueError("G2 live request contains duplicate joints")
        for name, position, velocity in zip(
            request.joint_names,
            request.joint_positions,
            request.joint_velocities,
            strict=True,
        ):
            if (
                isinstance(position, bool)
                or not isinstance(position, (int, float))
                or not math.isfinite(position)
            ):
                raise ValueError(f"G2 live request position for {name} must be finite")
            if (
                isinstance(velocity, bool)
                or not isinstance(velocity, (int, float))
                or not math.isfinite(velocity)
                or velocity < 0
            ):
                raise ValueError(
                    f"G2 live request velocity for {name} must be finite and non-negative"
                )
            if velocity > g2_live_velocity_rad_s(name) + _FLOAT_TOLERANCE:
                raise ValueError(
                    f"G2 live request exceeds velocity cap for {name}"
                )
        try:
            vendor_request = self._gdk.JointControlReq()  # type: ignore[attr-defined]
            vendor_request.life_time = request.life_time_s
            vendor_request.joint_names = list(request.joint_names)
            vendor_request.joint_positions = list(request.joint_positions)
            vendor_request.joint_velocities = list(request.joint_velocities)
            self._robot.joint_control_request(vendor_request)  # type: ignore[attr-defined]
        except Exception as error:
            raise G2CommandSendError(f"GDK JointControl failed: {error}") from error


class G2GDKJointServoCommandSender:
    """Translate validated arm targets into GDK's streaming servo API."""

    def __init__(
        self,
        *,
        gdk: object,
        robot: object,
        control_period_s: float,
        max_arm_velocity_rad_s: float = 0.5,
    ) -> None:
        if (
            isinstance(control_period_s, bool)
            or not isinstance(control_period_s, (int, float))
            or not math.isfinite(control_period_s)
            or control_period_s <= 0.0
            or control_period_s > 0.1
        ):
            raise ValueError("servo control_period_s must be within (0, 0.1]")
        if (
            isinstance(max_arm_velocity_rad_s, bool)
            or not isinstance(max_arm_velocity_rad_s, (int, float))
            or not math.isfinite(max_arm_velocity_rad_s)
            or max_arm_velocity_rad_s <= 0.0
            or max_arm_velocity_rad_s > _MAX_EXPLICIT_DUAL_ARM_VELOCITY_RAD_S
        ):
            raise ValueError("servo arm velocity must be within (0, 0.5] rad/s")
        self._gdk = gdk
        self._robot = robot
        self._control_period_s = float(control_period_s)
        self._max_arm_velocity_rad_s = float(max_arm_velocity_rad_s)
        self._arm_names = frozenset(
            name for name, _, _ in _G2_JOINT_LIMITS if "_arm_" in name
        )

    def send(self, request: G2JointControlRequestPreview) -> None:
        if not isinstance(request, G2JointControlRequestPreview):
            raise TypeError("request must be G2JointControlRequestPreview")
        if not request.joint_names:
            raise ValueError("G2 servo request must contain arm joints")
        if not (
            len(request.joint_names)
            == len(request.joint_positions)
            == len(request.joint_velocities)
        ):
            raise ValueError("G2 servo request joint fields must have equal lengths")
        if len(set(request.joint_names)) != len(request.joint_names):
            raise ValueError("G2 servo request contains duplicate joints")
        for name, position, velocity in zip(
            request.joint_names,
            request.joint_positions,
            request.joint_velocities,
            strict=True,
        ):
            if name not in self._arm_names:
                raise ValueError(f"G2 servo request supports arm joints only: {name}")
            if (
                isinstance(position, bool)
                or not isinstance(position, (int, float))
                or not math.isfinite(position)
            ):
                raise ValueError(f"G2 servo position for {name} must be finite")
            if (
                isinstance(velocity, bool)
                or not isinstance(velocity, (int, float))
                or not math.isfinite(velocity)
                or velocity <= 0.0
                or velocity > self._max_arm_velocity_rad_s + _FLOAT_TOLERANCE
            ):
                raise ValueError(f"G2 servo velocity for {name} exceeds configured cap")
        try:
            vendor_request = self._gdk.JointServoControlReq()  # type: ignore[attr-defined]
            vendor_request.control_period = self._control_period_s
            vendor_request.joint_names = list(request.joint_names)
            vendor_request.joint_positions = list(request.joint_positions)
            vendor_request.joint_velocities = list(request.joint_velocities)
            result = self._robot.joint_servo_control(vendor_request)  # type: ignore[attr-defined]
            success = self._gdk.GDKRes.kSuccess  # type: ignore[attr-defined]
            if result not in (None, 0) and result != success:
                raise RuntimeError(f"GDK rejected servo target: {result!r}")
        except Exception as error:
            raise G2CommandSendError(f"GDK JointServoControl failed: {error}") from error


class G2Adapter(RobotAdapter):
    """G2 boundary with dry-run preview and live read-only state modes."""

    def __init__(
        self,
        *,
        dry_run: bool = True,
        initial_positions: Mapping[str, float] | None = None,
        max_step_rad: float = 0.02,
        default_velocity_rad_s: float = 0.10,
        state_reader: G2JointStateReader | None = None,
        command_sender: G2JointCommandSender | None = None,
        gripper_sender: G2GripperCommandSender | None = None,
        allow_live_commands: bool = False,
        live_arm_velocity_cap_rad_s: float = _MAX_LIVE_VELOCITY_RAD_S,
        body_velocity_cap_rad_s: float = 0.1,
        continuous_servo_commands: bool = False,
        max_tracking_error_rad: float = 0.10,
        ik_solver: IKSolver | None = None,
        upper_body_mode: bool = False,
    ) -> None:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be bool")
        if not isinstance(allow_live_commands, bool):
            raise ValueError("allow_live_commands must be bool")
        if not isinstance(continuous_servo_commands, bool):
            raise ValueError("continuous_servo_commands must be bool")
        if not isinstance(upper_body_mode, bool):
            raise ValueError("upper_body_mode must be bool")
        if dry_run and state_reader is not None:
            raise ValueError("state_reader is only valid when dry_run=False")
        if dry_run and command_sender is not None:
            raise ValueError("command_sender is only valid when dry_run=False")
        if dry_run and gripper_sender is not None:
            raise ValueError("gripper_sender is only valid when dry_run=False")
        if dry_run and allow_live_commands:
            raise ValueError("allow_live_commands requires dry_run=False")
        if continuous_servo_commands and not allow_live_commands:
            raise ValueError("continuous servo mode requires live commands")
        if not dry_run and state_reader is None:
            raise UnsupportedCapabilityError(
                "live read-only G2Adapter requires an injected state_reader"
            )
        if not dry_run and command_sender is not None and not allow_live_commands:
            raise ValueError("command_sender requires allow_live_commands=True")
        if not dry_run and gripper_sender is not None and not allow_live_commands:
            raise ValueError("gripper_sender requires allow_live_commands=True")
        if (
            not dry_run
            and gripper_sender is not None
            and not callable(getattr(state_reader, "get_end_state", None))
        ):
            raise ValueError("gripper_sender requires get_end_state feedback")
        if not dry_run and allow_live_commands and command_sender is None:
            raise ValueError("allow_live_commands requires command_sender")
        if not dry_run and initial_positions:
            raise ValueError("initial_positions are only valid in dry-run mode")
        if not math.isfinite(default_velocity_rad_s) or default_velocity_rad_s <= 0:
            raise ValueError("default_velocity_rad_s must be finite and positive")
        arm_velocity_limit = (
            _MAX_CONFIGURABLE_ARM_VELOCITY_RAD_S
            if upper_body_mode
            else _MAX_EXPLICIT_DUAL_ARM_VELOCITY_RAD_S
        )
        if (
            not math.isfinite(live_arm_velocity_cap_rad_s)
            or live_arm_velocity_cap_rad_s <= 0
            or live_arm_velocity_cap_rad_s > arm_velocity_limit
        ):
            raise ValueError(
                f"live arm velocity cap must be within (0, {arm_velocity_limit:g}] rad/s"
            )
        if (
            isinstance(body_velocity_cap_rad_s, bool)
            or not isinstance(body_velocity_cap_rad_s, (int, float))
            or not math.isfinite(body_velocity_cap_rad_s)
            or body_velocity_cap_rad_s <= 0
            or body_velocity_cap_rad_s > _MAX_EXPLICIT_UPPER_BODY_VELOCITY_RAD_S
        ):
            raise ValueError("body velocity cap must be within (0, 0.25] rad/s")
        if allow_live_commands and max_step_rad > _MAX_LIVE_STEP_RAD:
            raise ValueError("live max_step_rad must be at most 0.02")
        if (
            allow_live_commands
            and default_velocity_rad_s > live_arm_velocity_cap_rad_s
        ):
            raise ValueError("live velocity exceeds configured arm velocity cap")
        if (
            isinstance(max_tracking_error_rad, bool)
            or not isinstance(max_tracking_error_rad, (int, float))
            or not math.isfinite(max_tracking_error_rad)
            or max_tracking_error_rad <= 0.0
            or max_tracking_error_rad > 0.5
        ):
            raise ValueError("max tracking error must be within (0, 0.5] rad")

        super().__init__(ik_solver=ik_solver)
        self._dry_run = dry_run
        self._state_reader = state_reader
        self._command_sender = command_sender
        self._gripper_sender = gripper_sender
        self._allow_live_commands = allow_live_commands
        self._continuous_servo_commands = continuous_servo_commands
        self._max_tracking_error_rad = float(max_tracking_error_rad)
        self._schema = (
            _make_g2_live_joint_schema(
                max_step_rad=max_step_rad,
                arm_velocity_rad_s=live_arm_velocity_cap_rad_s,
                body_velocity_rad_s=body_velocity_cap_rad_s,
            )
            if allow_live_commands
            else make_g2_joint_schema(
                max_step_rad=max_step_rad,
                arm_velocity_rad_s=live_arm_velocity_cap_rad_s,
                body_velocity_rad_s=body_velocity_cap_rad_s,
            )
        )
        self._default_velocity_rad_s = default_velocity_rad_s
        self._positions = {name: 0.0 for name in self._schema.commandable_names}
        if initial_positions:
            self._set_initial_positions(initial_positions)
        self._timestamp_ns = time.monotonic_ns()
        self._last_request: G2JointControlRequestPreview | None = None
        self._last_gripper_requests: tuple[G2GripperRequestPreview, ...] = ()
        self._gripper_reference_fractions: dict[str, float] = {}
        self._last_gripper_send_ns: dict[str, int] = {}
        self._last_commanded_positions: dict[str, float] = {}
        self._last_command_ns: int | None = None
        self._stopped = False

    def _set_initial_positions(self, positions: Mapping[str, float]) -> None:
        unknown = set(positions) - set(self._positions)
        if unknown:
            raise ValueError(f"unknown initial G2 joints: {sorted(unknown)}")
        for name, position in positions.items():
            joint = self._schema.get(name)
            if not math.isfinite(position):
                raise ValueError(f"initial position for {name} must be finite")
            if not joint.position_min <= position <= joint.position_max:
                raise ValueError(f"initial position for {name} exceeds position limit")
        self._positions.update(positions)

    @property
    def last_request(self) -> G2JointControlRequestPreview | None:
        return self._last_request

    @property
    def last_gripper_requests(self) -> tuple[G2GripperRequestPreview, ...]:
        return self._last_gripper_requests

    def get_capabilities(self) -> RobotCapabilities:
        ik_kind = getattr(self._ik_solver, "kind", None)
        has_urdf_ik = self.has_ik_solver and ik_kind == "urdf"
        has_native_ik = self.has_ik_solver and ik_kind == "native"
        if not self._dry_run:
            return RobotCapabilities(
                operating_mode=(
                    AdapterOperatingMode.LIVE
                    if self._allow_live_commands
                    else AdapterOperatingMode.READ_ONLY
                ),
                joint_state_read=True,
                joint_position_control=self._allow_live_commands,
                supports_partial_joint_command=self._allow_live_commands,
                physical_joint_commands=self._allow_live_commands,
                urdf_ik=has_urdf_ik,
                native_ik=has_native_ik,
                mobile_base_control=False,
                end_effector_control=self.has_ik_solver,
                gripper_state_read=callable(
                    getattr(self._state_reader, "get_end_state", None)
                ),
                gripper_control=(
                    self._allow_live_commands and self._gripper_sender is not None
                ),
            )
        return RobotCapabilities(
            operating_mode=AdapterOperatingMode.PREVIEW,
            joint_state_read=True,
            joint_position_control=True,
            supports_partial_joint_command=True,
            physical_joint_commands=False,
            urdf_ik=has_urdf_ik,
            native_ik=has_native_ik,
            mobile_base_control=False,
            end_effector_control=self.has_ik_solver,
            gripper_state_read=False,
            gripper_control=True,
        )

    def get_joint_schema(self) -> JointSchema:
        return self._schema

    def read_state(self, *, now_ns: int | None = None) -> RobotState:
        if now_ns is not None:
            _require_now_ns(now_ns)
        if not self._dry_run:
            try:
                payload = self._state_reader.get_joint_states()  # type: ignore[union-attr]
                positions = _parse_live_joint_positions(payload, self._schema)
            except G2StateReadError as error:
                raise G2TelemetryUnavailableError(str(error)) from error
            except Exception as error:
                raise G2TelemetryUnavailableError(
                    f"GDK get_joint_states() failed: {error}"
                ) from error
            self._positions = positions
            self._timestamp_ns = (
                time.monotonic_ns() if now_ns is None else now_ns
            )
            return RobotState(self._positions, self._timestamp_ns)
        if now_ns is not None:
            self._timestamp_ns = _require_now_ns(now_ns)
        return RobotState(self._positions, self._timestamp_ns)

    def read_gripper_state(self) -> Mapping[str, float]:
        if self._dry_run:
            raise UnsupportedCapabilityError(
                "dry-run G2Adapter has no physical gripper feedback"
            )
        reader = getattr(self._state_reader, "get_end_state", None)
        if not callable(reader):
            raise UnsupportedCapabilityError(
                "live G2Adapter has no gripper state feedback"
            )
        try:
            payload = reader()
        except Exception as error:
            raise G2StateReadError(f"GDK get_end_state() failed: {error}") from error
        fractions = _parse_live_gripper_fractions(payload)
        self._gripper_reference_fractions.update(fractions)
        return MappingProxyType(fractions)

    def get_safety_reference_state(
        self,
        state: RobotState,
        *,
        now_ns: int,
    ) -> RobotState:
        super().get_safety_reference_state(state, now_ns=now_ns)
        if not self._continuous_servo_commands or self._last_command_ns is None:
            return state
        age_ns = now_ns - self._last_command_ns
        if age_ns < 0:
            raise ValueError("last G2 command timestamp is in the future")
        if age_ns >= _MAX_LIVE_TTL_MS * 1_000_000:
            self._last_commanded_positions.clear()
            self._last_command_ns = None
            return state

        reference = dict(state.positions)
        for name, target in self._last_commanded_positions.items():
            actual = state.positions.get(name)
            if actual is None:
                raise G2StateReadError(f"G2 tracking state is missing joint {name}")
            error = abs(target - actual)
            if error > self._max_tracking_error_rad + _FLOAT_TOLERANCE:
                raise G2TrackingError(
                    name,
                    error_rad=error,
                    soft_limit_rad=self._max_tracking_error_rad,
                )
            reference[name] = target
        return RobotState(reference, state.timestamp_ns)

    def build_request_preview(
        self,
        command: ValidatedJointCommand,
        *,
        now_ns: int | None = None,
    ) -> G2JointControlRequestPreview:
        if not isinstance(command, ValidatedJointCommand):
            raise TypeError("G2Adapter accepts only ValidatedJointCommand")
        unknown = set(command.targets) - set(self._positions)
        if unknown:
            raise ValueError(f"unknown G2 command joints: {sorted(unknown)}")

        effective_now_ns = (
            time.monotonic_ns() if now_ns is None else _require_now_ns(now_ns)
        )
        if effective_now_ns < command.created_time_ns:
            raise ValueError("validated G2 command timestamp is in the future")
        if self._allow_live_commands and command.ttl_ms > _MAX_LIVE_TTL_MS:
            raise ValueError("live command TTL must be at most 100 ms")
        deadline_ns = command.created_time_ns + command.ttl_ms * 1_000_000
        remaining_ns = deadline_ns - effective_now_ns
        if remaining_ns <= 0:
            raise ValueError("validated G2 command has expired before send")

        reference_state = self.get_safety_reference_state(
            RobotState(self._positions, self._timestamp_ns),
            now_ns=effective_now_ns,
        )
        for name, target in command.targets.items():
            joint = self._schema.get(name)
            if not math.isfinite(target):
                raise ValueError(f"G2 joint {name} target must be finite")
            if not joint.position_min <= target <= joint.position_max:
                raise ValueError(f"G2 joint {name} target exceeds position limit")
            if (
                abs(target - reference_state.positions[name])
                > joint.max_step + _FLOAT_TOLERANCE
            ):
                raise ValueError(f"G2 joint {name} target exceeds step limit")

        if self._allow_live_commands:
            candidate_positions = dict(reference_state.positions)
            candidate_positions.update(command.targets)
            _validate_g2_coupled_constraints(candidate_positions)

        names = tuple(command.targets)
        return G2JointControlRequestPreview(
            life_time_s=remaining_ns / 1_000_000_000,
            joint_names=names,
            joint_positions=tuple(command.targets[name] for name in names),
            joint_velocities=tuple(
                min(
                    self._default_velocity_rad_s,
                    self._schema.get(name).max_velocity,
                )
                if self._allow_live_commands
                else self._default_velocity_rad_s
                for name in names
            ),
        )

    def send_joint_command(
        self,
        command: ValidatedJointCommand,
        *,
        now_ns: int | None = None,
    ) -> None:
        if self._stopped:
            raise RuntimeError("G2Adapter is stopped")
        if not self._dry_run and not self._allow_live_commands:
            raise UnsupportedCapabilityError(
                "live G2Adapter is read-only; joint commands are disabled"
            )
        effective_now_ns = (
            time.monotonic_ns() if now_ns is None else _require_now_ns(now_ns)
        )
        request = self.build_request_preview(command, now_ns=effective_now_ns)
        if not self._dry_run:
            self._command_sender.send(request)  # type: ignore[union-attr]
        self._last_request = request
        if self._continuous_servo_commands:
            self._last_commanded_positions.update(command.targets)
            self._last_command_ns = effective_now_ns

    def build_gripper_request_previews(
        self,
        command: ValidatedGripperCommand,
        *,
        now_ns: int | None = None,
    ) -> tuple[G2GripperRequestPreview, ...]:
        if not isinstance(command, ValidatedGripperCommand):
            raise TypeError("G2Adapter accepts only ValidatedGripperCommand")
        effective_now_ns = (
            time.monotonic_ns() if now_ns is None else _require_now_ns(now_ns)
        )
        if effective_now_ns < command.created_time_ns:
            raise ValueError("validated G2 gripper command timestamp is in the future")
        if self._allow_live_commands and command.ttl_ms > _MAX_LIVE_TTL_MS:
            raise ValueError("live gripper command TTL must be at most 100 ms")
        deadline_ns = command.created_time_ns + command.ttl_ms * 1_000_000
        if deadline_ns - effective_now_ns <= 0:
            raise ValueError("validated G2 gripper command has expired before send")

        previews = []
        for side in ("left", "right"):
            if side not in command.targets:
                continue
            fraction = command.targets[side]
            if (
                isinstance(fraction, bool)
                or not isinstance(fraction, (int, float))
                or not math.isfinite(fraction)
                or not 0.0 <= fraction <= 1.0
            ):
                raise ValueError(
                    f"G2 {side} gripper target must be within [0, 1]"
                )
            position = _G2_GRIPPER_OPEN_RAD + fraction * (
                _G2_GRIPPER_CLOSED_RAD - _G2_GRIPPER_OPEN_RAD
            )
            previews.append(
                G2GripperRequestPreview(
                    side=side,
                    group=_G2_GRIPPER_GROUPS[side],
                    target_type="omnipicker",
                    position_rad=position,
                )
            )
        return tuple(previews)

    def send_gripper_command(
        self,
        command: ValidatedGripperCommand,
        *,
        now_ns: int | None = None,
    ) -> None:
        if self._stopped:
            raise RuntimeError("G2Adapter is stopped")
        if not self._dry_run and not self._allow_live_commands:
            raise UnsupportedCapabilityError(
                "live G2Adapter is read-only; gripper commands are disabled"
            )
        if not self._dry_run and self._gripper_sender is None:
            raise UnsupportedCapabilityError(
                "live G2Adapter has no injected gripper command sender"
            )
        effective_now_ns = (
            time.monotonic_ns() if now_ns is None else _require_now_ns(now_ns)
        )
        requests = self.build_gripper_request_previews(
            command,
            now_ns=effective_now_ns,
        )
        if not self._dry_run:
            if len(requests) not in (1, 2):
                raise ValueError("live G2 gripper send requires one or two sides")
            for request in requests:
                side = request.side
                reference = self._gripper_reference_fractions.get(side)
                if reference is None:
                    raise ValueError(
                        f"G2 {side} gripper feedback must be read before live control"
                    )
                target = command.targets[side]
                if (
                    abs(target - reference)
                    > _G2_GRIPPER_MAX_STEP_FRACTION + _FLOAT_TOLERANCE
                ):
                    raise ValueError(f"G2 {side} gripper target exceeds step limit")
                previous_send_ns = self._last_gripper_send_ns.get(side)
                if (
                    previous_send_ns is not None
                    and effective_now_ns - previous_send_ns < _G2_GRIPPER_MIN_PERIOD_NS
                ):
                    raise ValueError(f"G2 {side} gripper target exceeds rate limit")
            # Count every physical attempt, including one whose vendor result is
            # unknown, so failures cannot trigger a rapid retry loop.
            for request in requests:
                self._last_gripper_send_ns[request.side] = effective_now_ns
            try:
                if len(requests) == 2:
                    self._gripper_sender.send_batch(requests)  # type: ignore[union-attr]
                else:
                    self._gripper_sender.send(requests[0])  # type: ignore[union-attr]
            except Exception:
                # A vendor-call failure leaves the physical outcome unknown.
                # Require fresh feedback before each attempted side can move again.
                for request in requests:
                    self._gripper_reference_fractions.pop(request.side, None)
                raise
            for request in requests:
                self._gripper_reference_fractions[request.side] = command.targets[
                    request.side
                ]
        self._last_gripper_requests = requests

    def stop(self) -> None:
        self._last_commanded_positions.clear()
        self._last_command_ns = None
        self._gripper_reference_fractions.clear()
        self._last_gripper_send_ns.clear()
        self._stopped = True


def _parse_live_joint_positions(
    payload: object,
    schema: JointSchema,
) -> dict[str, float]:
    if not isinstance(payload, Mapping):
        raise G2StateReadError("GDK joint-state payload must be a mapping")
    rows = payload.get("states")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise G2StateReadError("GDK joint-state states must be a sequence")

    expected = set(schema.commandable_names)
    observed: dict[str, float] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise G2StateReadError(
                f"GDK joint-state states[{index}] must be a mapping"
            )
        raw_name = row.get("name", row.get("joint_name"))
        if not isinstance(raw_name, str) or not raw_name:
            raise G2StateReadError(
                f"GDK joint-state states[{index}] must contain a joint name"
            )
        if raw_name not in expected:
            continue
        if raw_name in observed:
            raise G2StateReadError(f"duplicate G2 joint telemetry: {raw_name}")

        raw_position = row.get("motor_position")
        if isinstance(raw_position, (bool, str, bytes)):
            raise G2StateReadError(
                f"G2 joint {raw_name} motor_position must be finite"
            )
        try:
            position = float(raw_position)
        except (TypeError, ValueError, OverflowError) as error:
            raise G2StateReadError(
                f"G2 joint {raw_name} motor_position must be finite"
            ) from error
        if not math.isfinite(position):
            raise G2StateReadError(
                f"G2 joint {raw_name} motor_position must be finite"
            )
        observed[raw_name] = position

    missing = [name for name in schema.commandable_names if name not in observed]
    if missing:
        raise G2StateReadError(
            f"missing configured G2 joints: {', '.join(missing)}"
        )
    return {name: observed[name] for name in schema.commandable_names}


def _parse_live_gripper_fractions(payload: object) -> dict[str, float]:
    if not isinstance(payload, Mapping):
        raise G2StateReadError("GDK end-state payload must be a mapping")
    fractions = {}
    for side in ("left", "right"):
        key = f"{side}_end_state"
        block = payload.get(key)
        if not isinstance(block, Mapping):
            raise G2StateReadError(f"GDK end-state payload is missing {key}")
        names = block.get("names")
        states = block.get("end_states")
        if (
            not isinstance(names, Sequence)
            or isinstance(names, (str, bytes))
            or not isinstance(states, Sequence)
            or isinstance(states, (str, bytes))
            or len(names) != len(states)
        ):
            raise G2StateReadError(f"GDK {key} names/end_states are invalid")
        try:
            index = list(names).index(_G2_GRIPPER_NAMES[side])
            row = states[index]
        except ValueError as error:
            raise G2StateReadError(
                f"GDK {key} is missing {_G2_GRIPPER_NAMES[side]}"
            ) from error
        if not isinstance(row, Mapping):
            raise G2StateReadError(f"GDK {key} gripper state must be a mapping")
        raw_position = row.get("position")
        if isinstance(raw_position, (bool, str, bytes)):
            raise G2StateReadError(f"GDK {key} gripper position must be finite")
        try:
            position = float(raw_position)
        except (TypeError, ValueError, OverflowError) as error:
            raise G2StateReadError(
                f"GDK {key} gripper position must be finite"
            ) from error
        if not math.isfinite(position):
            raise G2StateReadError(f"GDK {key} gripper position must be finite")
        if (
            position < _G2_GRIPPER_OPEN_RAD - _G2_GRIPPER_FEEDBACK_TOLERANCE_RAD
            or position
            > _G2_GRIPPER_CLOSED_RAD + _G2_GRIPPER_FEEDBACK_TOLERANCE_RAD
        ):
            raise G2StateReadError(f"GDK {key} gripper position exceeds limits")
        clamped = min(_G2_GRIPPER_CLOSED_RAD, max(_G2_GRIPPER_OPEN_RAD, position))
        fractions[side] = (clamped - _G2_GRIPPER_OPEN_RAD) / (
            _G2_GRIPPER_CLOSED_RAD - _G2_GRIPPER_OPEN_RAD
        )
    return fractions
