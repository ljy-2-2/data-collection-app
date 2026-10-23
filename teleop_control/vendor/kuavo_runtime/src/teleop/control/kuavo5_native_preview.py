"""Kuavo PICO/native-IK candidate preview; no robot-send code path."""

from dataclasses import dataclass
from typing import Literal

from teleop.contracts.command import JointCommand
from teleop.contracts.intent import EndEffectorPoseIntent
from teleop.contracts.robot_state import RobotState
from teleop.contracts.xr import XRFrame
from teleop.retargeting.dual_arm import DualArmRetargeter
from teleop.robots.kuavo5 import Kuavo5Adapter
from teleop.robots.kuavo5_native_ik import ARM_NAMES, IKCandidateRejected


@dataclass(frozen=True)
class KuavoNativePreviewResult:
    status: Literal["stale", "unchanged", "idle", "preview", "rejected"]
    frame_sequence_id: int
    active_sides: tuple[str, ...]
    command: JointCommand | None = None
    target_positions_m: tuple[tuple[str, tuple[float, float, float]], ...] = ()
    # The same immutable feedback snapshot used as this candidate's IK seed.
    reference_positions_rad: tuple[tuple[str, float], ...] = ()
    actual_positions_rad: tuple[tuple[str, float], ...] = ()
    rejection_reason: str = ""


class KuavoNativePreviewPipeline:
    """Map an XR frame through an injected official-IK backend, never send it.

    The observed Kuavo schema remains read-only. Its calculated candidate is
    intentionally not a ValidatedJointCommand or permission to actuate.
    """

    def __init__(self, robot: Kuavo5Adapter, retargeter: DualArmRetargeter, *,
                 max_frame_age_ms: int = 100, candidate_ttl_ms: int = 200,
                 use_measured_state: bool = False):
        if not isinstance(robot, Kuavo5Adapter) or not robot.has_ik_solver:
            raise ValueError("a read-only Kuavo adapter with an injected IK solver is required")
        if not isinstance(retargeter, DualArmRetargeter):
            raise TypeError("retargeter must be a DualArmRetargeter")
        if type(max_frame_age_ms) is not int or max_frame_age_ms <= 0:
            raise ValueError("max_frame_age_ms must be a positive integer")
        if type(candidate_ttl_ms) is not int or candidate_ttl_ms <= 0:
            raise ValueError("candidate_ttl_ms must be a positive integer")
        if type(use_measured_state) is not bool:
            raise ValueError("use_measured_state must be boolean")
        self._robot = robot
        self._retargeter = retargeter
        self._max_frame_age_ns = max_frame_age_ms * 1_000_000
        self._candidate_ttl_ms = candidate_ttl_ms
        # Virtual propagation is for a stationary no-send preview only. A future
        # execution composition MUST use measured feedback, not assume a previous
        # unsent/rejected/rate-limited IK candidate has already been executed.
        self._use_measured_state = use_measured_state
        self._virtual_arm_positions = None
        self._last_device_timestamp_ns = 0
        self._last_device_received_ns = None
        self._source_invalid = False
        self._last_active_sides = ()

    @property
    def robot(self):
        """The exact adapter whose feedback and IK seed this preview uses."""
        return self._robot

    def reset(self):
        # Reset clutch/IK state, not the device-clock high-water mark. A cached
        # packet must not become new input after a reset or stream fault.
        self._retargeter.declutch()
        self._robot.reset_ik(("left", "right"))
        self._virtual_arm_positions = None
        self._last_active_sides = ()

    def _source_status(self, frame, now_ns):
        previous_timestamp = self._last_device_timestamp_ns
        if frame.device_timestamp_ns > previous_timestamp:
            # SDK polls receive new local sequence numbers even when the source
            # timestamp has not advanced. Only actual source progress renews age.
            self._last_device_timestamp_ns = frame.device_timestamp_ns
            self._last_device_received_ns = frame.received_monotonic_ns
            self._source_invalid = False
        age = now_ns-frame.received_monotonic_ns
        source_age = now_ns-self._last_device_received_ns
        if frame.stale or not 0 <= age < self._max_frame_age_ns \
                or not 0 <= source_age < self._max_frame_age_ns \
                or frame.device_timestamp_ns < previous_timestamp or self._source_invalid:
            self._source_invalid = True
            self.reset()
            return 'stale'
        if frame.device_timestamp_ns == previous_timestamp:
            return 'unchanged'
        return None

    def preview(self, frame: XRFrame, *, now_ns: int) -> KuavoNativePreviewResult:
        if not isinstance(frame, XRFrame):
            raise TypeError("frame must be XRFrame")
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a non-negative integer")
        source_status = self._source_status(frame, now_ns)
        if source_status is not None:
            return KuavoNativePreviewResult(source_status, frame.sequence_id,
                                            self._last_active_sides)

        actual_state = self._robot.read_state(now_ns=now_ns)
        state = actual_state
        if not self._use_measured_state and self._virtual_arm_positions is not None:
            virtual_positions = dict(actual_state.positions)
            virtual_positions.update(self._virtual_arm_positions)
            state = RobotState(virtual_positions, now_ns)
        anchors = None

        def get_anchor(side):
            nonlocal anchors
            if anchors is None:
                anchors = self._robot.get_end_effector_poses(state)
            return anchors.get(side)

        retargeting = self._retargeter.update(frame, get_anchor)
        if retargeting.intent is None:
            self._virtual_arm_positions = None
            self._last_active_sides = ()
            return KuavoNativePreviewResult("idle", frame.sequence_id, ())

        active_targets = retargeting.intent.targets
        self._last_active_sides = tuple(active_targets)
        ik_targets = dict(active_targets)
        if len(ik_targets) == 1:
            # Hold the ungripped hand in robot space. Reuse the FK already read
            # for clutch anchoring rather than requesting the same FK twice.
            other_side = "right" if "left" in ik_targets else "left"
            other_pose = get_anchor(other_side)
            if other_pose is None:
                raise ValueError(f"current {other_side} end-effector pose unavailable")
            ik_targets[other_side] = other_pose
        ik_intent = EndEffectorPoseIntent(ik_targets, source=retargeting.intent.source)
        try:
            command = self._robot.solve_ik(ik_intent, state,
                                           sequence_id=frame.sequence_id, now_ns=now_ns,
                                           ttl_ms=self._candidate_ttl_ms)
        except IKCandidateRejected as exc:
            return KuavoNativePreviewResult(
                "rejected", frame.sequence_id, tuple(active_targets),
                target_positions_m=tuple((side, pose.position_m)
                                         for side, pose in active_targets.items()),
                rejection_reason=str(exc),
            )
        if tuple(command.targets) != ARM_NAMES:
            raise ValueError("official IK candidate must contain the audited 14 arm joints")
        if not self._use_measured_state:
            self._virtual_arm_positions = dict(command.targets)
        return KuavoNativePreviewResult("preview", frame.sequence_id,
                                        tuple(active_targets), command,
                                        tuple((side, pose.position_m)
                                              for side, pose in active_targets.items()),
                                        reference_positions_rad=tuple(
                                            (name, state.positions[name]) for name in ARM_NAMES),
                                        actual_positions_rad=tuple(
                                            (name, actual_state.positions[name]) for name in ARM_NAMES))
