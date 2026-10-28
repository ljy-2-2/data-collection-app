"""One fixed, locally installed teleoperation bundle per isolated process.

The listener must explicitly start a worker; construction imports no ROS and
starts no processes. Clients cannot supply commands, code, paths or profiles.
EOF requests the existing core's cleanup. No kill/terminate fallback is used.
"""
from __future__ import annotations

from collections import deque
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from teleop.robots.kuavo5_session_profile import bundle_profile, require_root_owned

FIXED_PROFILE = dict(target_recovery='bounded_candidate', speed_profile='responsive',
                     arm_posture='elbow_preference', gripper_mode='off')
MAX_BUNDLE_BYTES = 1_048_576
MAX_PACKET_BYTES = 65_536


def effective_session_bundle(bundle, installation_sha256, snapshot):
    """Derive scalar overrides without changing the verified source archive."""
    from teleop.robots.kuavo5_session_settings import validate_snapshot, apply_settings
    snapshot = validate_snapshot(bundle, installation_sha256, snapshot)
    effective = apply_settings(bundle, snapshot['values'])
    effective['runtime_settings_identity'] = dict(revision=snapshot['revision'],
        settings_sha256=snapshot['settings_sha256'])
    return effective


def read_reserved_settings(bundle, manifest_path, owner_uid, *, ledger_reader=None):
    """Read the parent's frozen reservation AFTER acquiring the execution lock.

    No client paths or code are accepted. The privileged worker revalidates all
    numbers against its own immutable installation, not the PC's claimed bounds.
    """
    from teleop.robots.kuavo5_execution_lock import _network_owner, NETWORK_OWNER_PATH
    owner = (ledger_reader or _network_owner)(NETWORK_OWNER_PATH, owner_uid)
    if (not isinstance(owner, dict) or owner.get('active') is not True
            or owner.get('operation') == 'preparation' or 'session_settings' not in owner):
        raise ValueError('active session lacks frozen session settings')
    installation = hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
    return effective_session_bundle(bundle, installation, owner['session_settings'])


def read_installed_bundle(path, expected_sha256):
    """Read server-owned installation data, never code supplied by a client."""
    if not isinstance(expected_sha256, str) or re.fullmatch('[a-f0-9]{64}', expected_sha256) is None:
        raise ValueError('pinned bundle SHA256 required')
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('absolute installed bundle path required')
    with path.open('rb') as source:
        raw = source.read(MAX_BUNDLE_BYTES + 1)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError('installed bundle too large')
    bundle = json.loads(raw)
    if not isinstance(bundle, dict):
        raise ValueError('installed bundle must be an object')
    profile = bundle_profile(bundle)
    extra = {'gripper_config', 'gripper_config_sha256'} if profile['gripper_mode'] == 'damiao' else set()
    if 'base_mode' in profile:
        extra.add('base_mode')
    if 'return_home_config_sha256' in profile:
        extra |= {'return_home_config', 'return_home_config_sha256'}
    if set(bundle) != {'mode', 'sources', 'sha256', *FIXED_PROFILE} | extra:
        raise ValueError('exact installed bundle metadata required')
    if bundle['mode'] not in ('validate', 'execute'):
        raise ValueError('only saved 0.7 bounded elbow-preference profile is supported')
    sources = bundle['sources']
    if not isinstance(sources, dict) or not sources or any(
            not isinstance(k, str) or not k.startswith('teleop.') or not isinstance(v, str)
            for k, v in sources.items()):
        raise ValueError('invalid installed source manifest')
    actual = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    if actual != expected_sha256 or actual != bundle['sha256']:
        raise ValueError('installed bundle integrity mismatch')
    return bundle


def _packet_digest(packet):
    return hashlib.sha256(json.dumps(packet, sort_keys=True, allow_nan=False,
        separators=(',', ':')).encode()).hexdigest()


def _activity_scope_valid(evidence):
    """Old receipts remain conservative; malformed new observations never pass."""
    if 'activity_scope' not in evidence:
        return True
    scope = evidence['activity_scope']
    counts = ('arm_publications', 'base_publications', 'other_publications',
              'nonbaseline_control_calls')
    return (isinstance(scope, dict) and set(scope) == {'version', *counts}
        and type(scope['version']) is int and scope['version'] == 1
        and all(type(scope[k]) is int and scope[k] >= 0 for k in counts)
        and sum(scope[k] for k in counts[:3]) == evidence.get('publication_attempts')
        and type(evidence.get('control_call_count')) is int
        and scope['nonbaseline_control_calls'] <= evidence['control_call_count'])


def _arm_hold_required(packet, evidence, base_mode):
    """Exclude only independently observed base-only traffic from arm cleanup.

    No exemption for legacy receipts, unknown topics, any arm publication or
    attempted non-baseline service call (including failed/rolled-back calls).
    This proves software scope, never the physical state of the robot.
    """
    report = packet['transport']
    scope = evidence.get('activity_scope')
    if scope and scope['nonbaseline_control_calls']:
        return True
    if not (evidence['publication_attempts'] or report.get('publish_attempts')):
        return False
    base = packet.get('base_transport')
    if not isinstance(base, dict):
        base = {}
    base_only = (scope is not None and base_mode == 'vendor_vr_x'
        and scope['base_publications'] > 0
        and scope['base_publications'] == base.get('publish_attempts')
        and scope['arm_publications'] == scope['other_publications'] == 0
        and type(report.get('publish_attempts')) is int and report['publish_attempts'] == 0
        and type(report.get('ros_message_attempts')) is int
        and report['ros_message_attempts'] == 0
        and report.get('mode') == 'unknown' and report.get('hold_acknowledged') is False)
    return not base_only


