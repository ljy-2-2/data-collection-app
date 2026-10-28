"""Explicit bilateral preparation using the current installed configuration.

Import is inert. Calling prepare_grippers SENDS register/status CAN queries;
the privileged caller must own the shared execution lock and validate the r8
deployment before calling. No configuration files are changed. Only explicitly
permitted volatile mode4/TIMEOUT writes are possible, never motor control.
"""
import copy
import math
import struct
import time

from teleop.control.kuavo5_gripper_calibration import load_runtime_config
from .kuavo5_gripper_reference import require_reference, ReferenceMismatch
from .kuavo5_gripper_coordinates import resolve_coordinates
from .kuavo5_gripper_live import (
    OwnerClient, OwnerBusy, OwnerExpired, REGISTERS, inspect_motor,
    capture_calibration_sample, prepare_watchdog)


class PreparationClient(OwnerClient):
    """Restricted client: reads and two exact optional volatile writes only."""
    def __init__(self, *args, allow_volatile_writes=False, **kwargs):
        super().__init__(*args, **kwargs)
        if type(allow_volatile_writes) is not bool:
            raise ValueError('explicit Boolean volatile-write permission required')
        self.allow_volatile_writes = allow_volatile_writes
        self.parameter_outcomes = []
        self._parameter_inflight = None
        original_log = self.log

        def audited_log(event, **fields):
            if event == 'host_write_rejected' and self._parameter_inflight is not None:
                self._parameter_inflight['owner_rejection'] = fields.get('result')
            original_log(event, **fields)
        self.log = audited_log

    def send(self, cid, data, *, deadline_ns=None):
        read = (cid == 0x7ff and isinstance(data, bytes) and len(data) == 8
                and data[:2] == struct.pack('<H', self.mid) and data[4:] == bytes(4)
                and ((data[2] == 0x33 and data[3] in set(REGISTERS) | {0x50, 0x51})
                     or (data[2] == 0xcc and data[3] == 0)))
        write = (self.allow_volatile_writes and cid == 0x7ff and isinstance(data, bytes)
                 and data in (struct.pack('<HBBI', self.mid, 0x55, 10, 4),
                              struct.pack('<HBBI', self.mid, 0x55, 9, 4000)))
        if write:
            # No outer retries for parameter writes or ambiguous ACKs.
            outcome = dict(parameter='mode4' if data[3] == 10 else 'watchdog4000',
                           outcome='submission_attempted')
            self.parameter_outcomes.append(outcome)
            self._parameter_inflight = outcome
            try:
                result = super().send(cid, data, deadline_ns=deadline_ns)
            except (OwnerBusy, OwnerExpired):
                outcome['outcome'] = 'not_sent'
                raise
            except Exception:
                # The native owner explicitly rejects these before CAN I/O.
                # Transport error 8 and missing/invalid ACK remain ambiguous.
                outcome['outcome'] = ('not_sent' if outcome.get('owner_rejection') in
                                      (1, 2, 3, 4, 5, 6, 7) else 'unknown')
                raise
            finally:
                self._parameter_inflight = None
            outcome['outcome'] = 'host_accepted'
            return result
        if not read:
            raise ValueError('preparation allows reads and explicit mode4/TIMEOUT4000 only')
        end = time.monotonic_ns() + 1_000_000_000
        if deadline_ns is not None:
            end = min(end, deadline_ns)
        attempt = 0
        while True:
            try:
                return super().send(cid, data, deadline_ns=end)
            except (OwnerBusy, OwnerExpired) as exc:
                remaining = (end-time.monotonic_ns())/1e9
                if remaining <= 0:
                    raise
                delay = min((.003, .007, .011, .017)[attempt % 4], remaining)
                attempt += 1
                self.log('preparation_read_retry', attempt=attempt,
                         previous_request_unsent=True, error=str(exc))
                time.sleep(delay)
                if time.monotonic_ns() >= end:
                    raise

    def commission(self, *args, **kwargs):
        raise ValueError('preparation cannot commission or enable motor control')


def _verify_values(profile, motor, values, *, allow_mode1):
    expected = {key: getattr(profile, key) for key in
                ('motor_id', 'master_id', 'pmax', 'vmax', 'tmax', 'imax_a')}
    expected['bitrate_code'] = 4
    expected.update({key: motor['readback'][key] for key in
                     ('hardware_version', 'software_version', 'sub_version')})
    for key, value in expected.items():
        if values.get(key) != value:
            raise ValueError('current identity/limits differ: ' + key)
    if values.get('mode') not in ((1, 4) if allow_mode1 else (4,)):
        raise ValueError('unsupported current mode; requires mode4 (or disabled mode1 preparation)')
    if type(values.get('watchdog_ticks')) is not int:
        raise ValueError('invalid watchdog readback')


