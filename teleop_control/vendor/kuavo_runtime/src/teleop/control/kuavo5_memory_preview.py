"""PICO-frame processing against synthetic Kuavo-shaped memory state only.

The kinematics below are a reversible pose encoding used to exercise the
official FK/IK protocol shape. They are NOT Leju kinematics and prove nothing
about physical reachability. This module has no PICO SDK, SSH, ROS or robot
transport import; a separate app owns the optional read-only PICO connection.
"""

import math
from types import SimpleNamespace

from teleop.backends.gripper import GripperBackend
from teleop.contracts.schema import JointInfo, JointSchema
from teleop.contracts.xr import XRFrame
from teleop.control.gripper_safety import GripperSafetyGateway
from teleop.control.kuavo5_execution_coordinator import KuavoExecutionCoordinator
from teleop.control.kuavo5_native_preview import KuavoNativePreviewPipeline
from teleop.retargeting.dual_arm import DualArmRetargeter
from teleop.retargeting.gripper import GripperMapper
from teleop.robots.kuavo5 import Kuavo5Adapter
from teleop.robots.kuavo5_native_ik import ARM_NAMES, KuavoNativeIKSolver
from teleop.robots.kuavo5_native_rpc_backend import KuavoNativeRPCBackend


def _unit_quaternion(values):
    values = tuple(map(float, values))
    norm = math.sqrt(sum(value*value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError('synthetic quaternion must have positive finite norm')
    return [value/norm for value in values]


class SyntheticKuavoPoseRPC:
    """Reversible 7-values-per-hand protocol double; never external I/O."""

    def __init__(self):
        self.reject_ik = False

    def call(self, operation, request):
        if operation == 'fk':
            q = tuple(request['q'])
            if len(q) != 14:
                raise ValueError('synthetic FK requires 14 values')
            hands = {}
            for side, offset, base_y in (('left', 0, .2), ('right', 7, -.2)):
                hands[side+'_pose'] = {
                    'pos_xyz': [.4+q[offset], base_y+q[offset+1], .5+q[offset+2]],
                    'quat_xyzw': _unit_quaternion(q[offset+3:offset+7]),
                }
            return {'success': True, 'hand_poses': hands}
        if operation == 'ik':
            if self.reject_ik:
                return {'success': False}
            q = [0.] * 14
            for side, offset, base_y in (('left', 0, .2), ('right', 7, -.2)):
                pose = request['hand_poses'][side+'_pose']
                position = tuple(map(float, pose['pos_xyz']))
                quaternion = _unit_quaternion(pose['quat_xyzw'])
                q[offset:offset+3] = [position[0]-.4, position[1]-base_y,
                                      position[2]-.5]
                q[offset+3:offset+7] = quaternion
            return {'success': True, 'with_torso': False,
                    'q_torso': [], 'q_arm': q}
        raise ValueError('synthetic RPC only supports FK and IK')


def make_synthetic_schema():
    return JointSchema(tuple(
        JointInfo(name, -2., 2., 0., False) for name in ARM_NAMES))


def make_synthetic_initial_positions():
    positions = dict.fromkeys(ARM_NAMES, 0.)
    positions[ARM_NAMES[6]] = 1.
    positions[ARM_NAMES[13]] = 1.
    return positions


class KuavoMemoryPreviewRuntime:
    """Consume XRFrame values and apply would-be commands to a memory snapshot."""

    INPUT_TIMEOUT_NS = 100_000_000

    def __init__(self, *, clock):
        if not callable(clock):
            raise ValueError('an explicit trusted monotonic clock is required')
        self._clock = clock
        self.schema = make_synthetic_schema()
        self._initial = make_synthetic_initial_positions()
        self._positions = dict(self._initial)
        self._gripper_positions = {'left': 0., 'right': 0.}
        self._gripper_commands = []
        self._last_gripper_sequence = -1
        self._last_gripper_received_ns = -1
        self._last_feedback_ns = -1
        self.rpc = SyntheticKuavoPoseRPC()
        backend = KuavoNativeRPCBackend(self.rpc, frame_id='base_link')
        solver = KuavoNativeIKSolver(
            self.schema, backend, frame_id='base_link', frame_mode=0,
            clock=clock)
        self.robot = Kuavo5Adapter(
            self.schema, robot_version='synthetic-63-protocol', ik_solver=solver)
        preview = KuavoNativePreviewPipeline(self.robot, DualArmRetargeter())
        self.coordinator = KuavoExecutionCoordinator(
            self.robot, preview, self.schema, clock=clock)
        self._gripper_mapper = GripperMapper()
        self._gripper_mapper.prime(self._gripper_positions)
        self._gripper_backend = GripperBackend(default_ttl_ms=100)
        self._gripper_safety = GripperSafetyGateway()

    @property
    def initial_positions(self):
        return dict(self._initial)

    @property
    def positions(self):
        return dict(self._positions)

    @property
    def fake_commands(self):
        return self.coordinator.fake_commands

    @property
    def fake_holds(self):
        return self.coordinator.fake_holds

    @property
    def fake_gripper_commands(self):
        return tuple(self._gripper_commands)

    @property
    def gripper_positions(self):
        return dict(self._gripper_positions)

    @property
    def input_timeout_ns(self):
        return self.INPUT_TIMEOUT_NS

    def _accept_feedback(self, now_ns):
        if type(now_ns) is not int or now_ns < 0 \
                or now_ns <= self._last_feedback_ns:
            raise ValueError('feedback clock must strictly increase')
        self.robot.accept_sdk_joint_state(
            SimpleNamespace(position=[self._positions[name] for name in ARM_NAMES]),
            joint_names=ARM_NAMES,
            robot_version='synthetic-63-protocol',
            received_monotonic_ns=now_ns)
        self._last_feedback_ns = now_ns

    def step(self, frame: XRFrame, *, now_ns: int):
        if not isinstance(frame, XRFrame):
            raise TypeError('frame must be XRFrame')
        self._accept_feedback(now_ns)
        before = len(self.fake_commands)
        result = self.coordinator.step(frame, now_ns=now_ns)
        if len(self.fake_commands) > before:
            encoded = self.fake_commands[-1]
            self._positions = dict(zip(
                encoded.names, (math.radians(v) for v in encoded.positions_deg)))
        self._update_grippers(frame, now_ns=now_ns)
        return result

    def _update_grippers(self, frame, *, now_ns):
        sequence_ordered = frame.sequence_id > self._last_gripper_sequence
        time_ordered = frame.received_monotonic_ns >= self._last_gripper_received_ns
        fresh = 0 <= now_ns-frame.received_monotonic_ns < self.INPUT_TIMEOUT_NS
        if frame.sequence_id > self._last_gripper_sequence:
            self._last_gripper_sequence = frame.sequence_id
        if frame.stale or not sequence_ordered or not time_ordered or not fresh:
            self._gripper_mapper.prime(self._gripper_positions)
            return
        self._last_gripper_received_ns = frame.received_monotonic_ns
        intent = self._gripper_mapper.propose(
            {'left': frame.left_controller.trigger,
             'right': frame.right_controller.trigger},
            now_ns=now_ns,
        )
        if intent is None:
            return
        command = self._gripper_backend.build_command(
            intent, sequence_id=frame.sequence_id, now_ns=now_ns)
        validated = self._gripper_safety.validate(command, now_ns=now_ns)
        self._gripper_commands.append(validated)
        self._gripper_mapper.commit(intent, now_ns=now_ns)
        self._gripper_positions.update(validated.targets)

    def invalidate_input(self, reason: str, *, now_ns: int):
        """Latch a PICO stream timeout using fresh memory-only feedback."""
        self._accept_feedback(now_ns)
        self._gripper_mapper.prime(self._gripper_positions)
        return self.coordinator.invalidate_input(reason, now_ns=now_ns)

    def close(self):
        return self.coordinator.close()
