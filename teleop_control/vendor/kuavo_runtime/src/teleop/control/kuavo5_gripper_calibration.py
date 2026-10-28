"""Host-only manual endpoint calibration, based on Tianji gripper/endpoints.py.

No imports of robot SDKs or discovery. The caller obtains actual measurements
through its explicit owner session. Capturing or saving never sends CAN and
never changes the motor zero/Flash. New output files preserve old calibration.
"""

from dataclasses import asdict, dataclass
import copy
import json
import math
import os
from pathlib import Path
import tempfile

from teleop.robots.damiao_gripper import MotorFeedback, MotorProfile, finite, validate_routes


ROUTES = {'left': ('canbus0', 8, 0), 'right': ('canbus1', 1, 0)}


@dataclass(frozen=True)
class RuntimeGripperConfig:
    profiles: dict
    occupied_ids: dict
    owner_directory: str
    document: dict


def read_document(path_or_dict):
    value = (copy.deepcopy(path_or_dict) if isinstance(path_or_dict, dict)
             else json.loads(Path(path_or_dict).read_text(encoding='utf-8')))
    if not isinstance(value, dict):
        raise ValueError('gripper configuration must be an object')
    return value


def load_runtime_config(path_or_dict, *, require_commissioned=True):
    """Validate before any live interface is opened; no implicit defaults/IDs."""
    document = read_document(path_or_dict)
    if (document.get('schema_version') != 2 or document.get('transport') != 'leju_can_owner'
            or document.get('frame_format') != 'classic_standard'
            or document.get('can_bitrate_bps') != 1_000_000):
        raise ValueError('requires schema 2, leju_can_owner, classic_standard CAN at 1 Mbps')
    directory = document.get('owner_directory')
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise ValueError('explicit absolute owner_directory required')
    inventory = document.get('occupied_ids')
    if not isinstance(inventory, dict):
        raise ValueError('occupied_ids inventory required')
    for bus, ids in inventory.items():
        if (not isinstance(bus, str) or not isinstance(ids, list)
                or any(type(i) is not int or not 0 <= i <= 0x7ff for i in ids)):
            raise ValueError('invalid occupied_ids inventory')
    motors = document.get('motors')
    if not isinstance(motors, dict) or not motors or not set(motors) <= set(ROUTES):
        raise ValueError('configuration requires left and/or right motor')
    profiles = {}
    for side, motor in motors.items():
        if not isinstance(motor, dict):
            raise ValueError(side + ': motor configuration must be an object')
        raw_profile = motor.get('profile')
        if not isinstance(raw_profile, dict):
            raise ValueError(side + ': actual measured profile required; complete calibration first')
        try:
            profile = MotorProfile(**raw_profile)
        except (TypeError, ValueError) as exc:
            raise ValueError(side + ': invalid motor profile: ' + str(exc)) from exc
        if (profile.bus, profile.motor_id, profile.master_id) != ROUTES[side]:
            raise ValueError(side + ': route differs from left8/right1, Master0 configuration')
        if 'position_reference_policy' in motor:
            from teleop.robots.kuavo5_gripper_coordinates import validate_policy
            validate_policy(motor)
        # Match the independent native DMC1 commissioning ceilings. Protocol
        # ranges in MotorProfile are intentionally broader for offline codec
        # use; unsupported owner profiles must fail here, before SSH/ROS/CAN.
        for name, upper in (('speed_rad_s', 15. if profile.control_mode == 4 else 2.), ('kp', 100.),
                            ('max_torque_nm', 2.5), ('max_temperature_c', 60.)):
            if getattr(profile, name) > upper:
                raise ValueError(side + ': native CAN owner requires ' + name + ' <= ' + str(upper))
        if require_commissioned:
            calibration = motor.get('calibration', {})
            if motor.get('commissioned') is not True or not isinstance(calibration, dict):
                raise ValueError(side + ': not commissioned; measured endpoints alone are not a ready config; '
                                 'complete explicit watchdog readback and configuration commissioning first '
                                 '(apps/kuavo5_gripper_calibrate.py)')
            if calibration.get('source') != 'measured_manual_endpoints':
                raise ValueError(side + ': measured endpoint evidence required')
            marks = calibration.get('marks', {})
            sources = {}
            for endpoint in ('open', 'closed'):
                key = endpoint + '_endpoint_source'
                sources[endpoint] = motor.get(key, 'measured_manual_endpoints')
                if sources[endpoint] not in ('measured_manual_endpoints', 'operator_adjusted'):
                    raise ValueError(side + ': invalid ' + key)
            for endpoint, field in (('open', 'open_rad'), ('closed', 'closed_rad')):
                mark = marks.get(endpoint, {})
                manual_endpoint = sources[endpoint] == 'operator_adjusted'
                if not manual_endpoint and mark.get('position_rad') != getattr(profile, field):
                    raise ValueError(side + ': profile endpoints differ from calibration evidence')
                _validate_saved_mark(mark, side)
            _validate_pair(marks, profile.pmax, profile.margin_rad,
                           calibration.get('minimum_travel_rad', math.radians(5)))
            if 'operator_adjusted' in sources.values():
                # Retain/validate the original samples, including their xout values.
                # Adjusted endpoints are operator settings, NOT new measurements.
                # Do not synthesize timestamps, shaft readings or acceptance results.
                travel = profile.closed_rad - profile.open_rad
                measured_travel = marks['closed']['position_rad'] - marks['open']['position_rad']
                if travel * measured_travel <= 0:
                    raise ValueError(side + ': adjusted endpoints reverse closing direction')
                if abs(travel) < calibration.get('minimum_travel_rad', math.radians(5)):
                    raise ValueError(side + ': adjusted endpoints leave too little travel')
                if any(abs(value) >= min(math.pi-.02, profile.pmax)
                       for value in (profile.open_rad, profile.closed_rad)):
                    raise ValueError(side + ': adjusted endpoint exceeds single-turn/PMAX range')
            readback = motor.get('readback', {})
            if not isinstance(readback, dict):
                raise ValueError(side + ': actual register readback required')
            expected = {'motor_id': profile.motor_id, 'master_id': profile.master_id,
                        'mode': profile.control_mode, 'bitrate_code': 4,
                        'pmax': profile.pmax, 'vmax': profile.vmax, 'tmax': profile.tmax,
                        'watchdog_ticks': 4000}
            if any(readback.get(k) != v for k, v in expected.items()):
                raise ValueError(side + ': readback identity/mode/limits do not match profile')
            if profile.control_mode == 4 and readback.get('imax_a') != profile.imax_a:
                raise ValueError(side + ': mode4 Imax differs from actual readback')
        profiles[side] = profile
    validate_routes(profiles, inventory)
    return RuntimeGripperConfig(profiles, copy.deepcopy(inventory), directory, document)