def _sample_summary(profile, measured, motor):
    feedback = measured['feedback']
    position = measured['position_rad']
    if feedback.state != 0:
        raise ValueError('motor must already be disabled; no automatic disable')
    numbers = (position, feedback.position_rad, feedback.velocity_rad_s,
               feedback.mos_c, feedback.rotor_c)
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in numbers):
        raise ValueError('invalid motor feedback')
    stamp = feedback.received_ns
    now = time.monotonic_ns()
    if type(stamp) is not int or not 0 < stamp <= now or now-stamp > profile.feedback_timeout_ms*1_000_000:
        raise ValueError('motor feedback stale or future')
    if abs(feedback.velocity_rad_s) > max(.1, 2*profile.vmax/4095):
        raise ValueError('motor moving during preparation')
    if max(feedback.mos_c, feedback.rotor_c) >= profile.max_temperature_c:
        raise ValueError('motor temperature exceeds preparation limit')
    if abs(position-feedback.position_rad) > max(.002, 3*2*profile.pmax/65535):
        raise ValueError('motor moved while position was read')
    coordinates = resolve_coordinates(motor, measured, now_ns=now)
    reference = coordinates.report
    canonical = coordinates.to_canonical(position)
    canonical_feedback = coordinates.to_canonical(feedback.position_rad)
    inside = all(profile.bounds[0] <= p <= profile.bounds[1]
                 for p in (canonical, canonical_feedback))
    if not inside:
        raise ValueError('position outside installed soft bounds; no automatic movement')
    return dict(position_rad=canonical, raw_position_rad=position, xout_rad=measured['xout_rad'],
                position_reference=reference, state=feedback.state, received_ns=stamp,
                inside_soft_bounds=inside, soft_bounds_rad=list(profile.bounds))