def cleanup_report_ok(packet, receipt=None, bundle_sha256=None, *, gripper_mode='off', gripper_config_sha256=None,
                      settings_identity=None, base_mode='disabled'):
    """Require independent post-return software evidence; retain runtime faults.

    A failed run can finish cleanup successfully. A successful run can also fail
    cleanup. Neither exit code alone authorizes release of session ownership.
    """
    if not isinstance(packet, dict) or packet.get('event') != 'closed' \
            or type(packet.get('exit_code')) is not int or packet['exit_code'] not in (0, 1):
        return False
    report = packet.get('transport')
    if not isinstance(report, dict) or report.get('compute_cleanup_error') \
            or report.get('mode_request_inflight') is not None \
            or report.get('mode_request_pending') is not None:
        return False
    attempts = report.get('publish_attempts')
    if type(attempts) is not int or attempts < 0:
        return False
    if not isinstance(receipt, dict) or receipt.get('event') != 'worker_cleanup' \
            or type(receipt.get('version')) is not int or receipt['version'] != 1 \
            or type(receipt.get('core_exit_code')) is not int \
            or receipt.get('core_exit_code') != packet['exit_code'] \
            or receipt.get('closed_sha256') != _packet_digest(packet) \
            or receipt.get('bundle_sha256') != bundle_sha256 \
            or not isinstance(bundle_sha256, str) \
            or receipt.get('physical_stop_verified') is not False:
        return False
    if receipt.get('stage') == 'before_core':
        return (receipt.get('core_returned') is False
            and receipt.get('initialization_not_started') is True
            and receipt.get('ros_started') is False
            and receipt.get('physical_robot_command_sent') is False
            and packet['exit_code'] == 1 and attempts == 0
            and report.get('profile_configured') is False
            and report.get('worker_start_refused') is True)
    if receipt.get('core_returned') is not True:
        return False
    if settings_identity is not None and receipt.get('session_settings') != settings_identity:
        return False
    if base_mode == 'vendor_vr_x':
        base = packet.get('base_transport')
        if (not isinstance(base, dict) or base.get('closed') is not True
                or base.get('errors') != [] or base.get('physical_stop_verified') is not False
                or type(base.get('publish_attempts')) is not int or base['publish_attempts'] < 0
                or type(base.get('stop_observed')) is not bool):
            return False
        # Cleanup can be complete even if stopping could not be observed, but
        # such a run must never be reported as a successful session.
        if packet['exit_code'] == 0 and base['publish_attempts'] and not base['stop_observed']:
            return False
    if gripper_mode == 'damiao':
        observed = receipt.get('gripper_evidence')
        if (receipt.get('gripper_config_sha256') != gripper_config_sha256
                or not isinstance(observed, dict)
                or type(observed.get('sessions_created')) is not int
                or observed['sessions_created'] not in (0, 1)
                or any(observed.get(k) is not True for k in (
                    'sessions_closed', 'threads_exited', 'clients_closed', 'no_possible_enable', 'no_errors'))):
            return False
        gripper = packet.get('gripper_transport')
        if observed['sessions_created']:
            if (not isinstance(gripper, dict) or gripper.get('closed') is not True
                    or gripper.get('errors') != []):
                return False
        elif gripper is not None:
            return False
    evidence = receipt.get('evidence')
    if not isinstance(evidence, dict) or any(evidence.get(k) is not True for k in (
            'ros_shutdown_returned', 'ros_handles_unregistered',
            'control_calls_completed', 'child_processes_exited', 'observation_complete')):
        return False
    publications, calls = evidence.get('publication_attempts'), evidence.get('control_call_count')
    if type(publications) is not int or publications < 0 or type(calls) is not int or calls < 0:
        return False
    if not _activity_scope_valid(evidence):
        return False
    if calls == 0:
        # Interrupted preparation: no actuator-facing calls or publications.
        return publications == 0 and attempts == 0 and report.get('profile_configured') is False
    if evidence.get('baseline_services_confirmed') is not True:
        return False
    if report.get('closed') is not True or type(report.get('desired_mode')) is not int \
            or report['desired_mode'] != 0 \
            or report.get('vendor_fast_profile_active') is not False:
        return False
    if _arm_hold_required(packet, evidence, base_mode):
        return report.get('mode') == 'hold' and report.get('hold_acknowledged') is True
    return True