def mode4_candidate(path_or_dict, *, tianji=None):
    """Offline migration only. Preserve true historical reads and all limits.

    The operator must configure hardware mode4 and then obtain fresh bilateral
    readback before this can become a runtime configuration. No implied mode
    switch, parameter write, calibration, or physical validation.
    """
    original = load_runtime_config(path_or_dict)
    document = copy.deepcopy(original.document)
    previous = document.pop('preparation', None)
    if previous is not None:
        document['previous_preparation'] = previous
    document['purpose'] = 'Offline mode4 candidate; motor mode change and fresh readback still required'
    document['mode_migration'] = dict(target_mode=4, limits_preserved=True,
        source_modes={s:p.control_mode for s,p in original.profiles.items()},
        motor_mode_written=False, live_readback_verified=False,
        mechanical_zero_must_remain_unchanged=True)
    for motor in document['motors'].values():
        motor['profile'].update(control_mode=4, kp=0., kd=0.)
        motor['commissioned'] = False
    if tianji is not None:
        reference = read_document(tianji)
        for side, motor in document['motors'].items():
            source = reference['motors'][side]
            if source['control_mode'] != 4 or source['watchdog_ticks'] != 4000:
                raise ValueError('Tianji reference must use mode4 and watchdog4000')
            for field in ('speed_rad_s', 'accel_rad_s2', 'current_fraction',
                          'max_current_a', 'max_temperature_c', 'kp', 'kd'):
                motor['profile'][field] = source[field]
            if source['travel_margin_rad'] != motor['profile']['margin_rad']:
                raise ValueError('reference margin differs from measured envelope')
            if round(source['feedback_timeout_s']*1000) != motor['profile']['feedback_timeout_ms']:
                raise ValueError('reference feedback timeout differs')
        document['mode_migration'].update(limits_preserved=False,
            reference='TJ_FX/Marvin-VR-Teleop/gripper/config.json',
            native_build_required='kuavo-dm-r6-20261014', tianji_control_parameters_copied=True)
    load_runtime_config(document, require_commissioned=False)
    return document


