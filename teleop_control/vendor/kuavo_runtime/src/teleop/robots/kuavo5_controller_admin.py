"""Authenticated, explicit initialization jobs; no hardware I/O on import.

The caller serializes handle() with session creation. Slow observation/actions
run off the input thread. No request is retried; disconnect cancels jobs not yet
dispatched but cannot undo a manufacturer initialization already requested.
"""
import json
from pathlib import Path
import re
import subprocess
import threading
from .kuavo5_execution_lock import ExecutionLock

HELPER = '/usr/local/libexec/galaxis-kuavo5-init-v1/controller.py'

PREPARATION_PROTOCOL = 'kuavo_robot_preparation_v1'
PREPARATION_ACTIONS = ('status', 'start', 'initialize', 'grippers_inspect', 'grippers_prepare')
PREPARATION_REQUEST_FIELDS = frozenset(('action', 'request_id', 'confirm_motion',
    'motor_ids_confirmed', 'accept_unverified_gripper_bus', 'expected_installation_sha256'))


class PreparationHelperOutcomeUnknown(RuntimeError):
    """The fixed privileged helper may have dispatched; never retry it."""


def validate_preparation_request(request):
    if not isinstance(request, dict) or set(request) != PREPARATION_REQUEST_FIELDS:
        raise ValueError('exact preparation request fields required')
    if request['action'] not in PREPARATION_ACTIONS:
        raise ValueError('unsupported preparation action')
    for key, size in (('request_id', 32), ('expected_installation_sha256', 64)):
        if not isinstance(request[key], str) or re.fullmatch('[a-f0-9]{%d}' % size, request[key]) is None:
            raise ValueError('invalid preparation identity')
    if any(type(request[name]) is not bool for name in (
            'confirm_motion', 'motor_ids_confirmed', 'accept_unverified_gripper_bus')):
        raise ValueError('preparation confirmations must be explicit booleans')
    action = request['action']
    if action in ('start', 'initialize', 'grippers_prepare') and not request['confirm_motion']:
        raise ValueError('explicit motion confirmation required')
    if action == 'start' and not request['motor_ids_confirmed']:
        raise ValueError('explicit motor ID confirmation required')
    if action.startswith('grippers_') and not request['accept_unverified_gripper_bus']:
        raise ValueError('explicit shared gripper bus acceptance required')
    return dict(request)


