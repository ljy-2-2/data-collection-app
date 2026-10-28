"""Robot-independent migration of the proven legacy dual-arm retargeting math."""

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Callable, Mapping

from teleop.contracts.intent import CartesianPose, EndEffectorPoseIntent
from teleop.contracts.xr import ControllerState, Pose, XRFrame


ENGAGE_THRESHOLD = 0.95
RELEASE_THRESHOLD = 0.90
RELEASE_DEBOUNCE_NS = 30_000_000

Matrix3 = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]
Vector3 = tuple[float, float, float]

_IDENTITY: Matrix3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
_ROBOT_FROM_XR: Matrix3 = (
    (0.0, 0.0, -1.0),
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
)


def _matmul(left: Matrix3, right: Matrix3) -> Matrix3:
    return tuple(
        tuple(sum(left[row][k] * right[k][col] for k in range(3)) for col in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def _transpose(matrix: Matrix3) -> Matrix3:
    return tuple(tuple(matrix[col][row] for col in range(3)) for row in range(3))  # type: ignore[return-value]


def _matvec(matrix: Matrix3, vector: Vector3) -> Vector3:
    return tuple(sum(matrix[row][col] * vector[col] for col in range(3)) for row in range(3))  # type: ignore[return-value]


def _rpy_deg(rx: float, ry: float, rz: float) -> Matrix3:
    rx, ry, rz = map(math.radians, (rx, ry, rz))
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    matrix_x: Matrix3 = ((1, 0, 0), (0, cx, -sx), (0, sx, cx))
    matrix_y: Matrix3 = ((cy, 0, sy), (0, 1, 0), (-sy, 0, cy))
    matrix_z: Matrix3 = ((cz, -sz, 0), (sz, cz, 0), (0, 0, 1))
    return _matmul(_matmul(matrix_x, matrix_y), matrix_z)


_ROTATION_COMPENSATION = {
    "left": _rpy_deg(90.0, -5.0, 0.0),
    "right": _rpy_deg(90.0, 5.0, 0.0),
}


def _quaternion_to_matrix(quaternion: tuple[float, float, float, float]) -> Matrix3:
    qx, qy, qz, qw = quaternion
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if norm < 1e-12:
        return _IDENTITY
    qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm
    return (
        (1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)),
        (2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)),
        (2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)),
    )


def _matrix_to_quaternion(matrix: Matrix3) -> tuple[float, float, float, float]:
    trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * scale
        qx = (matrix[2][1] - matrix[1][2]) / scale
        qy = (matrix[0][2] - matrix[2][0]) / scale
        qz = (matrix[1][0] - matrix[0][1]) / scale
    elif matrix[0][0] > matrix[1][1] and matrix[0][0] > matrix[2][2]:
        scale = math.sqrt(1.0 + matrix[0][0] - matrix[1][1] - matrix[2][2]) * 2.0
        qw = (matrix[2][1] - matrix[1][2]) / scale
        qx = 0.25 * scale
        qy = (matrix[0][1] + matrix[1][0]) / scale
        qz = (matrix[0][2] + matrix[2][0]) / scale
    elif matrix[1][1] > matrix[2][2]:
        scale = math.sqrt(1.0 + matrix[1][1] - matrix[0][0] - matrix[2][2]) * 2.0
        qw = (matrix[0][2] - matrix[2][0]) / scale
        qx = (matrix[0][1] + matrix[1][0]) / scale
        qy = 0.25 * scale
        qz = (matrix[1][2] + matrix[2][1]) / scale
    else:
        scale = math.sqrt(1.0 + matrix[2][2] - matrix[0][0] - matrix[1][1]) * 2.0
        qw = (matrix[1][0] - matrix[0][1]) / scale
        qx = (matrix[0][2] + matrix[2][0]) / scale
        qy = (matrix[1][2] + matrix[2][1]) / scale
        qz = 0.25 * scale
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    return (qx / norm, qy / norm, qz / norm, qw / norm)


def _yaw_frame(head_pose: Pose) -> Matrix3:
    rotation = _quaternion_to_matrix(head_pose.orientation_xyzw)
    forward = _matvec(rotation, (0.0, 0.0, -1.0))
    horizontal = (forward[0], 0.0, forward[2])
    norm = math.sqrt(horizontal[0] ** 2 + horizontal[2] ** 2)
    if norm < 1e-6:
        return _IDENTITY
    z_axis = (-horizontal[0] / norm, 0.0, -horizontal[2] / norm)
    x_axis = (z_axis[2], 0.0, -z_axis[0])
    y_axis = (0.0, 1.0, 0.0)
    return (
        (x_axis[0], y_axis[0], z_axis[0]),
        (x_axis[1], y_axis[1], z_axis[1]),
        (x_axis[2], y_axis[2], z_axis[2]),
    )


@dataclass(frozen=True)
class DualArmRetargetingResult:
    intent: EndEffectorPoseIntent | None
    gripper_fractions: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gripper_fractions",
            MappingProxyType(dict(self.gripper_fractions)),
        )


