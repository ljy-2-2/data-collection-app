"""Bounded, JSON-safe execution evidence; no I/O, ROS imports or robot calls."""
import math
import threading
import time
from numbers import Real
from collections.abc import Mapping


class SuccessTraceSampler:
    """Limit large successful traces on the shared input/diagnostic stream.

    This samples evidence, never commands or input. Errors, state transitions,
    challenges and CAN feedback are outside the whitelist and always pass.
    Count omissions explicitly; this stream is not a full-rate dataset.
    """

    def __init__(self, *, clock=time.monotonic_ns):
        self._clock = clock
        self._period_ns = 100_000_000  # Each successful trace type: at most 10 Hz.
        self._last = {}
        self._skipped = dict(ik_trace=0, control_trace=0)
        self._total = dict(self._skipped)
        self._lock = threading.Lock()

    def select(self, packet):
        event = packet.get('event')
        if event not in self._total:
            return packet
        with self._lock:
            now = self._clock()
            last = self._last.get(event)
            if last is not None and now-last < self._period_ns:
                self._skipped[event] += 1
                self._total[event] += 1
                return None
            self._last[event] = now
            skipped, self._skipped[event] = self._skipped[event], 0
            return dict(packet, diagnostic_sampling_hz=10,
                        diagnostic_samples_omitted_before=skipped)

    def status(self):
        with self._lock:
            return dict(policy='successful_arm_traces_10hz',
                        omitted_by_event=dict(self._total),
                        full_rate_dataset=False)


def scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Real):
        try:
            return value if math.isfinite(value) else str(value)
        except (OverflowError, ValueError, TypeError):
            return {'invalid_number': 'not_representable_as_float',
                    'type': type(value).__name__[:64]}
    if isinstance(value, str):
        return value[:256]
    return {'invalid_type': type(value).__name__[:64]}


def vector(value):
    if not isinstance(value, (tuple, list)):
        return scalar(value)
    result = [scalar(v) for v in value[:32]]
    return result if len(value) <= 32 else {'values': result, 'length': len(value), 'truncated': True}


def poses(values):
    if values is None:
        return None
    return {side: {'frame_id': pose.frame_id[:256], 'position_m': list(pose.position_m),
                   'orientation_xyzw': list(pose.orientation_xyzw)}
            for side, pose in values.items()}


def response_fields(response):
    """Only bounded official scalar/joint fields; never retain the ROS object."""
    if isinstance(response, Mapping):
        return {k: (vector(response.get(k)) if k in ('q_arm', 'q_torso') else scalar(response.get(k)))
                for k in ('success', 'with_torso', 'q_arm', 'q_torso', 'time_cost')}
    return scalar(response)


def request_fields(request):
    if request is None:
        return None
    copied = {k: scalar(request[k]) for k in
              ('frame', 'joint_angles_as_q0', 'use_custom_ik_param')}
    if request.get('use_custom_ik_param') is True:
        params = request.get('ik_param')
        copied['ik_param'] = ({str(k)[:64]: scalar(v)
                               for k, v in list(params.items())[:16]}
                              if isinstance(params, Mapping) else scalar(params))
    copied['hand_poses'] = {side: {k: vector(hand[k]) for k in
        ('pos_xyz', 'quat_xyzw', 'elbow_pos_xyz', 'joint_angles')}
        for side, hand in request['hand_poses'].items()}
    return copied


def feedback_fields(names, state):
    """Bounded measured posture, distinct from a numerical initial guess."""
    positions = getattr(state, 'positions', None)
    if not isinstance(positions, Mapping):
        return None
    return vector([positions.get(n) for n in names])


