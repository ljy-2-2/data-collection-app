"""Local recorded-packet arithmetic only; no device or numerical IK backend.

Derivatives describe recorded position references, not measured robot dynamics.
The historical velocity field was not recorded: its capped-period calculation
is reconstructed for comparison, never presented as an observed ROS message.
"""

from collections import Counter
from collections.abc import Mapping
import math

from teleop.robots.kuavo5_native_ik import ARM_NAMES


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('non-negative integer required')
    return value


def _object(value):
    if not isinstance(value, Mapping):
        raise ValueError('JSON object required')
    return value


def _vector(values, size):
    if not isinstance(values, (tuple, list)) or len(values) != size:
        raise ValueError('incorrect vector size')
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError('finite numeric vector required')
    return values


def _sides(packet):
    sides = packet['active_sides']
    if not isinstance(sides, list) or not sides or len(set(sides)) != len(sides) \
            or not set(sides) <= {'left', 'right'}:
        raise ValueError('active arm sides required')
    return set(sides)


def _indices(sides):
    return [i for i in range(14) if ('left' if i < 7 else 'right') in sides]


def _goal_delta(a, b, sides):
    distances, rotations = [], []
    for side in sorted(sides):
        p, q = a['request']['hand_poses'][side+'_pose'], b['request']['hand_poses'][side+'_pose']
        distances.append(1000*math.dist(_vector(p['pos_xyz'], 3), _vector(q['pos_xyz'], 3)))
        qa, qb = _vector(p['quat_xyzw'], 4), _vector(q['quat_xyzw'], 4)
        na, nb = math.sqrt(sum(v*v for v in qa)), math.sqrt(sum(v*v for v in qb))
        if not .9 < na < 1.1 or not .9 < nb < 1.1:
            raise ValueError('near-unit goal quaternion required')
        dot = abs(sum(x*y for x, y in zip(qa, qb))/(na*nb))
        rotations.append(math.degrees(2*math.acos(min(1., dot))))
    return max(distances), max(rotations)