def cleanup_failure_checks(packet, receipt, bundle_sha256, **options):
    """Explain rejected evidence using fixed codes only; never authorize cleanup.

    The existing cleanup_report_ok remains the sole acceptance predicate. These
    diagnostics deliberately omit report values, service messages and stderr,
    any of which could include a path, credential or arbitrary child output.
    """
    try:
        if cleanup_report_ok(packet, receipt, bundle_sha256, **options):
            return []
    except (TypeError, ValueError, OverflowError):
        return ['cleanup_evidence_invalid']
    failed = []
    def require(condition, code):
        if not condition:
            failed.append(code)
    require(isinstance(packet, dict), 'closed_report_missing')
    require(isinstance(receipt, dict), 'cleanup_receipt_missing')
    if not isinstance(packet, dict) or not isinstance(receipt, dict):
        return failed
    require(packet.get('event') == 'closed' and type(packet.get('exit_code')) is int
            and packet['exit_code'] in (0, 1), 'closed_report_invalid')
    report = packet.get('transport')
    if not isinstance(report, dict):
        return failed + ['transport_report_missing']
    require(not report.get('compute_cleanup_error'), 'compute_cleanup_failed')
    require(report.get('mode_request_inflight') is None, 'mode_request_inflight')
    require(report.get('mode_request_pending') is None, 'mode_request_pending')
    attempts = report.get('publish_attempts')
    require(type(attempts) is int and attempts >= 0, 'publish_attempt_count_invalid')
    require(receipt.get('event') == 'worker_cleanup'
        and type(receipt.get('version')) is int and receipt['version'] == 1
        and type(receipt.get('core_exit_code')) is int
        and receipt.get('core_exit_code') == packet.get('exit_code')
        and receipt.get('closed_sha256') == _packet_digest(packet)
        and receipt.get('bundle_sha256') == bundle_sha256
        and isinstance(bundle_sha256, str)
        and receipt.get('physical_stop_verified') is False, 'cleanup_receipt_identity_mismatch')
    if receipt.get('stage') == 'before_core':
        require(receipt.get('core_returned') is False
            and receipt.get('initialization_not_started') is True
            and receipt.get('ros_started') is False
            and receipt.get('physical_robot_command_sent') is False
            and packet.get('exit_code') == 1 and attempts == 0
            and report.get('profile_configured') is False
            and report.get('worker_start_refused') is True, 'before_core_evidence_invalid')
        return failed or ['cleanup_report_rejected']
    require(receipt.get('core_returned') is True, 'core_return_unconfirmed')
    settings = options.get('settings_identity')
    require(settings is None or receipt.get('session_settings') == settings,
            'settings_identity_mismatch')
    if options.get('base_mode', 'disabled') == 'vendor_vr_x':
        base = packet.get('base_transport')
        if not isinstance(base, dict):
            failed.append('base_transport_missing')
        else:
            require(base.get('closed') is True, 'base_resources_not_closed')
            require(base.get('errors') == [], 'base_cleanup_errors')
            require(base.get('physical_stop_verified') is False
                and type(base.get('publish_attempts')) is int and base['publish_attempts'] >= 0
                and type(base.get('stop_observed')) is bool, 'base_report_invalid')
            require(not (packet.get('exit_code') == 0 and base.get('publish_attempts')
                and not base.get('stop_observed')), 'base_stop_unobserved_on_success')
    if options.get('gripper_mode', 'off') == 'damiao':
        require(receipt.get('gripper_config_sha256') == options.get('gripper_config_sha256'),
                'gripper_config_identity_mismatch')
        grippers = receipt.get('gripper_evidence')
        if not isinstance(grippers, dict):
            failed.append('gripper_evidence_missing')
        else:
            count = grippers.get('sessions_created')
            require(type(count) is int and count in (0, 1), 'gripper_instance_count_invalid')
            for key in ('sessions_closed', 'threads_exited', 'clients_closed', 'no_possible_enable', 'no_errors'):
                require(grippers.get(key) is True, 'gripper_' + key + '_unconfirmed')
            transport = packet.get('gripper_transport')
            require((isinstance(transport, dict) and transport.get('closed') is True
                and transport.get('errors') == []) if count else transport is None,
                'gripper_transport_cleanup_unconfirmed')
    evidence = receipt.get('evidence')
    if not isinstance(evidence, dict):
        failed.append('ros_evidence_missing')
    else:
        for key in ('ros_shutdown_returned', 'ros_handles_unregistered', 'control_calls_completed',
                    'child_processes_exited', 'observation_complete'):
            require(evidence.get(key) is True, key + '_unconfirmed')
        publications, calls = evidence.get('publication_attempts'), evidence.get('control_call_count')
        valid_counts = (type(publications) is int and publications >= 0
                        and type(calls) is int and calls >= 0)
        require(valid_counts, 'ros_activity_counts_invalid')
        scope_valid = _activity_scope_valid(evidence)
        require(scope_valid, 'ros_activity_scope_invalid')
        if valid_counts and calls == 0:
            require(publications == 0 and attempts == 0 and report.get('profile_configured') is False,
                    'no_control_calls_evidence_inconsistent')
        elif valid_counts:
            require(evidence.get('baseline_services_confirmed') is True,
                    'baseline_services_unconfirmed')
            require(report.get('closed') is True and type(report.get('desired_mode')) is int
                and report['desired_mode'] == 0 and report.get('vendor_fast_profile_active') is False,
                'arm_transport_cleanup_unconfirmed')
            if scope_valid and _arm_hold_required(packet, evidence, options.get('base_mode', 'disabled')):
                require(report.get('mode') == 'hold' and report.get('hold_acknowledged') is True,
                        'arm_hold_unconfirmed')
    return failed[:32] or ['cleanup_report_rejected']


