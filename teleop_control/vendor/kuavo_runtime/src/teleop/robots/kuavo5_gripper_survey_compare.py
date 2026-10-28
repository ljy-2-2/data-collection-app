"""Pure validation/comparison of manually positioned, disabled motor surveys.

No device imports or I/O. Matching xout readings is encoder evidence, not an
independent measurement of jaw position. Three sampled positions cannot prove a
whole-travel conversion, firmware cause, or permission to compensate/control.
"""
import math
import statistics


POINT_NAMES = ('open', 'middle', 'closed')
SAMPLE_COUNT = 3
MAX_SAMPLE_AGE_NS = 150_000_000
MAX_REGISTER_SPAN_NS = 50_000_000
MAX_POINT_SPREAD_RAD = 0.002
MIN_POINT_SEPARATION_RAD = 0.03
MIN_SURVEY_TRAVEL_RAD = 0.1
MAX_MATCH_DISTANCE_RAD = 0.003
MAX_WIDTH_DIFFERENCE_MM = 0.5
SHIFT_UNIFORMITY_TOLERANCE_RAD = 0.01
UNCHANGED_TOLERANCE_RAD = 0.01
MAX_SAMPLE_TEMPERATURE_C = 60.0

_READBACK_INTEGER_FIELDS = ('hardware_version', 'software_version', 'sub_version',
                            'motor_id', 'master_id', 'bitrate_code', 'mode',
                            'watchdog_ticks')
_READBACK_LIMIT_FIELDS = ('pmax', 'vmax', 'tmax', 'imax_a')
_IDENTITY_FIELDS = ('hardware_version', 'software_version', 'sub_version',
                    'motor_id', 'master_id', 'bitrate_code') + _READBACK_LIMIT_FIELDS


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(name + ' must be an object')
    return value


def _required(document, name):
    if name not in document:
        raise ValueError('missing ' + name)
    return document[name]


def _number(value, name):
    if type(value) not in (int, float):
        raise ValueError(name + ' must be a finite number')
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValueError(name + ' must be a finite number') from None
    if not math.isfinite(result):
        raise ValueError(name + ' must be a finite number')
    return result


def _integer(value, name, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(name + ' must be an integer in the supported range')
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + ' must be a nonempty string')
    return value


def _sha256(value, name):
    value = _text(value, name)
    if len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
        raise ValueError(name + ' must be a lowercase SHA-256 hexadecimal digest')
    return value


def _spread(values):
    return max(values) - min(values)


def _readback(readback):
    _object(readback, 'readback')
    for name in _READBACK_INTEGER_FIELDS:
        _integer(_required(readback, name), 'readback.' + name)
    for name in _READBACK_LIMIT_FIELDS:
        if _number(_required(readback, name), 'readback.' + name) <= 0:
            raise ValueError('readback.' + name + ' must be positive')
    _integer(readback['motor_id'], 'readback.motor_id', 1, 0x7ff)
    _integer(readback['master_id'], 'readback.master_id', 0, 0x7ff)
    _integer(readback['bitrate_code'], 'readback.bitrate_code', 0, 0xffffffff)
    _integer(readback['mode'], 'readback.mode', 1, 4)
    _integer(readback['watchdog_ticks'], 'readback.watchdog_ticks', 0, 0xffffffff)
    return readback


