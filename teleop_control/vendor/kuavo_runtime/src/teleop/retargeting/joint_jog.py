from dataclasses import dataclass
from enum import Enum
import math

from teleop.contracts.intent import JointJogIntent
from teleop.contracts.schema import JointSchema
from teleop.contracts.xr import XRFrame


class TeleopInputMode(str, Enum):
    """Exclusive ownership modes for shared PICO controller buttons."""

    OBSERVE = "observe"
    JOINT_JOG = "joint-jog"
    RECORDING = "recording"


@dataclass(frozen=True)
class JointJogStatus:
    """Read-only mapper result for monitoring and an optional direct-joint intent."""

    mode: TeleopInputMode
    selected_joint: str
    armed: bool
    intent: JointJogIntent | None
    reason: str
    direction: int = 0
    deadman: bool = False
    exit_requested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.mode, TeleopInputMode):
            raise ValueError("mode must be a TeleopInputMode")
        if not self.selected_joint:
            raise ValueError("selected_joint must not be empty")
        if not isinstance(self.armed, bool):
            raise ValueError("armed must be bool")
        if self.intent is not None and not isinstance(self.intent, JointJogIntent):
            raise ValueError("intent must be JointJogIntent or None")
        if not self.reason:
            raise ValueError("reason must not be empty")
        if type(self.direction) is not int or self.direction not in (-1, 0, 1):
            raise ValueError("direction must be -1, 0, or 1")
        if not isinstance(self.deadman, bool):
            raise ValueError("deadman must be bool")
        if not isinstance(self.exit_requested, bool):
            raise ValueError("exit_requested must be bool")
        if self.direction != 0 and not self.deadman:
            raise ValueError("non-zero direction requires deadman")
        if self.exit_requested and (self.direction != 0 or self.deadman):
            raise ValueError("exit request must be neutral and deadman-released")