def replay_window(records, *, epoch, first_sequence, last_sequence,
                  nominal_period_s=.01, max_velocity_rad_s=2.):
    """Join one explicit generation/window, preserving publication order.

    No numerical solver is executed and no candidates are recomputed. Inactive
    indices are masked to zero in derivative arrays; they are not zero-velocity
    hardware observations. The first reference has no predecessor in-window.
    """
    for value in (epoch, first_sequence, last_sequence):
        _integer(value)
    if first_sequence > last_sequence:
        raise ValueError('invalid sequence window')
    for value in (nominal_period_s, max_velocity_rad_s):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('finite positive reference configuration required')
    ik, commands = {}, []
    for record in records:
        record = _object(record)
        packet = _object(record.get('packet', record))
        event = packet.get('event')
        if event not in ('ik_trace', 'control_trace'):
            continue
        generation = _integer(packet.get('epoch') if event == 'control_trace'
                              else _object(packet.get('context')).get('epoch'))
        if generation != epoch:
            continue
        seq = _integer(packet.get('source_sequence_id'))
        if not first_sequence <= seq <= last_sequence:
            continue
        if packet.get('joint_order') != list(ARM_NAMES):
            raise ValueError('exact left7/right7 joint order required')
        if event == 'ik_trace':
            if seq in ik:
                raise ValueError('duplicate IK source sequence')
            response = _object(packet.get('response'))
            hand_poses = _object(_object(packet.get('request')).get('hand_poses'))
            for side in ('left_pose', 'right_pose'):
                hand = _object(hand_poses.get(side))
                _vector(hand.get('pos_xyz'), 3)
                _vector(hand.get('quat_xyzw'), 4)
            if response.get('success') is not True:
                raise ValueError('accepted official IK required')
            _vector(packet['response']['q_arm'], 14)
            _vector(packet['seed_rad'], 14)
            _integer(packet['completed_monotonic_ns'])
            ik[seq] = packet
        else:
            _vector(packet['command_rad'], 14)
            _integer(packet['publication_started_monotonic_ns'])
            _sides(packet)
            if commands and _sides(packet) != _sides(commands[-1]):
                raise ValueError('active sides changed within selected window; split the window')
            if packet.get('reference_kind') != 'bounded_ik_candidate':
                raise ValueError('window must contain consumed IK references only')
            commands.append(packet)
    if len(ik) < 2 or len(commands) < 2:
        raise ValueError('at least two IK results and consumed references required')
    counts = Counter(p['source_sequence_id'] for p in commands)
    consumers = {}
    for p in commands:
        seq = p['source_sequence_id']
        if seq not in ik:
            raise ValueError('missing consumed IK result')
        if p['publication_started_monotonic_ns'] < ik[seq]['completed_monotonic_ns']:
            raise ValueError('publication precedes consumed IK completion')
        consumers.setdefault(seq, set()).update(_sides(p))
    if len(consumers) < 2:
        raise ValueError('at least two distinct consumed IK results required')
    results = sorted((ik[seq] for seq in consumers), key=lambda p: p['completed_monotonic_ns'])
    transitions = []
    for a, b in zip(results, results[1:]):
        sides = consumers[a['source_sequence_id']] & consumers[b['source_sequence_id']]
        if not sides:
            continue
        indices = _indices(sides)
        before, after = a['response']['q_arm'], b['response']['q_arm']
        i = max(indices, key=lambda i: abs(after[i]-before[i]))
        distance, rotation = _goal_delta(a, b, sides)
        transitions.append(dict(sequences=[a['source_sequence_id'], b['source_sequence_id']],
            continuous_active_sides=sorted(sides), joint=ARM_NAMES[i],
            goal_position_step_mm=distance, goal_rotation_step_deg=rotation,
            returned_step_rad=after[i]-before[i], seed_step_rad=b['seed_rad'][i]-a['seed_rad'][i],
            consumed_commands=counts[b['source_sequence_id']]))
    steps, last_velocity, last_sides, last_interval = [], None, None, None
    for a, b in zip(commands, commands[1:]):
        dt = (b['publication_started_monotonic_ns']-a['publication_started_monotonic_ns'])/1e9
        if dt <= 0:
            raise ValueError('non-positive publication interval')
        sides = _sides(a) & _sides(b)
        indices = _indices(sides)
        velocity, legacy = [0.]*14, [0.]*14
        for i in indices:
            delta = b['command_rad'][i]-a['command_rad'][i]
            velocity[i] = delta/dt
            legacy[i] = max(-max_velocity_rad_s, min(max_velocity_rad_s,
                                                    delta/min(nominal_period_s, dt)))
        acceleration = None
        if last_velocity is not None and sides == last_sides:
            acceleration = [0.]*14
            for i in indices:
                # Position differences are interval-average velocities located
                # at interval midpoints, including unequal publication periods.
                acceleration[i] = (velocity[i]-last_velocity[i])/((dt+last_interval)/2)
        steps.append(dict(source_sequence_id=b['source_sequence_id'], actual_interval_s=dt,
            continuous_active_sides=sorted(sides), reference_velocity_rad_s=velocity,
            legacy_capped_velocity_rad_s=legacy, reference_acceleration_rad_s2=acceleration))
        last_velocity, last_sides, last_interval = velocity, sides, dt
    return dict(hardware_connected=False, official_solver_called=False,
        physical_root_cause='unresolved', epoch=epoch, ik_count=len(ik), command_count=len(commands),
        derivative_scope='recorded_position_reference_at_publication_start_not_hardware_dynamics',
        historical_velocity_field_observed=False, inactive_derivative_indices='masked_to_zero',
        nominal_period_s=nominal_period_s, max_velocity_rad_s=max_velocity_rad_s,
        ik_transitions=transitions, reference_steps=steps)