def validate_sample(sample, readback):
    """Validate one fresh disabled p_m -> xout -> p_m -> feedback capture.

    Times are checked against the saved sampled_ns from the same boot, never
    against today's clock. Temperature is the existing sampler's <60 C capture
    condition, not a modification to any runtime motor protection setting.
    """
    _object(sample, 'sample')
    _readback(readback)
    now = _integer(_required(sample, 'sampled_ns'), 'sampled_ns', 1)
    pmax = float(readback['pmax'])
    p = _number(_required(sample, 'position_rad'), 'position_rad')
    after = _number(_required(sample, 'position_after_rad'), 'position_after_rad')
    x = _number(_required(sample, 'xout_rad'), 'xout_rad')
    if abs(p) >= pmax or abs(after) >= pmax:
        raise ValueError('p_m outside PMAX range')
    if abs(x) >= math.pi:
        raise ValueError('xout outside unwrapped single-turn range')
    names = ('position_received_ns', 'xout_received_ns', 'position_after_received_ns')
    stamps = [_integer(_required(sample, name), name, 1) for name in names]
    feedback = _object(_required(sample, 'feedback'), 'feedback')
    stamp = _integer(_required(feedback, 'received_ns'), 'feedback.received_ns', 1)
    _integer(_required(feedback, 'sequence'), 'feedback.sequence', 1)
    state = _integer(_required(feedback, 'state'), 'feedback.state')
    if state != 0:
        raise ValueError('motor must already be disabled (state=0)')
    observed = _number(_required(feedback, 'position_rad'), 'feedback.position_rad')
    velocity = _number(_required(feedback, 'velocity_rad_s'), 'feedback.velocity_rad_s')
    _number(_required(feedback, 'torque_nm'), 'feedback.torque_nm')
    if abs(observed) >= pmax:
        raise ValueError('feedback position outside PMAX range')
    for name in ('mos_c', 'rotor_c'):
        temperature = _number(_required(feedback, name), 'feedback.' + name)
        if not -40 <= temperature < MAX_SAMPLE_TEMPERATURE_C:
            raise ValueError('temperature outside disabled sampler capture range')
    all_stamps = stamps + [stamp]
    if all_stamps != sorted(all_stamps):
        raise ValueError('expected p_m -> xout -> p_m -> feedback timestamp order')
    if any(value > now or now - value > MAX_SAMPLE_AGE_NS for value in all_stamps):
        raise ValueError('sample stale or from the future at sampled_ns')
    if stamps[-1] - stamps[0] > MAX_REGISTER_SPAN_NS:
        raise ValueError('position register read span exceeds 50 ms')
    if abs(velocity) > max(0.1, 2 * float(readback['vmax']) / 4095):
        raise ValueError('motor moving during sample')
    if _spread([p, after, observed]) > max(0.002, 6 * pmax / 65535):
        raise ValueError('position changed while sample was read')
    width = sample.get('measured_opening_mm')
    if width is not None and _number(width, 'measured_opening_mm') < 0:
        raise ValueError('measured_opening_mm must be nonnegative')
    return sample


def summarize_point(samples, readback):
    """Require three independent static captures and return their medians."""
    if not isinstance(samples, list) or len(samples) != SAMPLE_COUNT:
        raise ValueError('each point must contain exactly three samples')
    for sample in samples:
        validate_sample(sample, readback)
    for previous, current in zip(samples, samples[1:]):
        if current['position_received_ns'] <= previous['sampled_ns']:
            raise ValueError('point samples must be separate captures in timestamp order')
        if current['feedback']['sequence'] <= previous['feedback']['sequence']:
            raise ValueError('point feedback sequences must increase')
    positions = [float(item['position_rad']) for item in samples]
    after_positions = [float(item['position_after_rad']) for item in samples]
    xouts = [float(item['xout_rad']) for item in samples]
    difference = [p - x for p, x in zip(positions, xouts)]
    p_spread = _spread(positions + after_positions)
    x_spread = _spread(xouts)
    if p_spread > MAX_POINT_SPREAD_RAD or x_spread > MAX_POINT_SPREAD_RAD:
        raise ValueError('point moved during three samples; spread exceeds 0.002 rad')
    widths = [item.get('measured_opening_mm') for item in samples]
    present = [float(value) for value in widths if value is not None]
    if present and (len(present) != SAMPLE_COUNT or _spread(present) > MAX_WIDTH_DIFFERENCE_MM):
        raise ValueError('point measured widths must be consistently provided and stable')
    return {
        'samples': SAMPLE_COUNT,
        'position_rad': statistics.median(positions),
        'position_after_rad': statistics.median(after_positions),
        'xout_rad': statistics.median(xouts),
        'reference_difference_rad': statistics.median(difference),
        'reference_difference_spread_rad': _spread(difference),
        'position_spread_rad': p_spread,
        'xout_spread_rad': x_spread,
        'measured_opening_mm': statistics.median(present) if present else None,
        'first_sampled_ns': samples[0]['sampled_ns'],
        'last_sampled_ns': samples[-1]['sampled_ns'],
    }


