"""G2 dual-arm URDF IK adapted from the proven legacy ``test-teleop/ik.py``."""

from collections.abc import Mapping
import math
from pathlib import Path

from teleop.contracts.command import JointCommand
from teleop.contracts.intent import CartesianPose, EndEffectorPoseIntent
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointSchema


class G2URDFIKSolver:
    """One damped-least-squares step per selected G2 arm and control tick."""

    kind = "urdf"
    BASE_FRAME = "base_link"
    EE_FRAMES = {"left": "arm_l_end_link", "right": "arm_r_end_link"}
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
        urdf_path: str | Path | None = None,
        max_joint_velocity_rad_s: float = 0.2,
        control_period_s: float = 0.01,
        damping: float = 1.0e-3,
        rotation_weight: float = 0.5,
        posture_gain: float = 0.5,
        limit_margin_rad: float = 0.02,
    ) -> None:
        if not isinstance(joint_schema, JointSchema):
            raise TypeError("joint_schema must be a JointSchema")
        numeric = {
            "max_joint_velocity_rad_s": max_joint_velocity_rad_s,
            "control_period_s": control_period_s,
            "damping": damping,
            "rotation_weight": rotation_weight,
            "posture_gain": posture_gain,
            "limit_margin_rad": limit_margin_rad,
        }
        for name, value in numeric.items():
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if max_joint_velocity_rad_s <= 0 or control_period_s <= 0 or damping <= 0:
            raise ValueError("velocity, control period, and damping must be positive")
        if rotation_weight <= 0 or posture_gain < 0 or limit_margin_rad < 0:
            raise ValueError("IK weights and limit margin are invalid")

        try:
            import numpy as np
            import pinocchio as pin
        except ImportError as error:
            raise RuntimeError(
                "G2 URDF IK requires numpy and pinocchio; use the "
                "g2-teleop-py310 environment"
            ) from error

        self._np = np
        self._pin = pin
        self._schema = joint_schema
        self._max_velocity = float(max_joint_velocity_rad_s)
        self._dt = float(control_period_s)
        self._damping = float(damping)
        self._rotation_weight = float(rotation_weight)
        self._posture_gain = float(posture_gain)
        self._limit_margin = float(limit_margin_rad)
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
        self._idx_q = {}
        self._idx_v = {}
        self._lower = {}
        self._upper = {}
        for side, names in self.ARM_JOINTS.items():
            joints = [self._required_joint(name) for name in names]
            self._idx_q[side] = np.asarray([joint.idx_q for joint in joints])
            self._idx_v[side] = np.asarray([joint.idx_v for joint in joints])
            lower = self._model.lowerPositionLimit[self._idx_q[side]].copy()
            upper = self._model.upperPositionLimit[self._idx_q[side]].copy()
            for index, name in enumerate(names):
                info = joint_schema.get(name)
                lower[index] = max(lower[index], info.position_min)
                upper[index] = min(upper[index], info.position_max)
            lower += self._limit_margin
            upper -= self._limit_margin
            if np.any(lower > upper):
                raise ValueError(f"{side} arm has no valid IK range after margin")
            self._lower[side] = lower
            self._upper[side] = upper

        self._commanded = {"left": None, "right": None}
        self._seed = {"left": None, "right": None}
        self.last_error = {}

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
        for side in sides:
            if side not in self.ARM_JOINTS:
                raise ValueError(f"unsupported arm side: {side}")
            self._commanded[side] = None
            self._seed[side] = None
            self.last_error.pop(side, None)

    def _arm_positions(self, side: str, state: RobotState):
        missing = [name for name in self.ARM_JOINTS[side] if name not in state.positions]
        if missing:
            raise ValueError(f"robot state is missing {side} arm joints: {missing}")
        values = []
        for name in self.ARM_JOINTS[side]:
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

        targets = {}
        for side in ("left", "right"):
            target = intent.targets.get(side)
            if target is None:
                continue
            if target.frame_id != self.BASE_FRAME:
                raise ValueError(
                    f"{side} target must be expressed in {self.BASE_FRAME}"
                )
            targets.update(self._solve_side(side, target, state))

        return JointCommand(
            targets=targets,
            source=intent.source,
            sequence_id=sequence_id,
            created_time_ns=now_ns,
            ttl_ms=ttl_ms,
        )

    def _solve_side(
        self,
        side: str,
        target: CartesianPose,
        state: RobotState,
    ) -> dict[str, float]:
        np, pin = self._np, self._pin
        if self._commanded[side] is None:
            seed = self._arm_positions(side, state)
            self._commanded[side] = seed.copy()
            self._seed[side] = seed.copy()

        q_commanded = self._commanded[side]
        q = self._full_configuration(state)
        q[self._idx_q[side]] = q_commanded
        pin.framesForwardKinematics(self._model, self._data, q)
        current = (
            self._data.oMf[self._base_frame_id].inverse()
            * self._data.oMf[self._ee_frame_ids[side]]
        )
        qx, qy, qz, qw = target.orientation_xyzw
        target_rotation = pin.Quaternion(qw, qx, qy, qz).matrix()
        target_transform = pin.SE3(
            target_rotation,
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
        )[:, self._idx_v[side]]
        weights = np.ones(6)
        weights[3:] = self._rotation_weight
        weighted_jacobian = jacobian * weights[:, None]
        pseudo_inverse = weighted_jacobian.T @ np.linalg.inv(
            weighted_jacobian @ weighted_jacobian.T
            + self._damping * np.eye(6)
        )
        delta = pseudo_inverse @ (error * weights)
        nullspace = np.eye(len(delta)) - pseudo_inverse @ weighted_jacobian
        delta += nullspace @ (
            self._posture_gain
            * self._dt
            * (self._seed[side] - q_commanded)
        )
        if not np.all(np.isfinite(delta)):
            raise ValueError(f"{side} IK produced a non-finite joint step")

        max_step = self._max_velocity * self._dt
        biggest = float(np.max(np.abs(delta)))
        if biggest > max_step:
            delta *= max_step / biggest
        q_commanded = np.clip(
            q_commanded + delta,
            self._lower[side],
            self._upper[side],
        )
        self._commanded[side] = q_commanded
        return {
            name: float(value)
            for name, value in zip(self.ARM_JOINTS[side], q_commanded)
        }