def target_progress_fields(context):
    """Bounded proposal evidence; raw hand goals are not FK validation goals."""
    value = (context or {}).get('target_progress')
    if not isinstance(value, Mapping):
        return None
    result = {key: scalar(value.get(key)) for key in
              ('policy', 'token_id', 'failure_rebase_policy')}
    if value.get('policy') in ('fixed_anchor_bounded_latest_target', 'fixed_anchor_time_budget_latest_target'):
        for key in ('max_position_step_m', 'max_orientation_step_rad'):
            result[key] = scalar(value.get(key))
        for key in ('position_remaining_m', 'orientation_remaining_deg', 'numerical_step_scales'):
            sides = value.get(key)
            result[key] = {side: scalar(sides.get(side)) for side in ('left', 'right')
                           if side in sides} if isinstance(sides, Mapping) else None
    if value.get('policy') == 'fixed_anchor_time_budget_latest_target':
        for key in ('target_budget_cap_s', 'target_linear_rate_m_s', 'target_angular_rate_rad_s'):
            result[key] = scalar(value.get(key))
        budgets = value.get('target_budget_s')
        result['target_budget_s'] = {side: scalar(budgets.get(side)) for side in ('left', 'right')
            if side in budgets} if isinstance(budgets, Mapping) else None
    target_keys = ('raw_targets', 'effective_targets')
    workspace = value.get('workspace_targets')
    if (value.get('policy') in ('fixed_anchor_bounded_latest_target', 'fixed_anchor_time_budget_latest_target')
            and isinstance(workspace, Mapping) and workspace):
        target_keys += ('workspace_targets',)
        for key in ('workspace_policy', 'workspace_soft_margin_m', 'workspace_margin_origin'):
            result[key] = scalar(value.get(key))
        margins = value.get('workspace_effective_margin_m')
        result['workspace_effective_margin_m'] = {
            side: scalar(margins.get(side)) for side in ('left', 'right') if side in margins
        } if isinstance(margins, Mapping) else None
        constraints = value.get('workspace_constraints')
        result['workspace_constraints'] = {
            key: scalar(constraints.get(key)) for key in (
                'raw_clutch_anchor_preserved', 'full_ik_feasibility_proven',
                'collision_free_path_proven', 'orientation_change_can_shift_projected_position',
                'far_outside_sensitivity_tends_to_zero')
        } if isinstance(constraints, Mapping) else None
    for key in target_keys:
        targets = value.get(key)
        if not isinstance(targets, Mapping):
            result[key] = None
            continue
        result[key] = {}
        for side in ('left', 'right'):
            pose = targets.get(side)
            if isinstance(pose, Mapping):
                result[key][side] = dict(frame_id=scalar(pose.get('frame_id')),
                    position_m=vector(pose.get('position_m')),
                    orientation_xyzw=vector(pose.get('orientation_xyzw')))
    result['last_failure_rebase'] = {}
    sources = value.get('last_failure_rebase')
    if isinstance(sources, Mapping):
        for side in ('left', 'right'):
            source = sources.get(side)
            if not isinstance(source, Mapping):
                continue
            pose = source.get('qualified_pose')
            if isinstance(pose, Mapping):
                result['last_failure_rebase'][side] = dict(
                    accepted_token_id=scalar(source.get('accepted_token_id')),
                    failure_token_id=scalar(source.get('failure_token_id')),
                    qualified_pose=dict(frame_id=scalar(pose.get('frame_id')),
                        position_m=vector(pose.get('position_m')),
                        orientation_xyzw=vector(pose.get('orientation_xyzw'))))
    return result


def success_snapshot(names, *, sequence_id, now_ns, completed_ns, state, context,
                     identity, seed, request, response, actual, inverse_started_ns,
                     inverse_completed_ns):
    return dict(event='ik_trace', schema_version=1, source_sequence_id=sequence_id,
        context={k: scalar((context or {}).get(k)) for k in
                 ('epoch', 'input_created_time_ns', 'device_timestamp_ns')},
        target_progress=target_progress_fields(context),
        started_monotonic_ns=now_ns, completed_monotonic_ns=completed_ns,
        elapsed_ms=(completed_ns-now_ns)/1e6,
        timing_scope='work_started_to_accepted_solution',
        inverse_started_monotonic_ns=inverse_started_ns,
        inverse_completed_monotonic_ns=inverse_completed_ns,
        inverse_elapsed_ms=(inverse_completed_ns-inverse_started_ns)/1e6,
        response_time_cost_unit='ms', feedback_timestamp_ns=state.timestamp_ns,
        seed_rad=vector(seed), feedback_rad=feedback_fields(names, state),
        joint_order=list(names), request=request_fields(request),
        response=response_fields(response), actual_poses=poses(actual),
        model_identity={str(k)[:64]: scalar(v) for k, v in list((identity or {}).items())[:8]},
        execution_status='not_yet_offered_to_control_session',
        physical_execution_verified=False)