def validate_motor(side, motor):
    """Validate the route/readback and ordered open/middle/closed evidence."""
    if side not in ('left', 'right'):
        raise ValueError('side must be left or right')
    _object(motor, side + ' motor')
    if _required(motor, 'bus') not in ('canbus0', 'canbus1'):
        raise ValueError(side + '.bus must be canbus0 or canbus1')
    _integer(_required(motor, 'motor_id'), side + '.motor_id', 1, 0x7ff)
    _integer(_required(motor, 'master_id'), side + '.master_id', 0, 0x7ff)
    readback = _readback(_required(motor, 'readback'))
    for name in ('motor_id', 'master_id'):
        if motor[name] != readback[name]:
            raise ValueError(side + ': route differs from readback ' + name)
    points = _object(_required(motor, 'points'), side + '.points')
    if set(points) != set(POINT_NAMES):
        raise ValueError(side + ': open, middle and closed points required')
    summaries = [summarize_point(points[name], readback) for name in POINT_NAMES]
    for key in ('xout_rad', 'position_rad'):
        values = [point[key] for point in summaries]
        steps = [values[1] - values[0], values[2] - values[1]]
        if steps[0] * steps[1] <= 0:
            raise ValueError(side + ': middle must lie strictly between open and closed (' + key + ')')
        if min(abs(step) for step in steps) < MIN_POINT_SEPARATION_RAD:
            raise ValueError(side + ': points need at least 0.03 rad separation (' + key + ')')
        travel = abs(values[2] - values[0])
        if not MIN_SURVEY_TRAVEL_RAD <= travel < math.pi:
            raise ValueError(side + ': travel must cover 0.1 rad without single-turn wrap (' + key + ')')
    dp = summaries[-1]['position_rad'] - summaries[0]['position_rad']
    dx = summaries[-1]['xout_rad'] - summaries[0]['xout_rad']
    if dp * dx <= 0:
        raise ValueError(side + ': p_m and xout travel directions disagree')
    return motor


def validate_survey(document):
    """Return the unchanged document or raise ValueError for invalid evidence."""
    _object(document, 'survey')
    if type(document.get('schema_version')) is not int or document['schema_version'] != 1:
        raise ValueError('unsupported survey schema_version')
    if document.get('kind') != 'disabled_gripper_angle_survey' or document.get('complete') is not True:
        raise ValueError('complete disabled_gripper_angle_survey required')
    context = _object(_required(document, 'context'), 'context')
    for name in ('boot_id', 'release_id', 'owner_directory'):
        _text(_required(context, name), 'context.' + name)
    for name in ('controller_pid', 'controller_start_ticks'):
        _integer(_required(context, name), 'context.' + name, 1)
    for name in ('installation_sha256', 'gripper_config_sha256'):
        _sha256(_required(context, name), 'context.' + name)
    motors = _object(_required(document, 'motors'), 'motors')
    if set(motors) != {'left', 'right'}:
        raise ValueError('both left and right motor surveys required')
    for side in ('left', 'right'):
        validate_motor(side, motors[side])
    routes = [(motor['bus'], motor['motor_id']) for motor in motors.values()]
    if len(set(routes)) != len(routes):
        raise ValueError('left/right routes must identify different motors')
    return document


def _point_comparison(before, after):
    delta_x = after['xout_rad'] - before['xout_rad']
    widths = (before['measured_opening_mm'], after['measured_opening_mm'])
    have_widths = all(value is not None for value in widths)
    width_delta = widths[1] - widths[0] if have_widths else None
    reasons = []
    if abs(delta_x) > MAX_MATCH_DISTANCE_RAD:
        reasons.append('output_encoder_positions_do_not_match')
    if have_widths and abs(width_delta) > MAX_WIDTH_DIFFERENCE_MM:
        reasons.append('independent_jaw_widths_do_not_match')
    return {
        'matched': not reasons,
        'match_reasons': reasons,
        'before': before,
        'after': after,
        'output_encoder_delta_rad': delta_x,
        'measured_opening_delta_mm': width_delta,
        'physical_position_comparison': 'encoder_and_measured_width' if have_widths else 'encoder_only',
        'width_available_before': widths[0] is not None,
        'width_available_after': widths[1] is not None,
        'delta_reference_difference_rad': after['reference_difference_rad'] - before['reference_difference_rad'],
    }


