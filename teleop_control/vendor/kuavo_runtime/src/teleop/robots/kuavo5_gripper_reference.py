"""Coarse, host-only screening of the two Damiao position references.

This is deliberately NOT a calibrated encoder mapping. The interval observed at
the two historical endpoints, expanded by 0.05 rad, can expose the large startup
reference change seen in our logs. Passing cannot prove the interval covers all
intermediate positions or that a physical target is correct. The 0.05 rad is a
diagnostic allowance, never a native motion-limit or feedback tolerance.

No hardware imports, I/O, writes, offset compensation, or reference learning.
The caller remains responsible for verifying motor identity and CAN routing.
"""
import math
import time


DIAGNOSTIC_ALLOWANCE_RAD = 0.05
MAX_PAIR_SPAN_NS = 50_000_000


class ReferenceMismatch(ValueError):
    """Rejected reference check, retaining the JSON-compatible diagnostic."""

    def __init__(self, report):
        self.report = report
        explanation = ('角度参考与原记录不一致，未自动补偿' if report['status'] == 'shift_detected'
                       else '当前测量不足以确认角度参考，请查看具体原因')
        super().__init__('gripper position reference ' + report['status'] + ': ' +
                         explanation + '; ' + report['reason'])


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + ' must be finite')
    return float(value)


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(name + ' must be an integer >= ' + str(minimum))
    return value


def _position(value, name, limit):
    value = _number(value, name)
    if abs(value) >= limit:
        raise ValueError(name + ' outside unwrapped single-turn/PMAX range')
    return value


def _outside(value, interval):
    """Signed distance to an interval, zero when inside."""
    low, high = interval
    return value-low if value < low else value-high if value > high else 0.