def run_preparation_helper(helper_path, request):
    """One fixed root-owned entry, no caller-supplied CLI paths or commands."""
    request = validate_preparation_request(request)
    path = Path(helper_path)
    if (not path.is_absolute() or path.name != 'kuavo5_robot_preparation_helper.py'
            or path.parent.name != 'apps' or '..' in path.parts):
        raise ValueError('absolute installed preparation helper required')
    try:
        done = subprocess.run(['/usr/bin/sudo', '-n', '/usr/bin/python3', '-I', '-B', str(path)],
            input=json.dumps(request, allow_nan=False)+'\n', capture_output=True, text=True,
            timeout=180, env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'})
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise PreparationHelperOutcomeUnknown('preparation helper outcome unknown; inspect status, do not retry') from exc
    try:
        if len(done.stdout) > 131072:
            raise ValueError('helper response too large')
        result = json.loads(done.stdout, parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite helper result')))
        if (not isinstance(result, dict)
                or result.get('event') != 'robot_preparation_result'
                or result.get('action') != request['action']
                or result.get('request_id') != request['request_id']
                or type(result.get('ok')) is not bool
                or type(result.get('outcome_unknown')) is not bool):
            raise ValueError('helper response identity/envelope mismatch')
        result['helper_exit_code'] = done.returncode
        return result
    except (ValueError, TypeError) as exc:
        raise PreparationHelperOutcomeUnknown('preparation helper returned no reliable structured result') from exc


class PreparationAdmin:
    """New r8 path. The root helper alone holds the shared execution lock.

    The supervisor serializes handle() with session admission. Status observes
    metadata only; it must never open the gripper CAN owner. Uncertain dispatch
    latches admission closed and is never cleared by an ordinary status query.
    """
    def __init__(self, helper_path=None, *, runner=None):
        if runner is None and helper_path is None:
            raise ValueError('fixed installed helper required')
        self.runner = runner or (lambda request: run_preparation_helper(helper_path, request))
        self.jobs, self.job, self.closed, self.uncertain = {}, None, False, False
        self.ledger = None

    def bind_ledger(self, ledger):
        if self.jobs or (self.ledger is not None and self.ledger is not ledger):
            raise ValueError('preparation ledger must be bound before accepting requests')
        self.ledger = ledger

    def settle(self):
        """Supervisor thread commits known completion; worker never writes ledger."""
        job = self.job
        if not job or not job['done'].is_set() or not job.get('ledger_reserved'):
            return
        if job.get('outcome_unknown'):
            return
        try:
            self.ledger.release_preparation(job['request']['request_id'])
            job['ledger_reserved'] = False
        except Exception:
            # Includes listener closure before the helper returned. Retain the
            # on-disk marker; restarting the listener must not erase ambiguity.
            self.uncertain = True
            job['outcome_unknown'] = True

    @property
    def busy(self):
        self.settle()
        return self.job is not None and not self.job['done'].is_set()

    @property
    def blocked_reason(self):
        self.settle()
        return 'preparation_outcome_unknown' if self.uncertain else None

    def disconnect(self, owner):
        if self.job is not None and self.job['owner'] == owner:
            self.job['cancel'].set()

    def shutdown(self):
        self.closed = True
        if self.job is not None:
            self.job['cancel'].set()
        self.settle()

    def _reply(self, job, service_instance_id, installation_sha256):
        reply = dict(event='preparation_result' if job['done'].is_set() else 'preparation_pending',
            request_id=job['request']['request_id'], action=job['request']['action'],
            service_instance_id=service_instance_id, installation_sha256=installation_sha256)
        if job['done'].is_set():
            reply.update(result=job['result'], error=job['error'])
        return reply

    def handle(self, owner, packet, *, session_available, service_instance_id, installation_sha256):
        identity = {'expected_service_instance_id', 'expected_installation_sha256'}
        if (not isinstance(packet, dict) or not identity <= set(packet)
                or packet['expected_service_instance_id'] != service_instance_id
                or packet['expected_installation_sha256'] != installation_sha256
                or not isinstance(installation_sha256, str)
                or re.fullmatch('[a-f0-9]{64}', installation_sha256) is None):
            return dict(event='refused', reason='preparation_service_identity_mismatch')
        if packet.get('event') == 'preparation_job':
            if set(packet) != identity | {'event', 'request_id'}:
                return dict(event='refused', reason='invalid_preparation_poll')
            rid = packet.get('request_id')
            if not isinstance(rid, str) or rid not in self.jobs:
                return dict(event='refused', reason='unknown_preparation_request')
            return self._reply(self.jobs[rid], service_instance_id, installation_sha256)
        if set(packet) != PREPARATION_REQUEST_FIELDS | {'event', 'expected_service_instance_id'} or packet.get('event') != 'preparation_request':
            return dict(event='refused', reason='invalid_preparation_request')
        try:
            request = validate_preparation_request({key: packet[key] for key in PREPARATION_REQUEST_FIELDS})
        except ValueError:
            return dict(event='refused', reason='explicit_valid_preparation_request_required')
        action, rid = request['action'], request['request_id']
        if self.closed or self.busy or (action != 'status' and (not session_available or self.uncertain)):
            return dict(event='refused', reason='preparation_or_session_busy')
        if rid in self.jobs or len(self.jobs) >= 10000:
            return dict(event='refused', reason='preparation_request_not_replayable')
        mutating = action in ('start', 'initialize', 'grippers_prepare')
        job = dict(request=request, owner=owner, cancel=threading.Event(), done=threading.Event(),
                   result=None, error=None, ledger_reserved=False, outcome_unknown=False)
        if mutating and self.ledger is not None:
            try:
                self.ledger.reserve_preparation(rid, action, installation_sha256)
                job['ledger_reserved'] = True
            except Exception:
                self.uncertain = True
                return dict(event='refused', reason='preparation_owner_marker_failed')
        self.jobs[rid], self.job = job, job

        def execute():
            dispatched = False
            try:
                if job['cancel'].is_set():
                    raise RuntimeError('request cancelled before dispatch')
                dispatched = True
                result = self.runner(dict(request))
                if not isinstance(result, dict):
                    raise PreparationHelperOutcomeUnknown('invalid helper result')
                job['result'] = result
                if mutating and result.get('outcome_unknown') is True:
                    self.uncertain = True
                    job['outcome_unknown'] = True
                if result.get('ok') is False or result.get('error') is not None or result.get('helper_exit_code', 0) != 0:
                    job['error'] = dict(type='PreparationFailed', message='robot preparation did not complete',
                        outcome_unknown=result.get('outcome_unknown') is True)
            except Exception as exc:
                unknown = isinstance(exc, PreparationHelperOutcomeUnknown) or (mutating and dispatched)
                self.uncertain = self.uncertain or (mutating and unknown)
                job['outcome_unknown'] = mutating and unknown
                job['error'] = dict(type=type(exc).__name__, message=str(exc), outcome_unknown=unknown)
            finally:
                job['done'].set()
        try:
            threading.Thread(target=execute, name='kuavo-robot-preparation', daemon=False).start()
        except Exception as exc:
            job['error'] = dict(type=type(exc).__name__, message=str(exc), outcome_unknown=False)
            job['done'].set()
        return self._reply(job, service_instance_id, installation_sha256)


def run_helper(action):
    if action not in ('status', 'prepare', 'initialize'):
        raise ValueError('unsupported controller action')
    result = subprocess.run(['/usr/bin/sudo', '-n', '/usr/bin/python3', '-B', HELPER, action],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
        env={'PATH':'/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE':'1'})
    if result.returncode:
        raise RuntimeError((result.stdout or result.stderr)[-1500:])
    return json.loads(result.stdout)


class ControllerAdmin:
    def __init__(self, runner=run_helper, lock_factory=lambda: ExecutionLock(route='ssh')):
        self.runner, self.lock_factory = runner, lock_factory
        self.job = None
        self.seen = set()
        self.results = {}
        self.closed = False

    @property
    def busy(self):
        return self.job is not None and not self.job['done'].is_set()

    def disconnect(self, owner):
        if self.job is not None and self.job['owner'] == owner:
            self.job['cancel'].set()

    def shutdown(self):
        self.closed = True
        if self.job is not None:
            self.job['cancel'].set()

    def handle(self, owner, packet, session_available):
        if packet.get('event') == 'controller_job':
            if (set(packet) != {'event', 'request_id'} or not isinstance(packet['request_id'], str)
                    or packet['request_id'] not in self.results):
                return dict(event='refused', reason='unknown_controller_request')
            job = self.results[packet['request_id']]
            if not job['done'].is_set():
                return dict(event='controller_pending', request_id=job['id'])
            return dict(event='controller_result', request_id=job['id'],
                        action=job['action'], result=job['result'], error=job['error'])
        if set(packet) != {'event', 'action', 'request_id', 'confirm_motion'}:
            return dict(event='refused', reason='invalid_controller_request')
        action, rid = packet['action'], packet['request_id']
        if (action not in ('status', 'prepare', 'initialize') or not isinstance(rid, str)
                or re.fullmatch('[a-f0-9]{32}', rid) is None
                or type(packet['confirm_motion']) is not bool
                or (action != 'status' and packet['confirm_motion'] is not True)):
            return dict(event='refused', reason='explicit_valid_controller_request_required')
        if self.closed or self.busy or (action != 'status' and not session_available):
            return dict(event='refused', reason='controller_or_session_busy')
        if rid in self.seen or len(self.seen) >= 10000:
            return dict(event='refused', reason='controller_request_not_replayable')
        self.seen.add(rid)
        job = dict(id=rid, owner=owner, action=action, done=threading.Event(),
                   cancel=threading.Event(), result=None, error=None)
        self.job = job
        self.results[rid] = job

        def execute():
            try:
                if job['cancel'].is_set():
                    raise RuntimeError('request cancelled before dispatch')
                if action == 'status':
                    job['result'] = self.runner(action)
                else:
                    with self.lock_factory():
                        if job['cancel'].is_set():
                            raise RuntimeError('request cancelled before dispatch')
                        job['result'] = self.runner(action)
            except Exception as exc:
                job['error'] = type(exc).__name__+': '+str(exc)
            finally:
                job['done'].set()
        thread = threading.Thread(target=execute, name='kuavo-controller-admin', daemon=False)
        try:
            thread.start()
        except Exception as exc:
            job['error'] = str(exc)
            job['done'].set()
        return dict(event='controller_pending', request_id=rid)