def _validate_saved_mark(mark, side):
    if not isinstance(mark, dict):
        raise ValueError(side + ': malformed endpoint measurement')
    for field in ('position_rad', 'xout_rad'):
        finite(mark.get(field), field)
    if (type(mark.get('received_ns')) is not int or mark['received_ns'] < 0
            or type(mark.get('sequence')) is not int or mark['sequence'] < 1
            or type(mark.get('xout_received_ns')) is not int or mark['xout_received_ns'] < 0):
        raise ValueError(side + ': actual measurement timestamps and sequence required')
    if type(mark.get('state')) is not int or mark['state'] not in (0, 1):
        raise ValueError(side + ': endpoint captured in a fault state')


def _validate_pair(marks, pmax, margin, minimum):
    if set(marks) != {'open', 'closed'}:
        raise ValueError('capture both open and closed endpoints first (o/c)')
    minimum = finite(minimum, 'minimum travel')
    if minimum < math.radians(.5):
        raise ValueError('minimum travel must be at least 0.5 degrees')
    for mark in marks.values():
        _validate_saved_mark(mark, 'calibration')
    opened, closed = marks['open']['position_rad'], marks['closed']['position_rad']
    if max(abs(opened), abs(closed)) >= min(math.pi-.02, pmax):
        raise ValueError('endpoints must remain within cold-start single-turn range and PMAX')
    travel = closed-opened
    xout_travel = marks['closed']['xout_rad']-marks['open']['xout_rad']
    if abs(travel) < minimum or abs(xout_travel) < minimum or abs(travel) <= 2*margin:
        raise ValueError('measured travel too small or cannot contain endpoint margins')
    if travel*xout_travel <= 0 or not .5 <= abs(xout_travel/travel) <= 1.5:
        raise ValueError('motor feedback and output-shaft travel disagree')
    if marks['open']['sequence'] == marks['closed']['sequence']:
        raise ValueError('endpoints must use two distinct received feedback samples')
    return opened, closed