def stderr_exception_summary(lines):
    """Expose only an allowlisted exception class, never its arbitrary message."""
    allowed = frozenset(('AssertionError', 'AttributeError', 'BrokenPipeError', 'CannotSendRequest',
        'ConnectionError', 'EOFError', 'HTTPException', 'KeyError', 'OSError', 'PermissionError',
        'RemoteDisconnected', 'ResponseNotReady', 'RuntimeError', 'TimeoutError', 'TypeError',
        'ValueError', 'ROSException', 'ROSInterruptException', 'ServiceException'))
    exception_type = None
    for line in reversed(tuple(lines)[-32:]):
        if not isinstance(line, str) or len(line) > 65536:
            continue
        match = re.match(r'^(?:[A-Za-z_][A-Za-z_0-9]*\.){0,4}([A-Za-z_][A-Za-z_0-9]*):', line)
        if match and match[1] in allowed:
            exception_type = match[1]
            break
    return dict(present=bool(lines), last_exception_type=exception_type, message_omitted=True)


class _CleanupObserver:
    """Observe existing cleanup; never call a service, close a ROS handle or kill.

    Installed only inside a per-session worker after ROS (or fake ROS) exists.
    Wrappers forward the exact arguments/results and record completion order.
    A late timed-out mode setter cannot hide behind the core's mode-thread state.
    """
    BASELINE = {'/wheel_arm_change_arm_ctrl_mode': {'control_mode': 0},
                '/enable_lb_arm_quick_mode': {'quickMode': 0},
                '/enable_arm_traj_interpolator': {'data': False},
                '/change_arm_ctrl_mode': {'control_mode': 0}}

    def __init__(self, ros):
        import threading
        self.ros, self.lock = ros, threading.RLock()
        self.shutdown = False
        self.handles, self.calls, self.children = [], [], []
        self.publications, self.unknown_call, self.closed = 0, False, []
        self.publication_scope = dict(arm_publications=0, base_publications=0, other_publications=0)
        self.nonbaseline_control_calls = 0
        self.originals = {}

    def install(self):
        for name in ('Publisher', 'Subscriber', 'ServiceProxy', 'signal_shutdown'):
            self.originals[name] = getattr(self.ros, name)
        self.originals['Popen'] = subprocess.Popen
        observer = self
        class Handle:
            def __init__(self, raw, publication):
                self.raw, self.publication, self.closed = raw, publication, False
                # Resolved ROS name accounts for remapping; unknown handles
                # cannot qualify for the narrowly scoped base-only exemption.
                self.scope = {'/kuavo_arm_traj': 'arm_publications',
                              '/cmd_vel': 'base_publications'}.get(
                                  getattr(raw, 'resolved_name', None), 'other_publications')
            def __getattr__(self, name):
                return getattr(self.raw, name)
            def unregister(self):
                result = self.raw.unregister()
                with observer.lock:
                    self.closed = True
                return result
            def publish(self, *args, **kwargs):
                with observer.lock:
                    observer.publications += 1
                    observer.publication_scope[self.scope] += 1
                return self.raw.publish(*args, **kwargs)
        def handle_factory(name, publication):
            def factory(*args, **kwargs):
                value = Handle(self.originals[name](*args, **kwargs), publication)
                with self.lock:
                    self.handles.append(value)
                return value
            return factory
        self.ros.Publisher = handle_factory('Publisher', True)
        self.ros.Subscriber = handle_factory('Subscriber', False)
        def proxy(name, *args, **kwargs):
            raw = self.originals['ServiceProxy'](name, *args, **kwargs)
            class Proxy:
                def __getattr__(self, key):
                    return getattr(raw, key)
                def __call__(self_proxy, *values, **fields):
                    with observer.lock:
                        call = dict(name=name, request=dict(fields), completed=False, successful=False)
                        observer.calls.append(call)
                        if values or fields != observer.BASELINE.get(name):
                            observer.nonbaseline_control_calls += 1
                        if values or name not in observer.BASELINE:
                            observer.unknown_call = True
                    try:
                        result = raw(*values, **fields)
                        ok = (getattr(result, 'result', None) is True
                              and type(getattr(result, 'mode', None)) is int
                              and result.mode == fields['control_mode']
                              if 'control_mode' in fields else getattr(result, 'success', None) is True)
                        with observer.lock:
                            call['successful'] = ok
                        return result
                    finally:
                        with observer.lock:
                            call['completed'] = True
                            call['completion_order'] = sum(c['completed'] for c in observer.calls)
            return Proxy()
        self.ros.ServiceProxy = proxy
        def shutdown(*args, **kwargs):
            result = self.originals['signal_shutdown'](*args, **kwargs)
            with self.lock:
                self.shutdown = True
            return result
        self.ros.signal_shutdown = shutdown
        def popen(*args, **kwargs):
            result = self.originals['Popen'](*args, **kwargs)
            with self.lock:
                self.children.append(result)
            return result
        subprocess.Popen = popen

    def restore(self):
        for name, function in self.originals.items():
            if name == 'Popen':
                subprocess.Popen = function
            else:
                setattr(self.ros, name, function)

    def evidence(self):
        with self.lock:
            final = {}
            for call in sorted(self.calls, key=lambda c: c.get('completion_order', 0)):
                if call['completed']:
                    final[call['name']] = call
            baseline = all(name in final and final[name]['successful'] is True
                           and final[name]['request'] == value for name, value in self.BASELINE.items())
            return dict(ros_shutdown_returned=self.shutdown,
                ros_handles_unregistered=all(h.closed for h in self.handles),
                control_calls_completed=all(c['completed'] for c in self.calls),
                control_call_count=len(self.calls), baseline_services_confirmed=baseline,
                publication_attempts=self.publications,
                activity_scope=dict(version=1, **self.publication_scope,
                    nonbaseline_control_calls=self.nonbaseline_control_calls),
                child_processes_exited=all(p.poll() is not None for p in self.children),
                observation_complete=not self.unknown_call,
                last_control_completions={name: dict(request=c['request'], successful=c['successful'])
                                          for name, c in final.items()})