class JointJogMapper:
    """Map deliberate PICO button/stick gestures to one-joint jog intents.

    The mapper never reads robot state and never creates a robot command. Entering
    joint-jog mode, receiving stale input, or leaving the mode requires all mapped
    controls to return to neutral before input is armed again.
    """

    def __init__(
        self,
        schema: JointSchema,
        *,
        step_rad: float = 0.01,
        axis_threshold: float = 0.6,
        axis_rearm_threshold: float = 0.2,
        grip_threshold: float = 0.8,
        initial_joint: str | None = None,
        exit_hold_seconds: float = 1.0,
    ) -> None:
        if not isinstance(schema, JointSchema):
            raise ValueError("schema must be a JointSchema")
        self._selectable_joints = schema.commandable_names
        if not self._selectable_joints:
            raise ValueError("schema must expose at least one commandable joint")

        self._step_rad = self._validate_threshold("step_rad", step_rad, upper=None)
        self._axis_threshold = self._validate_threshold(
            "axis_threshold", axis_threshold, upper=1.0
        )
        self._axis_rearm_threshold = self._validate_threshold(
            "axis_rearm_threshold", axis_rearm_threshold, upper=1.0
        )
        self._grip_threshold = self._validate_threshold(
            "grip_threshold", grip_threshold, upper=1.0
        )
        if self._axis_rearm_threshold >= self._axis_threshold:
            raise ValueError("axis_rearm_threshold must be below axis_threshold")
        self._exit_hold_seconds = self._validate_threshold(
            "exit_hold_seconds", exit_hold_seconds, upper=None
        )
        self._exit_hold_ns = round(self._exit_hold_seconds * 1_000_000_000)

        too_small = [
            name
            for name in self._selectable_joints
            if schema.get(name).max_step < self._step_rad
        ]
        if too_small:
            raise ValueError(
                f"step_rad exceeds max_step for commandable joints: {too_small}"
            )

        if initial_joint is None:
            self._selected_index = 0
        else:
            if initial_joint not in self._selectable_joints:
                raise ValueError(f"unknown initial_joint: {initial_joint!r}")
            self._selected_index = self._selectable_joints.index(initial_joint)
        self._last_mode: TeleopInputMode | None = None
        self._armed = False
        self._previous_x = False
        self._previous_a = False
        self._joystick_ready = False
        self._exit_hold_started_ns: int | None = None

    @staticmethod
    def _validate_threshold(name: str, value: float, *, upper: float | None) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite positive number")
        converted = float(value)
        if not math.isfinite(converted) or converted <= 0:
            raise ValueError(f"{name} must be a finite positive number")
        if upper is not None and converted > upper:
            raise ValueError(f"{name} must be <= {upper}")
        return converted

    @property
    def selectable_joints(self) -> tuple[str, ...]:
        return self._selectable_joints

    @property
    def selected_joint(self) -> str:
        return self._selectable_joints[self._selected_index]

    def update(self, frame: XRFrame, mode: TeleopInputMode) -> JointJogStatus:
        if not isinstance(frame, XRFrame):
            raise ValueError("frame must be an XRFrame")
        if not isinstance(mode, TeleopInputMode):
            raise ValueError("mode must be a TeleopInputMode")

        if mode is not TeleopInputMode.JOINT_JOG:
            self._disarm()
            self._last_mode = mode
            return self._status(mode, reason=f"mode_{mode.value}")

        if self._last_mode is not TeleopInputMode.JOINT_JOG:
            self._disarm()
        self._last_mode = mode

        x = frame.left_controller.buttons.primary
        a = frame.right_controller.buttons.primary
        b = frame.right_controller.buttons.secondary
        axis_y = frame.right_controller.axis[1]
        grip = frame.right_controller.grip

        if frame.stale:
            self._disarm()
            return self._status(mode, reason="stale_input")

        if not self._armed:
            self._previous_x = x
            self._previous_a = a
            if self._controls_are_neutral(
                x=x,
                a=a,
                b=b,
                axis_y=axis_y,
                grip=grip,
            ):
                self._armed = True
                self._joystick_ready = True
                return self._status(mode, reason="armed")
            return self._status(mode, reason="waiting_for_neutral")

        x_pressed = x and not self._previous_x
        a_pressed = a and not self._previous_a
        self._previous_x = x
        self._previous_a = a

        exit_controls_safe = (
            not x
            and not a
            and grip < self._grip_threshold
            and abs(axis_y) <= self._axis_rearm_threshold
        )
        if b and exit_controls_safe:
            if self._exit_hold_started_ns is None:
                self._exit_hold_started_ns = frame.received_monotonic_ns
            elapsed_ns = frame.received_monotonic_ns - self._exit_hold_started_ns
            if elapsed_ns < 0:
                self._disarm()
                return self._status(mode, reason="non_monotonic_input")
            if elapsed_ns >= self._exit_hold_ns:
                self._disarm()
                return self._status(
                    mode,
                    reason="exit_requested",
                    exit_requested=True,
                )
            return self._status(mode, reason="exit_hold")
        self._exit_hold_started_ns = None

        selection_controls_safe = (
            grip < self._grip_threshold
            and abs(axis_y) <= self._axis_rearm_threshold
        )
        if (x or a) and not selection_controls_safe:
            self._joystick_ready = abs(axis_y) <= self._axis_rearm_threshold
            return self._status(mode, reason="selection_requires_neutral")

        if x and a:
            self._joystick_ready = abs(axis_y) <= self._axis_rearm_threshold
            return self._status(mode, reason="ambiguous_selection")
        if x_pressed:
            self._selected_index = (
                self._selected_index - 1
            ) % len(self._selectable_joints)
            self._joystick_ready = abs(axis_y) <= self._axis_rearm_threshold
            return self._status(mode, reason="selected_previous")
        if a_pressed:
            self._selected_index = (
                self._selected_index + 1
            ) % len(self._selectable_joints)
            self._joystick_ready = abs(axis_y) <= self._axis_rearm_threshold
            return self._status(mode, reason="selected_next")

        if grip < self._grip_threshold:
            self._joystick_ready = abs(axis_y) <= self._axis_rearm_threshold
            return self._status(mode, reason="deadman_released")
        if abs(axis_y) <= self._axis_rearm_threshold:
            self._joystick_ready = True
            return self._status(mode, reason="joystick_neutral", deadman=True)
        direction = 0
        if axis_y >= self._axis_threshold:
            direction = 1
        elif axis_y <= -self._axis_threshold:
            direction = -1
        if not self._joystick_ready:
            return self._status(
                mode,
                reason="joystick_not_rearmed",
                direction=direction,
                deadman=True,
            )

        if direction > 0:
            self._joystick_ready = False
            return self._status(
                mode,
                intent=JointJogIntent(
                    deltas={self.selected_joint: self._step_rad},
                    source="pico_joint_jog",
                ),
                reason="jog_positive",
                direction=1,
                deadman=True,
            )
        if direction < 0:
            self._joystick_ready = False
            return self._status(
                mode,
                intent=JointJogIntent(
                    deltas={self.selected_joint: -self._step_rad},
                    source="pico_joint_jog",
                ),
                reason="jog_negative",
                direction=-1,
                deadman=True,
            )
        return self._status(mode, reason="joystick_dead_zone", deadman=True)

    def _controls_are_neutral(
        self,
        *,
        x: bool,
        a: bool,
        b: bool,
        axis_y: float,
        grip: float,
    ) -> bool:
        return (
            not x
            and not a
            and not b
            and abs(axis_y) <= self._axis_rearm_threshold
            and grip < self._grip_threshold
        )

    def _disarm(self) -> None:
        self._armed = False
        self._previous_x = False
        self._previous_a = False
        self._joystick_ready = False
        self._exit_hold_started_ns = None

    def _status(
        self,
        mode: TeleopInputMode,
        *,
        intent: JointJogIntent | None = None,
        reason: str,
        direction: int = 0,
        deadman: bool = False,
        exit_requested: bool = False,
    ) -> JointJogStatus:
        return JointJogStatus(
            mode=mode,
            selected_joint=self.selected_joint,
            armed=self._armed,
            intent=intent,
            reason=reason,
            direction=direction,
            deadman=deadman,
            exit_requested=exit_requested,
        )