def report_reference(motor_record, measured, now_ns=None, *, configured_working_coverage=False):
    """Inspect existing samples only; return plain JSON data for every outcome.

    ``measured`` holds p_m -> xout -> p_m reads, each with its own receive time,
    followed by a fresh disabled ``feedback`` object. Historical timestamps are
    validated as evidence fields but never compared with the current boot clock.
    Missing, moving, wrapped or unsupported observations cannot pass.
    """
    report = dict(status='insufficient_evidence', reason='not evaluated',
                  scope='coarse_reference_screen_only', mapping_verified=False,
                  physical_target_verified=False, compensation_applied=False,
                  diagnostic_allowance_rad=DIAGNOSTIC_ALLOWANCE_RAD,
                  diagnostic_allowance_is_motion_tolerance=False,
                  identity_and_route_validation='caller_responsibility',
                  reference=None, current=None)
    try:
        now = _integer(time.monotonic_ns() if now_ns is None else now_ns, 'now_ns', 1)
        if not isinstance(motor_record, dict) or not isinstance(measured, dict):
            raise ValueError('motor record and measured sample must be objects')
        profile = motor_record['profile']
        pmax = _number(profile['pmax'], 'pmax')
        vmax = _number(profile['vmax'], 'vmax')
        if pmax <= 0 or vmax <= 0:
            raise ValueError('positive PMAX and VMAX required')
        timeout_ms = _integer(profile['feedback_timeout_ms'], 'feedback_timeout_ms', 1)
        if not 50 <= timeout_ms <= 300:
            raise ValueError('feedback_timeout_ms outside supported 50..300 range')
        temperature_limit = _number(profile['max_temperature_c'], 'max_temperature_c')
        limit = min(math.pi, pmax)
        position_tolerance = max(.002, 3*2*pmax/65535)
        velocity_tolerance = max(.1, 2*vmax/4095)

        calibration = motor_record['calibration']
        if calibration.get('source') != 'measured_manual_endpoints':
            raise ValueError('original measured endpoint evidence required')
        marks = calibration['marks']
        if set(marks) != {'open', 'closed'}:
            raise ValueError('both original endpoint marks required')
        references = {}
        for endpoint in ('open', 'closed'):
            mark = marks[endpoint]
            p = _position(mark['position_rad'], endpoint+' position_rad', limit)
            x = _position(mark['xout_rad'], endpoint+' xout_rad', limit)
            _integer(mark['received_ns'], endpoint+' received_ns')
            _integer(mark['xout_received_ns'], endpoint+' xout_received_ns')
            _integer(mark['sequence'], endpoint+' sequence', 1)
            if type(mark['state']) is not int or mark['state'] not in (0, 1):
                raise ValueError(endpoint+' mark captured in fault state')
            references[endpoint] = dict(position_rad=p, xout_rad=x, difference_rad=p-x)
        dp = references['closed']['position_rad']-references['open']['position_rad']
        dx = references['closed']['xout_rad']-references['open']['xout_rad']
        if dp*dx <= 0 or not .5 <= abs(dx/dp) <= 1.5:
            raise ValueError('historical position travels disagree')
        if abs(dp) >= math.pi or abs(dx) >= math.pi:
            raise ValueError('historical travel may cross the single-turn wrap; unsupported')
        if marks['open']['sequence'] == marks['closed']['sequence']:
            raise ValueError('historical endpoints must be independent samples')
        interval = sorted(value['difference_rad'] for value in references.values())
        coverage = sorted(value['xout_rad'] for value in references.values())
        expanded = [interval[0]-DIAGNOSTIC_ALLOWANCE_RAD,
                    interval[1]+DIAGNOSTIC_ALLOWANCE_RAD]
        report['reference'] = dict(source='original_measured_endpoint_differences',
            marks=references, difference_interval_rad=interval,
            expanded_difference_interval_rad=expanded, xout_coverage_rad=coverage,
            interior_mapping_verified=False, historical_timestamps_compared_to_current_boot=False)

        p = _position(measured['position_rad'], 'position_rad', limit)
        x = _position(measured['xout_rad'], 'xout_rad', limit)
        after = _position(measured['position_after_rad'], 'position_after_rad', limit)
        times = [_integer(measured[key], key, 1) for key in
                 ('position_received_ns', 'xout_received_ns', 'position_after_received_ns')]
        feedback = measured['feedback']
        stamp = _integer(feedback.received_ns, 'feedback received_ns', 1)
        observed = _position(feedback.position_rad, 'feedback position', limit)
        velocity = _number(feedback.velocity_rad_s, 'feedback velocity')
        temperatures = [_number(feedback.mos_c, 'mos temperature'),
                        _number(feedback.rotor_c, 'rotor temperature')]
        report['current'] = dict(position_rad=p, xout_rad=x, position_after_rad=after,
            feedback_position_rad=observed, velocity_rad_s=velocity,
            mos_c=temperatures[0], rotor_c=temperatures[1],
            position_received_ns=times[0], xout_received_ns=times[1],
            position_after_received_ns=times[2], feedback_received_ns=stamp)
        _integer(feedback.sequence, 'feedback sequence', 1)
        if type(feedback.state) is not int or feedback.state != 0:
            raise ValueError('motor must already be disabled')
        stamps = times+[stamp]
        if any(value > now or now-value > timeout_ms*1_000_000 for value in stamps):
            raise ValueError('position/feedback sample stale or from the future')
        if stamps != sorted(stamps):
            raise ValueError('expected p_m -> xout -> p_m -> feedback timestamp order')
        if times[-1]-times[0] > MAX_PAIR_SPAN_NS:
            raise ValueError('position read span exceeds 50 ms')
        if abs(velocity) > velocity_tolerance:
            raise ValueError('motor moving during reference sample')
        if max(temperatures) >= temperature_limit:
            raise ValueError('motor temperature exceeds reference-check limit')
        if max(abs(after-p), abs(observed-p), abs(observed-after)) > position_tolerance:
            raise ValueError('position changed while reference was read')
        differences = [p-x, after-x]
        residuals = [_outside(value, interval) for value in differences]
        report['current'].update(difference_rad=differences[0],
            difference_after_rad=differences[1], interval_residual_rad=residuals[0],
            interval_residual_after_rad=residuals[1],
            largest_absolute_residual_rad=max(abs(value) for value in residuals),
            pair_span_ns=times[2]-times[0], oldest_age_ns=now-times[0],
            within_historical_xout_coverage=coverage[0] <= x <= coverage[1])
        if not coverage[0] <= x <= coverage[1]:
            if not configured_working_coverage:
                raise ValueError('xout outside historical measurement coverage; no extrapolation')
            # Explicit coordinate-policy callers may inspect the operator's
            # already adjusted working range. This is NOT additional measured
            # coverage or a fitted/extrapolated encoder calibration.
            working = sorted(_position(profile[key], key, limit)
                             for key in ('open_rad', 'closed_rad'))
            derived = [working[0]-expanded[1], working[1]-expanded[0]]
            report['reference'].update(configured_xout_envelope_rad=derived,
                                      configured_envelope_is_measured=False)
            if not derived[0] <= x <= derived[1]:
                raise ValueError('xout outside configured working envelope')
        if any(not expanded[0] <= value <= expanded[1] for value in differences):
            report.update(status='shift_detected',
                          reason='p_m minus xout differs from historical endpoint interval beyond diagnostic allowance')
        else:
            report.update(status='compatible',
                          reason='no large reference mismatch detected; accurate mapping and physical target remain unverified')
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        report.update(status='insufficient_evidence', reason=str(exc))
    return report


def require_reference(motor_record, measured, now_ns=None):
    """Require coarse compatibility; never adjusts angles or approves motion."""
    report = report_reference(motor_record, measured, now_ns)
    if report['status'] != 'compatible':
        raise ReferenceMismatch(report)
    return report