class ManualCalibration:
    """Capture measured endpoints; caller owns all hardware/transparent mode."""

    def __init__(self, side, bus, motor_id, master_id, pmax, *, margin_rad=.02,
                 minimum_travel_rad=math.radians(5), max_age_ms=150):
        if side not in ROUTES or (bus, motor_id, master_id) != ROUTES[side]:
            raise ValueError('calibration requires confirmed left8/right1 routes')
        self.pmax = finite(pmax, 'PMAX')
        self.margin_rad = finite(margin_rad, 'margin_rad')
        self.minimum_travel_rad = finite(minimum_travel_rad, 'minimum_travel_rad')
        if self.pmax <= 0 or self.margin_rad < .002 or self.minimum_travel_rad < math.radians(.5):
            raise ValueError('invalid calibration limits')
        if type(max_age_ms) is not int or not 50 <= max_age_ms <= 300:
            raise ValueError('max_age_ms must be 50..300')
        self.side, self.bus, self.motor_id, self.master_id = side, bus, motor_id, master_id
        self.max_age_ns = max_age_ms*1_000_000
        self.marks = {}

    def capture(self, endpoint, feedback, *, now_ns, xout_rad=None, xout_received_ns=None):
        if endpoint not in ('open', 'closed') or not isinstance(feedback, MotorFeedback):
            raise ValueError('open/closed endpoint and actual MotorFeedback required')
        if type(now_ns) is not int or not 0 <= now_ns-feedback.received_ns < self.max_age_ns:
            raise ValueError('feedback missing, stale or from the future')
        if type(xout_received_ns) is not int or not 0 <= now_ns-xout_received_ns < self.max_age_ns:
            raise ValueError('fresh output-shaft register measurement required')
        if feedback.state not in (0, 1) or max(feedback.mos_c, feedback.rotor_c) >= 60:
            raise ValueError('motor fault or overtemperature during endpoint capture')
        position = finite(feedback.position_rad, 'feedback position')
        xout = finite(xout_rad, 'output-shaft position')
        if abs(position) >= min(math.pi-.02, self.pmax):
            raise ValueError('endpoint outside persistent single-turn/PMAX range')
        mark = dict(position_rad=position, xout_rad=xout, received_ns=feedback.received_ns,
                    sequence=feedback.sequence, xout_received_ns=xout_received_ns,
                    state=feedback.state, mos_c=feedback.mos_c, rotor_c=feedback.rotor_c)
        _validate_saved_mark(mark, self.side)
        self.marks[endpoint] = mark
        return copy.deepcopy(mark)

    def build_profile(self, **limits):
        opened, closed = _validate_pair(self.marks, self.pmax, self.margin_rad, self.minimum_travel_rad)
        return MotorProfile(bus=self.bus, motor_id=self.motor_id, master_id=self.master_id,
                            open_rad=opened, closed_rad=closed, margin_rad=self.margin_rad,
                            pmax=self.pmax, **limits)

    def record(self, **limits):
        profile = self.build_profile(**limits)
        return {'profile': asdict(profile), 'commissioned': False,
                'calibration': {'source': 'measured_manual_endpoints',
                    'minimum_travel_rad': self.minimum_travel_rad,
                    'marks': copy.deepcopy(self.marks),
                    'motor_zero_written': False, 'motor_flash_written': False}}

    def status(self):
        return dict(side=self.side, marks=copy.deepcopy(self.marks),
                    can_frames_sent=0, motor_zero_written=False, motor_flash_written=False)


def save_document(path, document):
    """Atomic publish to a NEW file, durable and never replaces old evidence."""
    path = Path(path)
    encoded = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.'+path.name+'.', delete=False) as output:
            temporary = output.name
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        # link is atomic and fails if destination exists, including a symlink.
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary:
            os.unlink(temporary)
    return path


def save_runtime_calibration(input_config, side, record, output, *, readback=None, commission=False):
    """Merge only the selected side into a NEW host configuration.

    Explicit commission marks an operator-reviewed calibration; it cannot make
    a communication-only candidate into measured endpoints. Runtime validates
    both sides before opening CAN. An unfinished other side stays unfinished.
    """
    document = read_document(input_config)
    if side not in ROUTES or side not in document.get('motors', {}):
        raise ValueError('selected side missing in source configuration')
    value = copy.deepcopy(record)
    if readback is not None:
        value['readback'] = copy.deepcopy(readback)
    value['commissioned'] = bool(commission)
    selected = copy.deepcopy(document)
    selected['motors'] = {side: value}
    load_runtime_config(selected, require_commissioned=bool(commission))
    document['motors'][side] = value
    return save_document(output, document)


def save_calibration(path, records_by_side, *, owner_directory, occupied_ids):
    document = {'schema_version': 2, 'transport': 'leju_can_owner',
                'frame_format': 'classic_standard', 'can_bitrate_bps': 1_000_000,
                'owner_directory': owner_directory, 'occupied_ids': occupied_ids,
                'motors': copy.deepcopy(records_by_side)}
    load_runtime_config(document, require_commissioned=False)
    return save_document(path, document)


def load_calibration(path):
    return load_runtime_config(path).profiles