def run_core_with_cleanup_receipt(bundle, runner):
    """Run the unchanged core and append evidence only after its final return."""
    import rospy
    observer = _CleanupObserver(rospy)
    gripper_instances = []
    gripper_module = original_gripper_class = None
    if bundle.get('gripper_mode') == 'damiao':
        from teleop.robots import kuavo5_gripper_owner as gripper_module
        original_gripper_class = gripper_module.OwnerGripperSession
        class ObservedGripper(original_gripper_class):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                gripper_instances.append(self)
        gripper_module.OwnerGripperSession = ObservedGripper
    original_stdout = sys.stdout
    class Writer:
        def __getattr__(self, name):
            return getattr(original_stdout, name)
        def write(self, text):
            # Core emits one encoded packet per write. Only closed is captured;
            # compressed diagnostic packets remain completely unchanged.
            try:
                value = json.loads(text)
                if isinstance(value, dict) and value.get('event') == 'closed':
                    observer.closed.append(value)
            except (TypeError, ValueError):
                pass
            return original_stdout.write(text)
        def flush(self):
            return original_stdout.flush()
    observer.install()
    sys.stdout = Writer()
    try:
        result = runner()
        if type(result) is int and result in (0, 1) and len(observer.closed) == 1:
            receipt = dict(event='worker_cleanup', version=1,
                bundle_sha256=bundle['sha256'], core_returned=True, core_exit_code=result,
                closed_sha256=_packet_digest(observer.closed[0]),
                evidence=observer.evidence(), physical_stop_verified=False)
            if 'runtime_settings_identity' in bundle:
                receipt['session_settings'] = dict(bundle['runtime_settings_identity'])
            if bundle.get('gripper_mode') == 'damiao':
                receipt.update(gripper_config_sha256=bundle['gripper_config_sha256'],
                    gripper_evidence=dict(sessions_created=len(gripper_instances),
                        sessions_closed=all(g._closed for g in gripper_instances),
                        threads_exited=all(not t.is_alive() for g in gripper_instances for t in g._threads.values()),
                        clients_closed=all(c.sock is None for g in gripper_instances for c in g._clients.values()),
                        no_possible_enable=all(not v for g in gripper_instances for v in g._possibly_enabled.values()),
                        no_errors=all(not g._errors for g in gripper_instances)))
            original_stdout.write(json.dumps(receipt, allow_nan=False, separators=(',', ':')) + '\n')
            original_stdout.flush()
        return result
    finally:
        sys.stdout = original_stdout
        observer.restore()
        if gripper_module is not None:
            gripper_module.OwnerGripperSession = original_gripper_class