class DualArmRetargeter:
    """Map independently clutched PICO controllers to robot-base EE targets."""

    def __init__(self, scale: float = 1.0, *, scales=None) -> None:
        scale = float(scale)
        if not math.isfinite(scale) or scale <= 0.0:
            raise ValueError("scale must be finite and positive")
        self._scale = scale
        self._scales = dict.fromkeys(("left", "right"), scale)
        if scales is not None:
            if not isinstance(scales, dict) or set(scales) != {"left", "right"}:
                raise ValueError("exact left/right position scales required")
            if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                   for v in scales.values()):
                raise ValueError("position scales must be finite and positive")
            self._scales = {side: float(v) for side, v in scales.items()}
        self._states = {
            side: {"engaged": False, "controller_position": None,
                   "controller_rotation": None, "anchor": None,
                   "operator_yaw": None, "release_started_ns": None}
            for side in ("left", "right")
        }

    def declutch(self, sides: tuple[str, ...] = ('left', 'right')) -> None:
        if not sides or len(set(sides)) != len(sides) or any(s not in self._states for s in sides):
            raise ValueError('declutch sides must be unique left/right names')
        for side in sides:
            state = self._states[side]
            state.update(
                engaged=False,
                controller_position=None,
                controller_rotation=None,
                anchor=None,
                operator_yaw=None,
                release_started_ns=None,
            )

    def update(
        self,
        frame: XRFrame,
        anchor_poses: Mapping[str, CartesianPose]
        | Callable[[str], CartesianPose | None],
    ) -> DualArmRetargetingResult:
        if not isinstance(frame, XRFrame):
            raise TypeError("frame must be an XRFrame")
        grippers = {
            "left": frame.left_controller.trigger,
            "right": frame.right_controller.trigger,
        }
        if frame.stale:
            self.declutch()
            return DualArmRetargetingResult(None, grippers)

        get_anchor = (
            anchor_poses
            if callable(anchor_poses)
            else lambda side: anchor_poses.get(side)
        )
        targets = {}
        for side, controller in (
            ("left", frame.left_controller),
            ("right", frame.right_controller),
        ):
            target = self._update_side(
                side,
                controller,
                frame.head_pose,
                get_anchor,
                frame.received_monotonic_ns,
            )
            if target is not None:
                targets[side] = target
        intent = EndEffectorPoseIntent(targets) if targets else None
        return DualArmRetargetingResult(intent, grippers)

    def _update_side(
        self,
        side: str,
        controller: ControllerState,
        head_pose: Pose,
        get_anchor: Callable[[str], CartesianPose | None],
        received_monotonic_ns: int,
    ) -> CartesianPose | None:
        state = self._states[side]
        controller_position = controller.pose.position_m
        controller_rotation = _matmul(
            _quaternion_to_matrix(controller.pose.orientation_xyzw),
            _ROTATION_COMPENSATION[side],
        )

        if not state["engaged"] and controller.grip > ENGAGE_THRESHOLD:
            anchor = get_anchor(side)
            if anchor is not None:
                if not isinstance(anchor, CartesianPose):
                    raise ValueError(f"{side} anchor must be a CartesianPose")
                state.update(
                    engaged=True,
                    controller_position=controller_position,
                    controller_rotation=controller_rotation,
                    anchor=anchor,
                    operator_yaw=_yaw_frame(head_pose),
                    release_started_ns=None,
                )
        elif state["engaged"]:
            if controller.grip < RELEASE_THRESHOLD:
                release_started_ns = state["release_started_ns"]
                if (
                    release_started_ns is None
                    or received_monotonic_ns < release_started_ns
                ):
                    state["release_started_ns"] = received_monotonic_ns
                elif (
                    received_monotonic_ns - release_started_ns
                    >= RELEASE_DEBOUNCE_NS
                ):
                    state["engaged"] = False
                    state["release_started_ns"] = None
            else:
                state["release_started_ns"] = None

        if not state["engaged"]:
            return None

        start_position = state["controller_position"]
        start_rotation = state["controller_rotation"]
        anchor = state["anchor"]
        operator_yaw = state["operator_yaw"]
        transform = _matmul(_ROBOT_FROM_XR, _transpose(operator_yaw))
        delta = tuple(
            controller_position[index] - start_position[index]
            for index in range(3)
        )
        translation_delta = _matvec(transform, delta)  # type: ignore[arg-type]
        relative_rotation = _matmul(controller_rotation, _transpose(start_rotation))
        rotation_delta = _matmul(
            _matmul(transform, relative_rotation),
            _transpose(transform),
        )
        anchor_rotation = _quaternion_to_matrix(anchor.orientation_xyzw)
        target_rotation = _matmul(rotation_delta, anchor_rotation)
        target_position = tuple(
            anchor.position_m[index] + self._scales[side] * translation_delta[index]
            for index in range(3)
        )
        return CartesianPose(
            frame_id=anchor.frame_id,
            position_m=target_position,  # type: ignore[arg-type]
            orientation_xyzw=_matrix_to_quaternion(target_rotation),
        )
