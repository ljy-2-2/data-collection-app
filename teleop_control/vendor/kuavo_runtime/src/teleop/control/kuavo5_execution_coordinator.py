"""Coordinate Kuavo candidate preview and a memory-recording execution session.

This coordinator itself has no robot transport: would-be publications and hold
requests are stored in memory.  Candidate generation is injected, however, so
this reusable class does NOT attest that an arbitrary preview backend is free of
external I/O.  Only a concrete composition that owns and audits that backend may
make such a claim (see the synthetic offline demo).
"""

from dataclasses import dataclass

from teleop.contracts.xr import XRFrame
from teleop.control.kuavo5_arm_execution_session import (
    ArmExecutionSession,
    ExecutionResult,
    GripSample,
)
from teleop.control.kuavo5_native_preview import (
    KuavoNativePreviewPipeline,
    KuavoNativePreviewResult,
)
from teleop.robots.kuavo5 import Kuavo5Adapter
from teleop.robots.kuavo5_arm_joint_encoder import KuavoArmJointEncoding
from teleop.robots.kuavo5_native_ik import ARM_NAMES


@dataclass(frozen=True)
class KuavoExecutionCycleResult:
    preview: KuavoNativePreviewResult
    execution: ExecutionResult
    active_sides: tuple[str, ...]
    candidate_offered: bool = False


class KuavoExecutionCoordinator:
    """Join preview and safety state machine; record final encodings in memory."""

    def __init__(self, robot: Kuavo5Adapter,
                 preview: KuavoNativePreviewPipeline, schema, *, clock,
                 **session_options):
        if not isinstance(robot, Kuavo5Adapter):
            raise TypeError('robot must be a Kuavo5Adapter')
        if not isinstance(preview, KuavoNativePreviewPipeline):
            raise TypeError('preview must be a KuavoNativePreviewPipeline')
        if preview.robot is not robot:
            raise ValueError('preview and coordinator must use the same robot')
        if schema is not robot.get_joint_schema():
            raise ValueError('coordinator must use the robot schema instance')
        if tuple(schema.names) != ARM_NAMES:
            raise ValueError('fixed left7/right7 schema required')
        if not callable(clock):
            raise ValueError('an explicit trusted monotonic clock is required')
        self._robot = robot
        self._preview = preview
        self._clock = clock
        self._memory_commands: list[KuavoArmJointEncoding] = []
        self._memory_holds: list[str] = []
        self._session = ArmExecutionSession(
            schema,
            publish=self._memory_commands.append,
            hold=self._memory_holds.append,
            **session_options,
        )

    @property
    def fake_commands(self):
        return tuple(self._memory_commands)

    @property
    def fake_holds(self):
        return tuple(self._memory_holds)

    def step(self, frame: XRFrame, *, now_ns: int) -> KuavoExecutionCycleResult:
        if not isinstance(frame, XRFrame):
            raise TypeError('frame must be XRFrame')
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError('now_ns must be a non-negative integer')

        preview = self._preview.preview(frame, now_ns=now_ns)
        # IK may be slow. Re-sample the trusted clock so input/candidate age is
        # judged at completion rather than frozen at the start of computation.
        completed_ns = self._clock_now(not_before=now_ns)
        if preview.status == 'stale':
            epoch = self._session.invalidate_input(
                'XR frame marked stale', now_ns=completed_ns)
        elif preview.status == 'unchanged':
            # Polling the SDK cache is not a new source frame. Advance execution
            # and its existing watchdog deadline without renewing the input lease.
            epoch = self._session.epoch
        else:
            epoch = self._session.observe_input(
                GripSample(frame.sequence_id, frame.received_monotonic_ns,
                           preview.active_sides),
                now_ns=completed_ns,
            )
        offered = False
        if preview.status == 'preview':
            offered = self._session.offer(preview.command, epoch=epoch)
        elif preview.status == 'rejected':
            self._session.reject_candidate(
                sequence_id=frame.sequence_id,
                epoch=epoch,
                reason=preview.rejection_reason,
            )

        execution = self._tick_at(completed_ns)
        return KuavoExecutionCycleResult(
            preview=preview,
            execution=execution,
            active_sides=preview.active_sides,
            candidate_offered=offered,
        )

    def _clock_now(self, *, not_before=None):
        now_ns = self._clock()
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError('trusted clock must return a non-negative integer')
        if not_before is not None and now_ns < not_before:
            raise ValueError('trusted clock moved backwards during preview')
        return now_ns

    def _tick_at(self, now_ns):
        feedback = self._robot.read_state(now_ns=now_ns)
        return self._session.tick(feedback, now_ns=now_ns)

    def tick(self) -> ExecutionResult:
        """Advance the independent control clock without accepting a new frame."""
        return self._tick_at(self._clock_now())

    def invalidate_input(self, reason: str, *, now_ns: int) -> ExecutionResult:
        """Latch an externally observed input-stream fault without replaying a goal."""
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError('now_ns must be a non-negative integer')
        completed_ns = self._clock_now(not_before=now_ns)
        self._session.invalidate_input(reason, now_ns=completed_ns)
        return self._tick_at(completed_ns)

    def close(self):
        return self._session.close()