def prepare_grippers(document, *, allow_volatile_writes=False,
                     accept_unverified_shared_bus=False, log,
                     client_factory=None, inspect=inspect_motor,
                     sample=capture_calibration_sample, prepare=prepare_watchdog):
    """Perform one locked, explicit inspection/preparation; report partial failure.

    The caller supplies log(event, **fields), owns the execution lock, validates
    the installation and r8 owner before entry, and binds the returned evidence
    to boot/PID/config identity. Historical readbacks are not current readiness.
    """
    sides = {side: dict(stage='not_checked', status='not_checked', ready=False,
                       owner_pid=None, readback=None, error=None, cleanup_error=None,
                       writes_attempted=[], writes_confirmed=[], parameter_outcomes=[])
             for side in ('left', 'right')}
    result = dict(ready=False, sides=sides, error=None, cleanup_errors=[],
                  clients_closed=True, configuration_changed=False,
                  formal_configuration_changed=False,
                  volatile_parameter_write_attempted=False,
                  outcome_unknown=False,
                  motor_control_sent=False, flash_written=False,
                  shared_bus_compatibility_verified=False,
                  readiness_scope='observed_preparation_only_not_motion_acceptance')
    clients = {}

    def failure(side, exc):
        record = sides[side]
        record['status'] = 'failed'
        record['ready'] = False
        record['error'] = type(exc).__name__ + ': ' + str(exc)
        if isinstance(exc, ReferenceMismatch):
            record['position_reference'] = copy.deepcopy(exc.report)
            log('gripper_reference_rejected', side=side, report=exc.report)

    def stage(side, value):
        sides[side]['stage'] = value
        log('gripper_preparation_stage', side=side, stage=value)

    try:
        if type(allow_volatile_writes) is not bool or type(accept_unverified_shared_bus) is not bool:
            raise ValueError('explicit Boolean preparation permissions required')
        if not accept_unverified_shared_bus:
            raise ValueError('explicit shared-bus test acceptance required before CAN queries')
        config = load_runtime_config(document)
        if set(config.profiles) != {'left', 'right'}:
            raise ValueError('installed bilateral configuration required')
        for side, profile in config.profiles.items():
            if profile.control_mode != 4:
                raise ValueError(side+': network preparation supports installed mode4 only')
            for key in ('hardware_version', 'software_version', 'sub_version'):
                if type(config.document['motors'][side]['readback'].get(key)) is not int:
                    raise ValueError(side+': installed motor identity missing: '+key)
        if client_factory is None:
            client_factory = lambda *args: PreparationClient(
                *args, allow_volatile_writes=allow_volatile_writes)

        # Inspect both sides completely before permitting the first write.
        for side in ('left', 'right'):
            profile, record = config.profiles[side], sides[side]
            try:
                stage(side, 'connect')
                client = client_factory(config.owner_directory, side,
                    lambda event, side=side, **fields: log(event, side=side, **fields))
                clients[side] = client
                client.connect()
                if type(client.pid) is not int or client.pid <= 0:
                    raise ValueError('invalid CAN owner peer PID')
                record['owner_pid'] = client.pid
                if not client.control:
                    raise ValueError('running CAN owner is read-only')
                stage(side, 'preflight_identity')
                values = inspect(client)
                record['readback'] = copy.deepcopy(values)
                _verify_values(profile, config.document['motors'][side], values, allow_mode1=True)
                stage(side, 'preflight_disabled_position')
                record.update(_sample_summary(profile, sample(client, values), config.document['motors'][side]))
                record['status'] = 'preflight_passed'
            except Exception as exc:
                failure(side, exc)
        if any(record['error'] is not None for record in sides.values()):
            return result
        if sides['left']['owner_pid'] != sides['right']['owner_pid']:
            for side in sides:
                sides[side]['stage'] = 'owner_identity'
                failure(side, ValueError('left/right sockets have different owner PIDs'))
            return result

        for side in ('left', 'right'):
            profile, record, client = config.profiles[side], sides[side], clients[side]
            motor = config.document['motors'][side]
            try:
                stage(side, 'refresh_before_prepare')
                values = inspect(client)
                record['readback'] = copy.deepcopy(values)
                _verify_values(profile, motor, values, allow_mode1=True)
                record.update(_sample_summary(profile, sample(client, values), motor))
                if allow_volatile_writes:
                    if values['mode'] != 4:
                        stage(side, 'mode4_write')
                        client.refresh_status(expected_state=0)
                        origin = client.register(0x50, 'f')
                        record['writes_attempted'].append('mode4')
                        client.send(0x7ff, struct.pack('<HBBI', profile.motor_id, 0x55, 10, 4))
                        stage(side, 'mode4_readback')
                        if client.register(10) != 4:
                            raise ValueError('mode4 write not confirmed; no automatic retry/restore')
                        record['writes_confirmed'].append('mode4')
                        values = inspect(client)
                        record['readback'] = copy.deepcopy(values)
                        _verify_values(profile, motor, values, allow_mode1=False)
                        measured = sample(client, values)
                        record.update(_sample_summary(profile, measured, motor))
                        if abs(measured['position_rad']-origin) > max(.002, 3*2*profile.pmax/65535):
                            raise ValueError('disabled position changed across mode write')
                    stage(side, 'watchdog_prepare')
                    if values['watchdog_ticks'] != 4000:
                        record['writes_attempted'].append('watchdog4000')
                    prepare(client, values)
                stage(side, 'final_readback')
                values = inspect(client)  # Independent read; never trust prepare's return.
                record['readback'] = copy.deepcopy(values)
                _verify_values(profile, motor, values, allow_mode1=True)
                record.update(_sample_summary(profile, sample(client, values), motor))
                if values['watchdog_ticks'] == 4000 and 'watchdog4000' in record['writes_attempted']:
                    record['writes_confirmed'].append('watchdog4000')
                record['ready'] = values['mode'] == 4 and values['watchdog_ticks'] == 4000
                record['status'] = 'ready' if record['ready'] else 'needs_preparation'
                if allow_volatile_writes and not record['ready']:
                    raise ValueError('mode4/TIMEOUT4000 final readback not confirmed')
                record['stage'] = 'complete'
            except Exception as exc:
                failure(side, exc)
                # Keep partial evidence; never continue writes on the other side.
                break
        result['ready'] = all(record['ready'] for record in sides.values())
        return result
    except Exception as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        return result
    finally:
        for side, client in clients.items():
            sides[side]['parameter_outcomes'] = copy.deepcopy(getattr(client, 'parameter_outcomes', []))
            try:
                client.close()
            except Exception as exc:
                detail = dict(side=side, error=type(exc).__name__+': '+str(exc))
                sides[side]['cleanup_error'] = detail['error']
                sides[side]['ready'] = False
                result['cleanup_errors'].append(detail)
                result['clients_closed'] = False
        result['volatile_parameter_write_attempted'] = any(
            record['writes_attempted'] for record in sides.values())
        result['outcome_unknown'] = bool(result['cleanup_errors']) or any(
            attempt['outcome'] in ('submission_attempted', 'unknown', 'host_accepted')
            and attempt['parameter'] not in record['writes_confirmed']
            for record in sides.values() for attempt in record['parameter_outcomes'])
        if result['cleanup_errors']:
            result['ready'] = False