def control_snapshot(names, *, epoch, source, latest_frame, publication_sequence_id,
                     started_ns, completed_ns, command, feedback, active_sides,
                     reference_kind, command_sequence_id, recovery_trigger=None,
                     wire_evidence=None, tracking_governor=None):
    commanded = [command.targets[n] for n in names]
    measured = [feedback.positions[n] for n in names]
    return dict(event='control_trace', schema_version=1,
        epoch=epoch, source_sequence_id=source['sequence_id'],
        command_sequence_id=command_sequence_id,
        source_input_created_time_ns=source['input_created_time_ns'],
        source_device_timestamp_ns=source['device_timestamp_ns'],
        publication_sequence_id=publication_sequence_id,
        latest_input_sequence_id=latest_frame.sequence_id,
        latest_input_created_time_ns=latest_frame.received_monotonic_ns,
        latest_device_timestamp_ns=latest_frame.device_timestamp_ns,
        publication_started_monotonic_ns=started_ns,
        publication_completed_monotonic_ns=completed_ns,
        publication_elapsed_ms=(completed_ns-started_ns)/1e6,
        source_input_age_at_publication_ms=(completed_ns-source['input_created_time_ns'])/1e6,
        latest_input_age_at_publication_ms=(completed_ns-latest_frame.received_monotonic_ns)/1e6,
        feedback_timestamp_ns=feedback.timestamp_ns, joint_order=list(names),
        command_rad=commanded, feedback_rad=measured,
        tracking_error_abs_rad=[abs(a-b) for a, b in zip(commanded, measured)],
        tracking_governor=(None if tracking_governor is None else dict(tracking_governor)),
        active_sides=list(active_sides), reference_kind=reference_kind,
        recovery_trigger=(None if recovery_trigger is None else {
            k: recovery_trigger[k] for k in ('epoch', 'sequence_id',
                                              'input_created_time_ns',
                                              'device_timestamp_ns')}),
        wire=(None if wire_evidence is None else {
            key: vector(value) if isinstance(value, (tuple, list)) else scalar(value)
            for key, value in wire_evidence.items()}),
        delivery_scope='publisher_enqueue_only', physical_execution_verified=False)


class ComputeResponseRejected(ValueError):
    """A failed boundary with received evidence, never an IK success result."""
    def __init__(self, reason, response):
        super().__init__(reason)
        self.response_evidence = response_fields(response)


def failure_snapshot(schema, *, sequence_id, now_ns, completed_ns, state, context,
                     identity, seed, request, response, actual, stage, error,
                     position_tol, position_axis_tol, rotation_tol):
    names = tuple(schema.names)
    # Only the audited arm order is supplied by the caller.
    limits = [{'joint': n, 'lower_rad': schema.get(n).position_min,
               'upper_rad': schema.get(n).position_max} for n in names]
    captured = getattr(error, 'response_evidence', None) if response is None else None
    raw_q = response.get('q_arm') if isinstance(response, Mapping) else (
        captured.get('q_arm') if isinstance(captured, Mapping) else None)
    violations = []
    if isinstance(raw_q, (tuple, list)) and len(raw_q) == len(names):
        for bound, value in zip(limits, raw_q):
            if isinstance(value, bool) or not isinstance(value, Real) \
                    or not isinstance(scalar(value), Real):
                continue
            lower, upper = bound['lower_rad'], bound['upper_rad']
            if not lower <= value <= upper:
                violations.append(dict(bound, value_rad=value,
                    side='lower' if value < lower else 'upper',
                    excess_rad=lower-value if value < lower else value-upper))
    raw_response = response_fields(response) if response is not None else captured
    return dict(event='ik_failure', schema_version=1, source_sequence_id=sequence_id,
        context={k: scalar((context or {}).get(k)) for k in
                 ('epoch', 'input_created_time_ns', 'device_timestamp_ns')},
        target_progress=target_progress_fields(context),
        started_monotonic_ns=now_ns, completed_monotonic_ns=completed_ns,
        elapsed_ms=(completed_ns-now_ns)/1e6,
        timing_scope='work_started_to_failure', response_time_cost_unit='ms',
        feedback_timestamp_ns=getattr(state, 'timestamp_ns', None), seed_rad=vector(seed),
        feedback_rad=feedback_fields(names, state),
        stage=stage, error_type=type(error).__name__, reason=str(error)[:512],
        model_identity={str(k)[:64]: scalar(v) for k, v in list((identity or {}).items())[:8]},
        joint_order=list(names), joint_limits=limits, request=request_fields(request),
        response=raw_response, actual_poses=poses(actual), limit_violations=violations,
        position_tolerance_m=position_tol,
        position_axis_tolerance_m=position_axis_tol,
        rotation_tolerance_deg=rotation_tol,
        physical_execution_verified=False)