def compare_surveys(before, after):
    """Classify cross-boot observations without creating or permitting offsets."""
    validate_survey(before)
    validate_survey(after)
    a, b = before['context'], after['context']
    issues = []
    if a['boot_id'] == b['boot_id']:
        issues.append('different_boot_ids_required')
    for field in ('release_id', 'installation_sha256', 'gripper_config_sha256', 'owner_directory'):
        if a[field] != b[field]:
            issues.append('context_changed:' + field)
    report = {
        'schema_version': 1,
        'kind': 'disabled_gripper_angle_survey_comparison',
        'result': 'insufficient_evidence',
        'compensation_permitted': False,
        'physical_target_verified': False,
        'whole_travel_mapping_verified': False,
        'firmware_root_cause': 'unknown',
        'scope': 'three_sampled_positions_only',
        'timestamps_compared_across_boots': False,
        'boot_identity_scope': 'robot_computer_only',
        'motor_power_cycle_operator_confirmed': after.get('motor_power_cycle_operator_confirmed') is True,
        'motor_power_cycle_independently_verified': False,
        'context_before': dict(a),
        'context_after': dict(b),
        'issues': issues,
        'tolerances': {
            'point_matching_rad': MAX_MATCH_DISTANCE_RAD,
            'independent_width_matching_mm': MAX_WIDTH_DIFFERENCE_MM,
            'shift_uniformity_rad': SHIFT_UNIFORMITY_TOLERANCE_RAD,
            'reference_unchanged_rad': UNCHANGED_TOLERANCE_RAD,
        },
        'motors': {},
    }
    position_kinds = []
    for side in ('left', 'right'):
        first, second = before['motors'][side], after['motors'][side]
        side_issues = []
        for field in ('bus', 'motor_id', 'master_id'):
            if first[field] != second[field]:
                side_issues.append('route_changed:' + field)
        for field in _IDENTITY_FIELDS:
            if first['readback'][field] != second['readback'][field]:
                side_issues.append('identity_or_limit_changed:' + field)
        points = {}
        for name in POINT_NAMES:
            points[name] = _point_comparison(
                summarize_point(first['points'][name], first['readback']),
                summarize_point(second['points'][name], second['readback']))
            position_kinds.append(points[name]['physical_position_comparison'])
            if not points[name]['matched']:
                side_issues.append('unmatched_point:' + name)
        deltas = [points[name]['delta_reference_difference_rad'] for name in POINT_NAMES]
        median = statistics.median(deltas)
        spread = _spread(deltas)
        if side_issues or issues:
            result = 'insufficient_evidence'
        elif spread > SHIFT_UNIFORMITY_TOLERANCE_RAD:
            result = 'nonuniform_shift'
        elif max(abs(value) for value in deltas) <= UNCHANGED_TOLERANCE_RAD:
            result = 'reference_unchanged_at_sampled_positions'
        else:
            result = 'stable_shift_at_sampled_positions'
        report['motors'][side] = {
            'result': result,
            'issues': side_issues,
            'points': points,
            'median_reference_shift_rad': median,
            'reference_shift_spread_rad': spread,
            'shift_uniform_at_sampled_positions': spread <= SHIFT_UNIFORMITY_TOLERANCE_RAD if not side_issues and not issues else None,
            'reboot_parameter_observations': {
                field: {'before': first['readback'][field], 'after': second['readback'][field]}
                for field in ('mode', 'watchdog_ticks')},
        }
    results = [motor['result'] for motor in report['motors'].values()]
    if issues or 'insufficient_evidence' in results:
        report['result'] = 'insufficient_evidence'
    elif 'nonuniform_shift' in results:
        report['result'] = 'nonuniform_shift'
    elif 'stable_shift_at_sampled_positions' in results:
        report['result'] = 'stable_shift_at_sampled_positions'
    else:
        report['result'] = 'reference_unchanged_at_sampled_positions'
    report['physical_position_comparison'] = (
        'encoder_and_measured_width' if all(value == 'encoder_and_measured_width' for value in position_kinds)
        else 'encoder_only' if all(value == 'encoder_only' for value in position_kinds)
        else 'partially_measured_width')
    return report
