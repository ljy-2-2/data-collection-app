"""Offline G2 upper-body IK with one shared waist and one or both arms.

This solver deliberately lives beside, rather than inside, the frozen dual-arm
solver.  Selecting it is therefore an explicit mode choice and cannot silently
change the behavior of the validated ``dual-arm`` path.
"""

from collections.abc import Mapping
import math
from pathlib import Path
from types import MappingProxyType

from teleop.contracts.command import JointCommand
from teleop.contracts.intent import CartesianPose, EndEffectorPoseIntent
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointSchema


class UpperBodyHoldError(ValueError):
    """No candidate preserves the latched hand pose within model tolerance."""


class G2UpperBodyIKSolver:
    """Jointly move waist/arms while preserving the unoperated hand pose."""

    kind = "urdf"
    BASE_FRAME = "base_link"
    HOLD_POSITION_TOLERANCE_M = 0.002
    HOLD_ROTATION_TOLERANCE_RAD = math.radians(1.0)
    HOLD_TASK_WEIGHT = 100.0
    EE_FRAMES = {"left": "arm_l_end_link", "right": "arm_r_end_link"}
    BODY_JOINTS = (
        "idx01_body_joint1",
        "idx02_body_joint2",
        "idx03_body_joint3",
        "idx04_body_joint4",
        "idx05_body_joint5",
    )
    ARM_JOINTS = {
        "left": (
            "idx21_arm_l_joint1", "idx22_arm_l_joint2", "idx23_arm_l_joint3",
            "idx24_arm_l_joint4", "idx25_arm_l_joint5", "idx26_arm_l_joint6",
            "idx27_arm_l_joint7",
        ),
        "right": (
            "idx61_arm_r_joint1", "idx62_arm_r_joint2", "idx63_arm_r_joint3",
            "idx64_arm_r_joint4", "idx65_arm_r_joint5", "idx66_arm_r_joint6",
            "idx67_arm_r_joint7",
        ),
    }

    def __init__(
        self,
        *,
        joint_schema: JointSchema,
        body_deviation_limits_rad: Mapping[str, float],
        body_combined_pitch_deviation_limit_rad: float | None = None,
        urdf_path: str | Path | None = None,
        arm_max_joint_velocity_rad_s: float = 0.2,
        body_max_joint_velocity_rad_s: float = 0.1,
        control_period_s: float = 0.01,
        damping: float = 1.0e-3,
        rotation_weight: float = 0.5,
        body_regularization_weight: float = 40.0,
        body_roll_regularization_weight: float = 5.0,
        body_yaw_regularization_weight: float = 1.0,
        arm_regularization_weight: float = 0.05,
        posture_gain: float = 0.5,
        limit_margin_rad: float = 0.02,
    ) -> None:
        if not isinstance(joint_schema, JointSchema):
            raise TypeError("joint_schema must be a JointSchema")
        if set(body_deviation_limits_rad) != set(self.BODY_JOINTS):
            raise ValueError(
                "body_deviation_limits_rad must define exactly every G2 body joint"
            )
        body_limits = {}
        for name in self.BODY_JOINTS:
            value = body_deviation_limits_rad[name]
            if isinstance(value, bool):
                raise ValueError("body_deviation_limits_rad values must be positive")
            try:
                converted = float(value)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "body_deviation_limits_rad values must be positive"
                ) from error
            if not math.isfinite(converted) or converted <= 0:
                raise ValueError("body_deviation_limits_rad values must be positive")
            body_limits[name] = converted

        numeric = {
            "arm_max_joint_velocity_rad_s": arm_max_joint_velocity_rad_s,
            "body_max_joint_velocity_rad_s": body_max_joint_velocity_rad_s,
            "control_period_s": control_period_s,
            "damping": damping,
            "rotation_weight": rotation_weight,
            "body_regularization_weight": body_regularization_weight,
            "body_roll_regularization_weight": body_roll_regularization_weight,
            "body_yaw_regularization_weight": body_yaw_regularization_weight,
            "arm_regularization_weight": arm_regularization_weight,
            "posture_gain": posture_gain,
            "limit_margin_rad": limit_margin_rad,
        }
        if body_combined_pitch_deviation_limit_rad is not None:
            numeric["body_combined_pitch_deviation_limit_rad"] = (
                body_combined_pitch_deviation_limit_rad
            )
        converted_numeric = {}
        for name, value in numeric.items():
            if isinstance(value, bool):
                raise ValueError(f"{name} must be finite")
            try:
                converted = float(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be finite") from error
            if not math.isfinite(converted):
                raise ValueError(f"{name} must be finite")
            converted_numeric[name] = converted
        if (
            converted_numeric["arm_max_joint_velocity_rad_s"] <= 0
            or converted_numeric["body_max_joint_velocity_rad_s"] <= 0
            or converted_numeric["control_period_s"] <= 0
            or converted_numeric["damping"] <= 0
            or converted_numeric["rotation_weight"] <= 0
            or converted_numeric["body_regularization_weight"] <= 0
            or converted_numeric["body_roll_regularization_weight"] <= 0
            or converted_numeric["body_yaw_regularization_weight"] <= 0
            or converted_numeric["arm_regularization_weight"] < 0
            or converted_numeric["posture_gain"] < 0
            or converted_numeric["limit_margin_rad"] < 0
            or (
                "body_combined_pitch_deviation_limit_rad" in converted_numeric
                and converted_numeric["body_combined_pitch_deviation_limit_rad"] <= 0
            )
        ):
            raise ValueError("upper-body IK rates, weights, or margins are invalid")

        try:
            import numpy as np
            import pinocchio as pin
            from scipy.optimize import lsq_linear
        except ImportError as error:
            raise RuntimeError(
                "G2 upper-body IK requires numpy, pinocchio, and scipy; use "
                "the g2-teleop-py310 environment"
            ) from error

        self._np = np
        self._pin = pin
        self._lsq_linear = lsq_linear
        self._schema = joint_schema
        self._body_deviation_limits = body_limits
        self._arm_velocity = converted_numeric["arm_max_joint_velocity_rad_s"]
        self._body_velocity = converted_numeric["body_max_joint_velocity_rad_s"]
        self._dt = converted_numeric["control_period_s"]
        self._damping = converted_numeric["damping"]
        self._rotation_weight = converted_numeric["rotation_weight"]
        self._body_regularization = converted_numeric[
            "body_regularization_weight"
        ]
        self._body_roll_regularization = converted_numeric[
            "body_roll_regularization_weight"
        ]
        self._body_yaw_regularization = converted_numeric[
            "body_yaw_regularization_weight"
        ]
        self._arm_regularization = converted_numeric[
            "arm_regularization_weight"
        ]
        self._posture_gain = converted_numeric["posture_gain"]
        self._limit_margin = converted_numeric["limit_margin_rad"]
        self._combined_pitch_limit = converted_numeric.get(
            "body_combined_pitch_deviation_limit_rad"
        )

        default_urdf = Path(__file__).resolve().parents[3] / "assets/g2/G2_t2_crs.urdf"
        self._urdf_path = Path(urdf_path) if urdf_path is not None else default_urdf
        if not self._urdf_path.is_file():
            raise FileNotFoundError(f"G2 URDF not found: {self._urdf_path}")

        self._model = pin.buildModelFromUrdf(str(self._urdf_path))
        self._data = self._model.createData()
        self._q_neutral = pin.neutral(self._model)
        self._base_frame_id = self._required_frame_id(self.BASE_FRAME)
        self._ee_frame_ids = {
            side: self._required_frame_id(name)
            for side, name in self.EE_FRAMES.items()
        }

        all_names = self.BODY_JOINTS + self.ARM_JOINTS["left"] + self.ARM_JOINTS["right"]
        self._idx_q = {}
        self._idx_v = {}
        self._hard_lower = {}
        self._hard_upper = {}
        for name in all_names:
            joint = self._required_joint(name)
            if joint.nq != 1 or joint.nv != 1:
                raise ValueError(f"G2 upper-body joint {name} must be one-DoF")
            self._idx_q[name] = joint.idx_q
            self._idx_v[name] = joint.idx_v
            info = joint_schema.get(name)
            lower = max(
                float(self._model.lowerPositionLimit[joint.idx_q]),
                float(info.position_min),
            ) + self._limit_margin
            upper = min(
                float(self._model.upperPositionLimit[joint.idx_q]),
                float(info.position_max),
            ) - self._limit_margin
            if lower > upper:
                raise ValueError(f"joint {name} has no valid IK range after margin")
            self._hard_lower[name] = lower
            self._hard_upper[name] = upper

        self._body_seed = None
        self._body_commanded = None
        self._arm_seed = {"left": None, "right": None}
        self._arm_commanded = {"left": None, "right": None}
        self._active_sides = set()
        self._hold_targets: dict[str, CartesianPose] = {}
        self._hold_recovery_limits: dict[str, tuple[float, float]] = {}
        self.hold_errors: dict[str, tuple[float, float]] = {}
        self.last_error = {}
        self.task_progress = {}
        self.hold_step_scale = 1.0

    @property
    def hold_targets(self) -> Mapping[str, CartesianPose]:
        """Fixed base-frame poses, latched only when a side enters hold."""
        return MappingProxyType(dict(self._hold_targets))

    def _required_frame_id(self, name: str) -> int:
        frame_id = self._model.getFrameId(name)
        if frame_id >= len(self._model.frames):
            raise ValueError(f"G2 URDF is missing frame {name}")
        return frame_id

    def _required_joint(self, name: str):
        if not self._model.existJointName(name):
            raise ValueError(f"G2 URDF is missing joint {name}")
        return self._model.joints[self._model.getJointId(name)]

    def reset(self, sides: tuple[str, ...] = ("left", "right")) -> None:
        sides = tuple(sides)
        if not sides or len(set(sides)) != len(sides):
            raise ValueError("IK reset sides must be unique and non-empty")
        for side in sides:
            if side not in self.ARM_JOINTS:
                raise ValueError(f"unsupported arm side: {side}")
            self._arm_seed[side] = None
            self._arm_commanded[side] = None
            self._active_sides.discard(side)
            self._hold_targets.pop(side, None)
            self._hold_recovery_limits.pop(side, None)
            self.last_error.pop(side, None)
        if not self._active_sides:
            self._body_seed = None
            self._body_commanded = None
            self._hold_targets.clear()
            self._hold_recovery_limits.clear()
            for side in self.ARM_JOINTS:
                self._arm_seed[side] = None
                self._arm_commanded[side] = None
        self.hold_errors.clear()

    def _positions(self, names: tuple[str, ...], state: RobotState):
        values = []
        for name in names:
            if name not in state.positions:
                raise ValueError(f"robot state is missing joint {name}")
            value = state.positions[name]
            if isinstance(value, bool):
                raise ValueError(f"robot state joint {name} must be finite")
            try:
                value = float(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"robot state joint {name} must be finite") from error
            if not math.isfinite(value):
                raise ValueError(f"robot state joint {name} must be finite")
            values.append(value)
        return self._np.asarray(values, dtype=float)

    def _full_configuration(self, state: RobotState):
        q = self._q_neutral.copy()
        for name, raw_position in state.positions.items():
            if not self._model.existJointName(name):
                continue
            if isinstance(raw_position, bool):
                raise ValueError(f"robot state joint {name} must be finite")
            try:
                position = float(raw_position)
            except (TypeError, ValueError) as error:
                raise ValueError(f"robot state joint {name} must be finite") from error
            if not math.isfinite(position):
                raise ValueError(f"robot state joint {name} must be finite")
            joint = self._model.joints[self._model.getJointId(name)]
            if joint.nq == 1:
                q[joint.idx_q] = position
            elif joint.nq == 2:
                q[joint.idx_q] = math.cos(position)
                q[joint.idx_q + 1] = math.sin(position)
        return q

    def _pose_from_transform(self, transform) -> CartesianPose:
        quaternion = self._pin.Quaternion(transform.rotation).coeffs()
        return CartesianPose(
            frame_id=self.BASE_FRAME,
            position_m=tuple(float(value) for value in transform.translation),
            orientation_xyzw=tuple(float(value) for value in quaternion),
        )

    def forward_kinematics(self, state: RobotState) -> Mapping[str, CartesianPose]:
        if not isinstance(state, RobotState):
            raise TypeError("state must be a RobotState")
        q = self._full_configuration(state)
        self._pin.framesForwardKinematics(self._model, self._data, q)
        base = self._data.oMf[self._base_frame_id]
        return {
            side: self._pose_from_transform(base.inverse() * self._data.oMf[frame_id])
            for side, frame_id in self._ee_frame_ids.items()
        }

    def _initialize_commands(self, active_sides, state: RobotState) -> None:
        if self._body_commanded is None:
            seed = self._positions(self.BODY_JOINTS, state)
            for name, value in zip(self.BODY_JOINTS, seed, strict=True):
                if not self._hard_lower[name] <= value <= self._hard_upper[name]:
                    raise ValueError(
                        f"body seed joint {name} is outside the safe IK range"
                    )
            self._body_seed = seed.copy()
            self._body_commanded = seed.copy()
        for side in active_sides:
            if self._arm_commanded[side] is None:
                seed = self._positions(self.ARM_JOINTS[side], state)
                self._arm_seed[side] = seed.copy()
                self._arm_commanded[side] = seed.copy()

    def _prepare_targets(self, intent, state):
        actual = self.forward_kinematics(state)
        active_sides = set(intent.targets)
        for side in self.ARM_JOINTS:
            if side in active_sides:
                self._hold_targets.pop(side, None)
                self._hold_recovery_limits.pop(side, None)
            elif side not in self._hold_targets:
                self._hold_targets[side] = actual[side]
            # On either clutch edge, use actual joints for the posture reference.
            if (side in active_sides) != (side in self._active_sides):
                self._arm_seed[side] = self._positions(self.ARM_JOINTS[side], state)
        self._active_sides = active_sides
        self.hold_errors.clear()
        return {**self._hold_targets, **intent.targets}

    def _body_excesses(self, positions):
        deltas = positions[:5] - self._body_seed
        excesses = [max(0.0, abs(float(deltas[i])) - self._body_deviation_limits[n])
                    for i, n in enumerate(self.BODY_JOINTS)]
        if self._combined_pitch_limit is not None:
            excesses.append(max(0.0, abs(float(self._np.sum(deltas[:3])))
                                - self._combined_pitch_limit))
        return excesses

    def _hold_pose_errors(self, q):
        np, pin = self._np, self._pin
        pin.framesForwardKinematics(self._model, self._data, q)
        errors = {}
        for side, target in self._hold_targets.items():
            transform = (self._data.oMf[self._base_frame_id].inverse()
                         * self._data.oMf[self._ee_frame_ids[side]])
            x, y, z, w = target.orientation_xyzw
            rotation = pin.Quaternion(w, x, y, z).matrix()
            errors[side] = (
                float(np.linalg.norm(transform.translation - np.asarray(target.position_m))),
                float(np.linalg.norm(pin.log3(transform.rotation.T @ rotation))),
            )
        return errors

    def _checked_hold_step(self, q, names, commanded, step, lower_step, upper_step):
        np, pin = self._np, self._pin
        self.hold_step_scale = 1.0
        if np.any(step < lower_step - 1e-12) or np.any(step > upper_step + 1e-12):
            raise ValueError(
                "upper-body candidate violates position/velocity bounds after "
                "combined pitch limiting"
            )
        unchecked_candidate = commanded + step
        if any(
            not self._hard_lower[name] <= value <= self._hard_upper[name]
            for name, value in zip(names, unchecked_candidate, strict=True)
        ):
            raise ValueError("upper-body candidate violates hard joint bounds")
        if not self._hold_targets:
            return unchecked_candidate
        tolerances = (self.HOLD_POSITION_TOLERANCE_M, self.HOLD_ROTATION_TOLERANCE_RAD)
        before_excesses = self._body_excesses(commanded)
        outside = max(before_excesses) > 1e-10
        observed_errors = self._hold_pose_errors(q)
        for side, error in observed_errors.items():
            if all(e <= t for e, t in zip(error, tolerances)):
                self._hold_recovery_limits.pop(side, None)
            elif outside and side not in self._hold_recovery_limits:
                self._hold_recovery_limits[side] = tuple(
                    max(e, t) for e, t in zip(error, tolerances)
                )
            if side in self._hold_recovery_limits:
                ceiling = self._hold_recovery_limits[side]
                if any(e > c + 1e-10 for e, c in zip(error, ceiling)):
                    raise UpperBodyHoldError('held-hand feedback worsened during boundary recovery')
                # Advance recovery from observed feedback, never from an unsent candidate.
                self._hold_recovery_limits[side] = tuple(
                    max(e, t) for e, t in zip(error, tolerances)
                )
        # Test the actual nonlinear FK, not only the linear Jacobian prediction.
        # Smaller steps preserve velocity/position bounds whenever zero is feasible.
        for scale in (1.0, 0.5, 0.25, 0.125, 0.0625, 0.0):
            if np.any(scale * step < lower_step - 1e-12) or np.any(scale * step > upper_step + 1e-12):
                continue
            candidate = commanded + scale * step
            if any(not self._hard_lower[n] <= v <= self._hard_upper[n]
                   for n, v in zip(names, candidate)):
                continue
            test_q = q.copy()
            for name, value in zip(names, candidate):
                test_q[self._idx_q[name]] = value
            errors = self._hold_pose_errors(test_q)
            after_excesses = self._body_excesses(candidate)
            if any(a > b + 1e-10 for a, b in zip(after_excesses, before_excesses)):
                continue
            if outside and not any(a < b - 1e-12 for a, b in zip(after_excesses, before_excesses)):
                continue
            acceptable = True
            for side, error in errors.items():
                if all(e <= t for e, t in zip(error, tolerances)):
                    continue
                ceiling = self._hold_recovery_limits.get(side)
                if (ceiling is None
                    or any(e > c + 1e-10 for e, c in zip(error, ceiling))
                    or not any(max(e, t) < c - 1e-12
                               for e, t, c in zip(error, tolerances, ceiling))):
                    acceptable = False
                    break
            if acceptable:
                self.hold_errors = errors
                self.hold_step_scale = scale
                return candidate
        raise UpperBodyHoldError(
            'upper-body hold exceeds 2 mm / 1 deg model tolerance; '
            'candidate rejected, original hold anchor retained'
        )

    def _limit_combined_pitch_step(self, commanded, step, lower_step, upper_step):
        """Clip the three Y-axis body joints to one aggregate pitch envelope."""

        if self._combined_pitch_limit is None:
            return step
        pitch_count = 3
        current = float(
            self._np.sum(commanded[:pitch_count] - self._body_seed[:pitch_count])
        )
        change = float(self._np.sum(step[:pitch_count]))
        if abs(current) > self._combined_pitch_limit + 1e-10:
            # A physical overshoot must not kill the session. Override the pitch
            # part with one bounded inward recovery step; other joints still use
            # the IK result. Distribute correction across available joint motion.
            direction = -1.0 if current > 0 else 1.0
            capacities = (
                -lower_step[:pitch_count]
                if direction < 0
                else upper_step[:pitch_count]
            )
            capacities = self._np.maximum(capacities, 0.0)
            total_capacity = float(self._np.sum(capacities))
            if total_capacity <= 1e-15:
                raise ValueError("upper-body IK cannot recover combined pitch envelope")
            correction = min(
                abs(current) - self._combined_pitch_limit,
                total_capacity,
            )
            limited = step.copy()
            limited[:pitch_count] = (
                direction * correction * capacities / total_capacity
            )
            return limited
        proposed = current + change
        if abs(proposed) <= self._combined_pitch_limit + 1e-12:
            return step
        if abs(change) <= 1e-15:
            raise ValueError("upper-body IK cannot satisfy combined pitch envelope")
        boundary = math.copysign(self._combined_pitch_limit, proposed)
        scale = max(0.0, min(1.0, (boundary - current) / change))
        limited = step.copy()
        limited[:pitch_count] *= scale
        return limited

    def solve(
        self,
        intent: EndEffectorPoseIntent,
        state: RobotState,
        *,
        sequence_id: int,
        now_ns: int,
        ttl_ms: int,
    ) -> JointCommand:
        if not isinstance(intent, EndEffectorPoseIntent):
            raise TypeError("intent must be an EndEffectorPoseIntent")
        if not isinstance(state, RobotState):
            raise TypeError("state must be a RobotState")
        active_sides = tuple(
            side for side in ("left", "right") if side in intent.targets
        )
        for side in active_sides:
            if intent.targets[side].frame_id != self.BASE_FRAME:
                raise ValueError(
                    f"{side} target must be expressed in {self.BASE_FRAME}"
                )

        all_sides = ("left", "right")
        targets = self._prepare_targets(intent, state)
        self._initialize_commands(all_sides, state)
        # Every solve starts at observed joints. Offline replay explicitly advances
        # RobotState; read-only preview never integrates unexecuted candidates.
        self._body_commanded = self._positions(self.BODY_JOINTS, state)
        for side in all_sides:
            self._arm_commanded[side] = self._positions(self.ARM_JOINTS[side], state)
        variable_names = self.BODY_JOINTS + tuple(
            name for side in all_sides for name in self.ARM_JOINTS[side]
        )
        commanded = self._np.concatenate(
            (self._body_commanded,)
            + tuple(self._arm_commanded[side] for side in all_sides)
        )
        seed = self._np.concatenate(
            (self._body_seed,)
            + tuple(self._arm_seed[side] for side in all_sides)
        )

        q = self._full_configuration(state)
        for name, value in zip(variable_names, commanded, strict=True):
            q[self._idx_q[name]] = value
        pin, np = self._pin, self._np
        pin.framesForwardKinematics(self._model, self._data, q)

        task_rows = []
        task_values = []
        cartesian_task_values = []
        velocity_indexes = np.asarray([self._idx_v[name] for name in variable_names])
        for side in all_sides:
            current = (
                self._data.oMf[self._base_frame_id].inverse()
                * self._data.oMf[self._ee_frame_ids[side]]
            )
            target = targets[side]
            qx, qy, qz, qw = target.orientation_xyzw
            target_transform = pin.SE3(
                pin.Quaternion(qw, qx, qy, qz).matrix(),
                np.asarray(target.position_m, dtype=float),
            )
            error = pin.log(current.actInv(target_transform)).vector
            self.last_error[side] = (
                float(np.linalg.norm(error[:3])),
                float(np.linalg.norm(error[3:])),
            )
            jacobian = pin.computeFrameJacobian(
                self._model,
                self._data,
                q,
                self._ee_frame_ids[side],
                pin.LOCAL,
            )[:, velocity_indexes]
            weights = np.ones(6)
            weights[3:] = self._rotation_weight
            if side in self._hold_targets:
                weights *= self.HOLD_TASK_WEIGHT
            task_rows.append(jacobian * weights[:, None])
            task_values.append(error * weights)
            # SE(3).log couples translation with rotation (a screw path). For
            # hand tracking, retain a straight Cartesian position direction and
            # an independent SO(3) orientation direction in the LOCAL frame.
            cartesian_error = np.concatenate((
                current.rotation.T @ (target_transform.translation-current.translation),
                pin.log3(current.rotation.T @ target_transform.rotation),
            ))
            cartesian_task_values.append(cartesian_error * weights)

        count = len(variable_names)
        regularization = np.full(count, self._arm_regularization)
        regularization[: len(self.BODY_JOINTS)] = self._body_regularization
        regularization[3] = self._body_roll_regularization
        regularization[4] = self._body_yaw_regularization
        regularization_sqrt = np.sqrt(regularization)
        task_rows.append(np.diag(regularization_sqrt))
        task_values.append(
            regularization_sqrt * self._posture_gain * self._dt * (seed - commanded)
        )
        task_rows.append(math.sqrt(self._damping) * np.eye(count))
        task_values.append(np.zeros(count))
        matrix = np.vstack(task_rows)
        values = np.concatenate(task_values)

        lower_step = []
        upper_step = []
        for index, name in enumerate(variable_names):
            info = self._schema.get(name)
            max_velocity = (
                self._body_velocity if name in self.BODY_JOINTS else self._arm_velocity
            )
            if info.max_velocity is not None:
                max_velocity = min(max_velocity, float(info.max_velocity))
            max_step = min(float(info.max_step), max_velocity * self._dt)
            position_lower = self._hard_lower[name]
            position_upper = self._hard_upper[name]
            if name in self.BODY_JOINTS:
                body_index = self.BODY_JOINTS.index(name)
                deviation = self._body_deviation_limits[name]
                position_lower = max(
                    position_lower,
                    float(self._body_seed[body_index]) - deviation,
                )
                position_upper = min(
                    position_upper,
                    float(self._body_seed[body_index]) + deviation,
                )
            if commanded[index] < position_lower:
                recovery = min(position_lower - commanded[index], max_step)
                lower_step.append(
                    recovery if recovery < max_step else max_step - 1e-12
                )
                upper_step.append(max_step)
            elif commanded[index] > position_upper:
                lower_step.append(-max_step)
                recovery = max(position_upper - commanded[index], -max_step)
                upper_step.append(
                    recovery if recovery > -max_step else -max_step + 1e-12
                )
            else:
                lower_step.append(max(position_lower - commanded[index], -max_step))
                upper_step.append(min(position_upper - commanded[index], max_step))

        lower_step = np.asarray(lower_step)
        upper_step = np.asarray(upper_step)
        if np.any(lower_step > upper_step):
            raise ValueError("upper-body IK has inconsistent position/velocity bounds")
        self.task_progress = {}
        if max(self._body_excesses(commanded)) <= 1e-10:
            from teleop.robots.task_priority_ik import bounded_task_step, TaskStepError
            inequality = None
            if self._combined_pitch_limit is not None:
                pitch_row = np.zeros(count)
                pitch_row[:3] = 1.
                pitch = float(np.sum(commanded[:3] - self._body_seed[:3]))
                inequality = (np.vstack((pitch_row, -pitch_row)), np.asarray([
                    self._combined_pitch_limit-pitch, self._combined_pitch_limit+pitch]))
            try:
                step, progress = bounded_task_step(
                    task_rows[:2], cartesian_task_values, lower_step, upper_step,
                    regularization + self._damping,
                    self._posture_gain * self._dt * (seed-commanded),
                    priorities=[self.HOLD_TASK_WEIGHT if side in self._hold_targets else 1.
                                for side in all_sides], inequality=inequality)
            except TaskStepError as error:
                raise UpperBodyHoldError(str(error)) from error
            self.task_progress = dict(zip(all_sides, progress))
        else:
            # Preserve the independently tested monotonic boundary recovery path.
            result = self._lsq_linear(matrix, values, bounds=(lower_step, upper_step),
                                     method="bvls", tol=1.0e-10, max_iter=100)
            if not result.success or not np.all(np.isfinite(result.x)):
                raise ValueError(f"upper-body IK failed: {result.message}")
            step = result.x
        bounded_step = self._limit_combined_pitch_step(
            commanded, step, lower_step, upper_step
        )
        next_positions = self._checked_hold_step(
            q, variable_names, commanded, bounded_step, lower_step, upper_step
        )

        body_count = len(self.BODY_JOINTS)
        self._body_commanded = next_positions[:body_count].copy()
        offset = body_count
        for side in all_sides:
            arm_count = len(self.ARM_JOINTS[side])
            self._arm_commanded[side] = next_positions[offset : offset + arm_count].copy()
            offset += arm_count

        return JointCommand(
            targets={
                name: float(value)
                for name, value in zip(variable_names, next_positions, strict=True)
            },
            source=intent.source,
            sequence_id=sequence_id,
            created_time_ns=now_ns,
            ttl_ms=ttl_ms,
        )