class SessionWorker:
    """Nonblocking parent-side process bridge. Call poll_events every loop tick.

    backend=fake requires an explicitly installed test fixture. backend=robot
    requires allow_execute=True and is only for an operator-started deployment.
    A completed worker is never reused; reconnect must create another instance.
    """
    def __init__(self, installed_bundle, expected_sha256, *, backend='disabled',
                 allow_execute=False, python_executable=sys.executable,
                 fixture_path=None, process_factory=subprocess.Popen,
                 clock=time.monotonic, session_settings=None, installation_sha256=None):
        if backend not in ('disabled', 'fake', 'robot') or type(allow_execute) is not bool:
            raise ValueError('explicit fake/robot worker backend required')
        if backend == 'robot' and not allow_execute:
            raise ValueError('robot worker requires explicit server execution permission')
        if backend == 'fake' and fixture_path is None:
            raise ValueError('fake worker requires explicit local test fixture')
        if backend == 'robot' and fixture_path is not None:
            raise ValueError('fake fixture forbidden for robot worker')
        self.bundle_path, self.expected_sha256 = Path(installed_bundle), expected_sha256
        self.backend, self.allow_execute = backend, allow_execute
        self.python_executable = str(python_executable)
        self.fixture_path = None if fixture_path is None else Path(fixture_path)
        self.process_factory, self.clock = process_factory, clock
        self.session_settings = copy.deepcopy(session_settings)
        self.installation_sha256 = installation_sha256
        self.process = None
        self._started = self._input_closed = self._spawn_attempted = False
        self._buffers = {'stdout': bytearray(), 'stderr': bytearray()}
        self._eof = {'stdout': False, 'stderr': False}
        self._pending = deque()
        self._stderr_tail = deque(maxlen=32)
        self.closed_report = None
        self.profile = dict(FIXED_PROFILE)
        self.cleanup_receipt = None
        self.cleanup_error = None
        self.input_error = None

    def start(self):
        if self.backend == 'disabled':
            raise ValueError('worker backend disabled until explicitly selected')
        if self._input_closed:
            raise RuntimeError('worker was closed before start')
        if self._started:
            raise RuntimeError('worker cannot restart; create a new session')
        self._started = True
        bundle = read_installed_bundle(self.bundle_path, self.expected_sha256)
        if self.session_settings is not None:
            bundle = effective_session_bundle(bundle, self.installation_sha256, self.session_settings)
        self.profile = bundle_profile(bundle)
        if not Path(self.python_executable).is_absolute():
            raise ValueError('absolute worker interpreter required')
        command = [self.python_executable, '-B', '-u', str(Path(__file__).resolve()),
                   '--bundle', str(self.bundle_path), '--sha256', self.expected_sha256,
                   '--backend', self.backend]
        if self.backend == 'fake':
            if not self.fixture_path.is_absolute() or not self.fixture_path.is_file():
                raise ValueError('absolute installed fake fixture required')
            command.extend(['--fake-fixture', str(self.fixture_path)])
            if self.session_settings is not None:
                command.extend(['--fake-session-settings', json.dumps(self.session_settings, allow_nan=False)])
        else:
            command.append('--execute')
            if self.profile['gripper_mode'] == 'damiao':
                root = self.bundle_path.parent
                launcher = root/'apps/kuavo5_gripper_worker.py'
                for path in (self.bundle_path, root/'manifest.json', launcher,
                             root/'apps/kuavo5_session_service.py',
                             *list((root/'src').rglob('*.py'))):
                    require_root_owned(path)
                if self.bundle_path.name != 'core.json':
                    raise ValueError('privileged worker requires fixed core.json installation')
                command = ['/usr/bin/sudo', '-n', '/usr/bin/python3', '-I', '-B', str(launcher)]
        # Fixed ROS environment only for the explicitly selected robot backend.
        # No shell, SSH, discovery or manufacturer controller startup exists here.
        env = dict(PATH='/usr/bin:/bin', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
        if self.backend == 'robot':
            env.update(PYTHONPATH='/home/lab/kuavo-ros-control/devel/lib/python3/dist-packages:/opt/ros/noetic/lib/python3/dist-packages',
                       ROS_MASTER_URI='http://127.0.0.1:11311', ROS_IP='127.0.0.1')
        if self._input_closed:
            raise RuntimeError('worker was closed during start validation')
        self._spawn_attempted = True
        self.process = self.process_factory(command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env)
        try:
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                if not pipe.closed:
                    os.set_blocking(pipe.fileno(), False)
            if self._input_closed:
                self.request_close()
        except BaseException as exc:
            # The child already exists: retain it for the supervisor's cleanup
            # accounting and request EOF immediately. A partially configured
            # pipe must not later block poll_events.
            self.cleanup_error = 'worker pipe setup failed: ' + str(exc)
            self.request_close()
            raise

    def poll(self):
        return None if self.process is None else self.process.poll()

    @property
    def never_spawned(self):
        """Only meaningful after the supervisor startup thread has completed."""
        return not self._spawn_attempted and self.process is None

    @property
    def stderr_tail(self):
        return tuple(self._stderr_tail)

    @property
    def cleanup_confirmed(self):
        return self.process is not None and self.closed_report is not None \
            and self.poll() == self.closed_report.get('exit_code') and all(self._eof.values()) \
            and self.cleanup_error is None and cleanup_report_ok(
                self.closed_report, self.cleanup_receipt, self.expected_sha256,
                gripper_mode=self.profile['gripper_mode'],
                gripper_config_sha256=self.profile.get('gripper_config_sha256'),
                base_mode=self.profile.get('base_mode', 'disabled'),
                settings_identity=({k: self.session_settings[k] for k in ('revision', 'settings_sha256')}
                                   if self.session_settings is not None else None))

    @property
    def output_drained(self):
        return all(self._eof.values())

    def cleanup_diagnostics(self):
        """Bounded read-only snapshot; it cannot clear ownership or change I/O."""
        code = self.poll()
        failed = cleanup_failure_checks(self.closed_report, self.cleanup_receipt, self.expected_sha256,
            gripper_mode=self.profile['gripper_mode'],
            gripper_config_sha256=self.profile.get('gripper_config_sha256'),
            base_mode=self.profile.get('base_mode', 'disabled'),
            settings_identity=({k: self.session_settings[k] for k in ('revision', 'settings_sha256')}
                               if self.session_settings is not None else None))
        if self.process is None:
            failed.append('worker_process_unavailable')
        elif code is None:
            failed.append('worker_not_exited')
        elif isinstance(self.closed_report, dict) and code != self.closed_report.get('exit_code'):
            failed.append('worker_exit_code_mismatch')
        if not self.output_drained:
            failed.append('worker_output_not_drained')
        if self.cleanup_error is not None:
            failed.append('worker_output_or_pipe_error')
        return dict(worker_exit_code=code if type(code) is int and -255 <= code <= 255 else None,
            closed_report_present=isinstance(self.closed_report, dict),
            cleanup_receipt_present=isinstance(self.cleanup_receipt, dict),
            output_drained=self.output_drained, cleanup_io_error_present=self.cleanup_error is not None,
            failed_checks=failed[:32], stderr_summary=stderr_exception_summary(self.stderr_tail))

    def request_close(self):
        """Stop accepting input and close only this process's stdin; never kill."""
        self._pending.clear()
        self._input_closed = True
        if self.process is not None:
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass

    def send_input(self, packet, *, absolute_deadline=None):
        if self.process is None or self._input_closed or self.poll() is not None:
            raise RuntimeError('worker input unavailable')
        if not isinstance(packet, dict) or set(packet) != {'event', 'challenge_id', 'message'} \
                or packet.get('event') != 'xr':
            raise ValueError('only unchanged XR challenge replies may enter worker')
        deadline = self.clock() + .1 if absolute_deadline is None else absolute_deadline
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= self.clock():
            raise TimeoutError('worker input deadline expired')
        payload = (json.dumps(packet, allow_nan=False, separators=(',', ':')) + '\n').encode()
        if len(payload) > MAX_PACKET_BYTES:
            raise ValueError('worker input exceeds packet limit')
        if self._pending:
            self.input_error = 'worker input backpressure'
            self.request_close()
            raise BufferError(self.input_error)
        self._pending.append([payload, 0, deadline])
        self._flush_input()

    def _flush_input(self):
        while self._pending and not self._input_closed:
            payload, offset, deadline = self._pending[0]
            if self.clock() >= deadline:
                # Partial packet cannot be replaced without corrupting framing.
                # Even whole expired input is not renewed or sent late.
                if offset:
                    self.input_error = 'partially written worker input expired'
                    self.request_close()
                    return
                self._pending.popleft()
                continue
            try:
                count = os.write(self.process.stdin.fileno(), payload[offset:])
            except BlockingIOError:
                return
            except (BrokenPipeError, OSError) as exc:
                self.input_error = 'worker input pipe: ' + str(exc)
                self.request_close()
                return
            if count <= 0:
                return
            self._pending[0][1] += count
            if self._pending[0][1] == len(payload):
                self._pending.popleft()

    def poll_events(self):
        """Drain at most 128 KiB per pipe per call, never wait for pipe I/O."""
        if self.process is None:
            return []
        self._flush_input()
        events = []
        for name in ('stdout', 'stderr'):
            if self._eof[name]:
                continue
            stream, buffer = getattr(self.process, name), self._buffers[name]
            for _ in range(8):
                # Also protect cleanup after partial nonblocking setup failure.
                # This bridge is the sole reader, so readability cannot be
                # consumed by another thread between select and read.
                if not select.select([stream.fileno()], [], [], 0.)[0]:
                    break
                try:
                    chunk = os.read(stream.fileno(), 16384)
                except BlockingIOError:
                    break
                if not chunk:
                    self._eof[name] = True
                    stream.close()
                    if buffer:
                        if name == 'stderr':
                            self._stderr_tail.append(buffer.decode('utf-8', errors='replace'))
                        else:
                            self.cleanup_error = 'truncated worker output'
                        buffer.clear()
                    break
                buffer.extend(chunk)
                while b'\n' in buffer:
                    line, _, rest = buffer.partition(b'\n')
                    buffer[:] = rest
                    if len(line) > MAX_PACKET_BYTES:
                        self.cleanup_error = 'oversize worker output'
                        self.request_close()
                        continue
                    if name == 'stderr':
                        self._stderr_tail.append(line.decode('utf-8', errors='replace'))
                        continue
                    try:
                        packet = json.loads(line.decode('utf-8'), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite worker output')))
                        if not isinstance(packet, dict) or not isinstance(packet.get('event'), str):
                            raise ValueError('packet lacks event')
                    except Exception as exc:
                        self.cleanup_error = 'invalid worker output: ' + str(exc)
                        self.request_close()
                        continue
                    if packet['event'] == 'worker_cleanup':
                        if self.cleanup_receipt is not None or self.closed_report is None:
                            self.cleanup_error = 'misordered or duplicate worker cleanup receipt'
                        self.cleanup_receipt = packet
                        continue
                    if self.cleanup_receipt is not None:
                        self.cleanup_error = 'worker emitted after cleanup receipt'
                    if packet['event'] == 'closed':
                        if self.closed_report is not None:
                            self.cleanup_error = 'duplicate worker close report'
                        self.closed_report = packet
                    elif self.closed_report is not None:
                        self.cleanup_error = 'worker emitted after closed report'
                    events.append(packet)
                if len(buffer) > MAX_PACKET_BYTES:
                    self.cleanup_error = 'unterminated oversized worker output'
                    buffer.clear()
                    self.request_close()
        return events


def _install_bundle(bundle):
    # Same source execution mechanism as the existing audited in-memory bundle,
    # but the source came from a pinned local installation instead of stdin.
    import __future__
    import importlib.abc
    import importlib.util
    sources = bundle['sources']
    packages = {'teleop', 'teleop.contracts', 'teleop.robots', 'teleop.control', 'teleop.xr', 'teleop.retargeting'}
    class MemoryModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in sources or fullname in packages:
                return importlib.util.spec_from_loader(fullname, self, is_package=fullname in packages)
        def create_module(self, spec):
            return None
        def exec_module(self, module):
            exec(compile(sources.get(module.__name__, ''), '<installed-kuavo:' + module.__name__ + '>',
                         'exec', flags=__future__.annotations.compiler_flag, dont_inherit=True), module.__dict__)
    sys.meta_path.insert(0, MemoryModules())


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle')
    parser.add_argument('--sha256')
    parser.add_argument('--backend', choices=('validate', 'fake', 'robot'), default='validate')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--fake-fixture')
    parser.add_argument('--fake-session-settings')
    args = parser.parse_args(argv)
    if args.backend == 'validate' and not args.bundle and not args.execute:
        print(json.dumps(dict(event='KUAVO_SESSION_WORKER_DRY_RUN', process_started=False,
            ros_started=False, physical_robot_command_sent=False)))
        return 0
    if not args.bundle or not args.sha256:
        parser.error('installed bundle and pinned SHA256 required')
    if args.backend == 'robot' and (not args.execute or args.fake_fixture):
        parser.error('robot backend needs --execute and forbids fake fixtures')
    if args.backend != 'robot' and args.execute:
        parser.error('--execute belongs only to operator-started robot backend')
    if args.backend == 'fake' and not args.fake_fixture:
        parser.error('fake backend requires explicit test fixture')
    if args.fake_session_settings and args.backend != 'fake':
        parser.error('injected settings belong only to the explicit offline fake backend')
    bundle = read_installed_bundle(args.bundle, args.sha256)
    if args.backend == 'validate':
        print(json.dumps(dict(event='KUAVO_SESSION_WORKER_VALIDATED', sha256=bundle['sha256'],
                              ros_started=False, physical_robot_command_sent=False)))
        return 0
    execution_lock = None
    if args.backend == 'robot':
        # The network worker and current SSH entry share one execution slot.
        # Import before installing the core's in-memory package loader.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from teleop.robots.kuavo5_execution_lock import ExecutionLock
        lock_options = {}
        if bundle['gripper_mode'] == 'damiao':
            import pwd
            if os.getuid() != 0 or os.geteuid() != 0:
                raise PermissionError('gripper network worker requires protected privileged entry')
            require_root_owned(Path(__file__).absolute())
            require_root_owned(args.bundle)
            lock_options['trusted_operator_uid'] = pwd.getpwnam('lab').pw_uid
        execution_lock = ExecutionLock(route='network', **lock_options)
        try:
            execution_lock.acquire()
            if bundle['gripper_mode'] == 'damiao':
                bundle = read_reserved_settings(bundle, Path(args.bundle).parent/'manifest.json',
                                                lock_options['trusted_operator_uid'])
        except Exception as exc:
            execution_lock.close()
            # No core import, ROS initialization or actuator call has happened.
            # A competing SSH session is not this failed worker's resource.
            packet = dict(event='closed', exit_code=1, transport=dict(
                publish_attempts=0, profile_configured=False,
                worker_start_refused=True, reason=str(exc), physical_stop_verified=False))
            print(json.dumps(packet), flush=True)
            print(json.dumps(dict(event='worker_cleanup', version=1, stage='before_core',
                bundle_sha256=bundle['sha256'], core_returned=False, core_exit_code=1,
                closed_sha256=_packet_digest(packet), initialization_not_started=True,
                ros_started=False, physical_robot_command_sent=False,
                physical_stop_verified=False)), flush=True)
            return 1
    if args.backend == 'fake' and args.fake_session_settings:
        snapshot = json.loads(args.fake_session_settings)
        bundle = effective_session_bundle(bundle, snapshot.get('installation_sha256'), snapshot)
    _install_bundle(bundle)
    if args.backend == 'fake':
        import runpy
        fixture = runpy.run_path(args.fake_fixture)
        return fixture['run_fake'](bundle, run_session=lambda runner: run_core_with_cleanup_receipt(bundle, runner))
    try:
        from teleop.robots.kuavo5_live_agent import run_robot
        return run_core_with_cleanup_receipt(bundle, lambda: run_robot(
            motion_enabled=True, configure_vendor_profile=True,
            **{k: bundle[k] for k in FIXED_PROFILE},
            **({'base_mode': bundle['base_mode']} if 'base_mode' in bundle else {}),
            **({'gripper_config': bundle['gripper_config']} if bundle['gripper_mode'] == 'damiao' else {}),
            **({'arm_motion_parameters': bundle['runtime_arm_motion_parameters'],
                'settings_identity': bundle['runtime_settings_identity']}
               if 'runtime_settings_identity' in bundle else {}),
            **({'return_home_config': bundle['return_home_config']}
               if 'return_home_config' in bundle else {})))
    finally:
        execution_lock.close()


if __name__ == '__main__':
    sys.exit(main())
