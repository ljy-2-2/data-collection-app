"""Fixed preparation operations; construction/import never performs hardware I/O.

The root bootstrap supplies a verified, immutable configuration. Status reads
ROS metadata and process/socket metadata, never a CAN frame. CAN inspection is
a separate explicitly accepted, exclusive operation. No action is auto-retried.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import time
import uuid
import xmlrpc.client

from .kuavo5_execution_lock import ExecutionLock

INITIAL_SERVICE = '/humanoid_controller/real_initial_start'
CALLER = '/galaxis_preparation_observer'
PARAMS = ('/robot_version', '/real', '/use_sim_time', '/use_external_mpc',
          '/hardware/is_ready', '/hardware/ready_to_start', '/cali', '/cali_arm', '/cali_leg')
REQUIRED_NODES = {'/nodelet_manager', '/nodelet_controller', '/nodelet_hardware'}
ACTIONS = ('status', 'start', 'initialize', 'grippers_inspect', 'grippers_prepare')


class PreparationOutcomeUnknown(RuntimeError):
    outcome_unknown = True


class PreparationRefused(ValueError):
    outcome_unknown = False


def digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, allow_nan=False).encode()).hexdigest()


def operator_lock():
    return ExecutionLock(route='ssh', trusted_operator_uid=pwd.getpwnam('lab').pw_uid)


def process_identity(pid, proc=Path('/proc')):
    if type(pid) is not int or pid < 1:
        raise ValueError('invalid controller PID')
    fields = (proc/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] in ('Z', 'X'):
        raise ValueError('controller process is not live')
    return dict(boot_id=(proc/'sys/kernel/random/boot_id').read_text().strip(),
                pid=pid, start_ticks=int(fields[19]))


class _TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = 2.
        return connection


def xmlrpc_proxy(uri):
    return xmlrpc.client.ServerProxy(uri, transport=_TimeoutTransport())


def vendor_trigger():
    """Only called after an explicit confirmed action and durable receipt."""
    os.environ.update(ROS_MASTER_URI='http://127.0.0.1:11311', ROS_IP='127.0.0.1')
    os.environ.pop('ROS_HOSTNAME', None)
    sys.path[:0] = ['/home/lab/kuavo-ros-control/devel/lib/python3/dist-packages',
                    '/opt/ros/noetic/lib/python3/dist-packages']
    import rospy
    from std_srvs.srv import Trigger, TriggerRequest
    proxy = rospy.ServiceProxy(INITIAL_SERVICE, Trigger, persistent=False)
    proxy.wait_for_service(timeout=2.)
    response = proxy.call(TriggerRequest())
    if response.success is not True:
        raise RuntimeError('manufacturer rejected initialization: ' + response.message)
    return dict(accepted=True, initialization_completed=False,
                detail='manufacturer accepted o; query readiness, do not repeat')


class VendorObserver:
    def __init__(self, proxy_factory=xmlrpc_proxy, proc=Path('/proc'), trigger=vendor_trigger):
        self.proxy_factory, self.proc, self.trigger = proxy_factory, Path(proc), trigger

    def _node_identity(self, master, name):
        code, _, uri = master.lookupNode(CALLER, name)
        if code != 1 or not isinstance(uri, str):
            raise ValueError('live ROS node unavailable: ' + name)
        code, _, pid = self.proxy_factory(uri).getPid(CALLER)
        if code != 1:
            raise ValueError('ROS node PID unavailable: ' + name)
        return process_identity(pid, self.proc)

    def snapshot(self):
        result = dict(phase='unavailable', parameters={}, nodes=[], controller_identity=None,
                      initialize_service=False, provider_matches_controller=False,
                      physical_robot_state_verified=False)
        try:
            master = self.proxy_factory('http://127.0.0.1:11311')
            code, _, graph = master.getSystemState(CALLER)
            if code != 1:
                raise ValueError('ROS graph unavailable')
            nodes = {node for section in graph for _, names in section for node in names}
            result['nodes'] = sorted(nodes & (REQUIRED_NODES | {'/arms_ik_node'}))
            for name in PARAMS:
                code, _, value = master.getParam(CALLER, name)
                result['parameters'][name] = value if code == 1 else None
            values = result['parameters']
            if not nodes & REQUIRED_NODES:
                result['phase'] = 'not_started'
                return result
            if (str(values['/robot_version']) != '63' or values['/real'] is not True
                    or values['/use_sim_time'] not in (None, False)
                    or values['/use_external_mpc'] is not True):
                result['phase'] = 'incompatible'
                return result
            if any(values[name] is not False for name in ('/cali', '/cali_arm', '/cali_leg')):
                result['phase'] = 'calibration_or_unknown'
                return result
            if not REQUIRED_NODES <= nodes:
                result['phase'] = 'preparing'
                return result
            identity = self._node_identity(master, '/nodelet_manager')
            command = (self.proc/str(identity['pid'])/'cmdline').read_bytes().replace(b'\0', b' ')
            if b'nodelet' not in command or b'manager' not in command:
                raise ValueError('controller node is not the expected nodelet manager process')
            # nodelet_controller/hardware may be separate loader processes;
            # validate they are live, without confusing loaders with the host.
            for name in REQUIRED_NODES - {'/nodelet_manager'}:
                self._node_identity(master, name)
            result['controller_identity'] = identity
            providers = [names for name, names in graph[2] if name == INITIAL_SERVICE]
            result['initialize_service'] = bool(providers)
            if providers:
                owners = [name for names in providers for name in names]
                result['provider_matches_controller'] = bool(owners) and all(
                    self._node_identity(master, name) == identity for name in owners)
            ready = type(values['/hardware/is_ready']) is int and values['/hardware/is_ready'] == 1
            if ready and '/arms_ik_node' in nodes:
                self._node_identity(master, '/arms_ik_node')
                result['phase'] = 'ready'
            elif (type(values['/hardware/is_ready']) is int and values['/hardware/is_ready'] == 0
                    and type(values['/hardware/ready_to_start']) is int
                    and values['/hardware/ready_to_start'] == 1
                    and result['initialize_service'] and result['provider_matches_controller']):
                result['phase'] = 'awaiting_confirmation'
            else:
                result['phase'] = 'preparing'
        except Exception as exc:
            result.update(phase='unavailable', error=type(exc).__name__ + ': ' + str(exc))
        return result

    def initialize(self, expected_identity):
        latest = self.snapshot()
        if (latest['phase'] != 'awaiting_confirmation'
                or latest['controller_identity'] != expected_identity
                or latest['provider_matches_controller'] is not True):
            raise PreparationRefused('initialization controller/provider/phase changed before dispatch')
        return self.trigger()


class InstalledManager:
    """Call only the pinned independent manager; never stop or adopt a process."""
    def __init__(self, runtime_path, runtime_sha256, run=subprocess.run):
        self.path, self.sha, self.run = Path(runtime_path), runtime_sha256, run

    def _call(self, arguments):
        info = self.path.lstat()
        if (not re.fullmatch(r'/opt/galaxis/kuavo-controller/releases/[A-Za-z0-9_-]+/controller_runtime\.py', str(self.path))
                or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or hashlib.sha256(self.path.read_bytes()).hexdigest() != self.sha):
            raise ValueError('independent manager path or hash differs')
        result = self.run(['/usr/bin/python3', '-I', '-B', str(self.path)] + arguments,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=15 if arguments == ['--status'] else 65,
                          env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        if result.returncode not in (0, 2):
            raise RuntimeError('independent manager failed: ' + result.stderr[-1000:])
        document = json.loads(result.stdout or result.stderr)
        if not isinstance(document, dict):
            raise ValueError('invalid independent manager response')
        return document

    def status(self):
        result = self._call(['--status'])
        # The independent runtime already checks these root-protected files.
        # Only use them after it has established its immutable context.
        if result.get('phase') in ('process_running', 'unmanaged_controller'):
            config = json.loads(self.path.with_name('controller-config.json').read_text())
            launcher = self.path.with_name('operator_deployment.py')
            if hashlib.sha256(launcher.read_bytes()).hexdigest() != config['operator_deployment_sha256']:
                raise ValueError('pinned r8 observer changed')
            checked = self.run(['/usr/bin/python3', '-I', '-B', str(launcher),
                                '--deployment', config['deployment'], '--check'],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
                               env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
            if checked.returncode not in (0, 2):
                raise ValueError('r8 metadata inspection failed')
            result['r8'] = json.loads(checked.stdout)
        return result

    def request_start(self, confirm_motion, motor_ids_confirmed):
        if confirm_motion is not True or motor_ids_confirmed is not True:
            raise ValueError('explicit startup and motor identity confirmation required')
        return self._call(['--request-start', '--confirm-motion', '--motor-ids-confirmed'])


def socket_metadata(document, identity, proc=Path('/proc')):
    """Read socket ownership metadata only. No connect, recv or send."""
    directory = Path(document['owner_directory'])
    paths = {side: str(directory/('canbus%d.sock' % i)) for i, side in enumerate(('left', 'right'))}
    kernel = {}
    for line in (proc/'net/unix').read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[7] in paths.values():
            if fields[7] in kernel:
                raise ValueError('ambiguous CAN socket ownership')
            kernel[fields[7]] = fields[6]
    descriptors = set()
    if identity:
        if process_identity(identity['pid'], proc) != identity:
            raise ValueError('controller changed during socket observation')
        for path in (proc/str(identity['pid'])/'fd').iterdir():
            try:
                descriptors.add(os.readlink(str(path)))
            except FileNotFoundError:
                continue
    result = {}
    for side, name in paths.items():
        try:
            info = Path(name).lstat()
            inode = kernel.get(name)
            valid = stat.S_ISSOCK(info.st_mode) and inode is not None and 'socket:[%s]' % inode in descriptors
            result[side] = dict(path=name, exists=True, owned_by_controller=valid,
                                kernel_inode=inode, file_inode=info.st_ino, owner_uid=info.st_uid)
        except FileNotFoundError:
            result[side] = dict(path=name, exists=False, owned_by_controller=False)
    return result


def verify_vendor_pins(pins):
    if not isinstance(pins, dict) or len(pins) < 2:
        raise ValueError('audited manufacturer initialization source pins required')
    for name, expected in pins.items():
        path = Path(name)
        if (not name.startswith('/home/lab/kuavo-ros-control/') or '..' in path.parts
                or not re.fullmatch('[a-f0-9]{64}', expected)
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ValueError('manufacturer source changed: ' + name)


class RobotPreparation:
    def __init__(self, manager, observer, gripper_runner, config, state_dir,
                 lock_factory=operator_lock, clock=time.time, owner_observer=socket_metadata,
                 vendor_check=verify_vendor_pins, state_owner_uid=0, history_ttl_s=120.):
        self.manager, self.observer, self.gripper_runner = manager, observer, gripper_runner
        self.config, self.state_dir, self.lock_factory = config, Path(state_dir), lock_factory
        self.clock, self.owner_observer, self.vendor_check = clock, owner_observer, vendor_check
        self.state_owner_uid, self.history_ttl_s = state_owner_uid, history_ttl_s

    def _config_check(self):
        if digest(self.config['gripper_document']) != self.config['gripper_config_sha256']:
            raise ValueError('formal gripper configuration hash differs')
        if not re.fullmatch('[a-f0-9]{64}', self.config['controller_config_sha256']):
            raise ValueError('controller configuration hash missing')

    def _directory(self):
        self.state_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = self.state_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.state_owner_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('protected preparation state directory required')

    def _write(self, name, value):
        self._directory()
        fd = os.open(str(self.state_dir/name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(str(self.state_dir), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _history(self, context):
        newest = None
        if self.state_dir.exists():
            info = self.state_dir.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.state_owner_uid or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('protected preparation state directory required')
            for path in self.state_dir.glob('grippers-*.json'):
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != self.state_owner_uid or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError('untrusted gripper preparation history')
                record = json.loads(path.read_text())
                if newest is None or record['observed_at'] > newest['observed_at']:
                    newest = record
        if newest is None:
            return dict(state='not_observed', live_motor_state_verified=False)
        age = self.clock() - newest['observed_at']
        matching = newest['context'] == context
        return dict(state='historical', observed_at=newest['observed_at'], age_s=age,
                    context_matches=matching, recent=matching and 0 <= age <= self.history_ttl_s,
                    result=newest['result'], live_motor_state_verified=False)

    def _read_record(self, path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.state_owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError('untrusted preparation record')
        return json.loads(path.read_text())

    def _unresolved_apply(self, context):
        """A lost write result survives service restart and config changes."""
        if not self.state_dir.exists():
            return None
        for path in self.state_dir.glob('apply-*.json'):
            if path.name.startswith('apply-result-'):
                continue
            record = self._read_record(path)
            if record['context']['controller'] != context['controller']:
                continue
            result_path = path.with_name('apply-result-' + record['request_id'] + '.json')
            try:
                result = self._read_record(result_path)
            except FileNotFoundError:
                return record['request_id']
            if result['outcome_unknown'] is True:
                return record['request_id']
        return None

    @staticmethod
    def _write_outcome_unknown(answer):
        if answer.get('cleanup_errors'):
            return True
        if type(answer.get('outcome_unknown')) is bool:
            return answer['outcome_unknown']
        for side in ('left', 'right'):
            result = answer.get('sides', {}).get(side, {})
            if result.get('cleanup_error'):
                return True
            if set(result.get('writes_attempted', [])) - set(result.get('writes_confirmed', [])):
                return True
        return False

    def _snapshot(self, history=True):
        self._config_check()
        manager = self.manager.status()
        controller = self.observer.snapshot()
        phase = controller['phase']
        if manager['phase'] == 'not_started' and phase in ('not_started', 'unavailable'):
            phase = 'not_started'
        elif phase == 'not_started' and manager['phase'] in ('process_running', 'unmanaged_controller'):
            # The launcher may be alive before any controller ROS nodes appear.
            # Only the managed launcher establishes startup progress; an external
            # process without matching ROS state must not invite start/adoption.
            phase = 'starting' if manager['phase'] == 'process_running' else 'unavailable'
        elif manager['phase'] not in ('process_running', 'unmanaged_controller'):
            phase = 'starting' if manager['phase'] == 'starting' else 'unavailable'
        identity = controller.get('controller_identity')
        owners = copy.deepcopy(self.owner_observer(self.config['gripper_document'], identity))
        context = dict(controller=identity, owner=owners,
                       gripper_config_sha256=self.config['gripper_config_sha256'],
                       controller_config_sha256=self.config['controller_config_sha256'])
        result = dict(phase=phase, manager=manager, controller=controller, sockets=owners,
                      context=context, observed_at=self.clock(), can_frames_sent=False,
                      physical_robot_state_verified=False,
                      readiness_scope='controller_report_and_historical_gripper_evidence')
        if history:
            result['grippers'] = self._history(context)
            result['unresolved_gripper_preparation'] = self._unresolved_apply(context)
        return result

    def perform(self, action, *, request_id=None, confirm_motion=False, motor_ids_confirmed=False,
                accept_unverified_gripper_bus=False):
        if action not in ACTIONS or any(type(value) is not bool for value in (
                confirm_motion, motor_ids_confirmed, accept_unverified_gripper_bus)):
            raise ValueError('valid fixed preparation action and boolean permissions required')
        if action == 'status':
            return self._snapshot()
        if not isinstance(request_id, str) or re.fullmatch('[a-f0-9]{32}', request_id) is None:
            raise ValueError('explicit unique preparation request ID required')
        if action != 'grippers_inspect' and not confirm_motion:
            raise ValueError('explicit confirmation required before preparation I/O')
        if action == 'start' and not motor_ids_confirmed:
            raise ValueError('motor identities must be explicitly confirmed')
        if action.startswith('grippers_') and not accept_unverified_gripper_bus:
            raise ValueError('explicit unverified gripper bus acceptance required')
        with self.lock_factory():
            self._config_check()
            self.vendor_check(self.config['vendor_pins'])
            before = self._snapshot(history=False)
            self._write('request-' + request_id + '.json', dict(action=action,
                request_id=request_id, observed_at=self.clock(), context=before['context']))
            if action == 'start':
                if before['phase'] != 'not_started' or before['manager']['phase'] != 'not_started':
                    raise ValueError('controller is already present or startup state unknown; no adoption')
                try:
                    answer = self.manager.request_start(True, True)
                except Exception as exc:
                    raise PreparationOutcomeUnknown('start outcome requires observation; no retry: ' + str(exc)) from exc
                accepted = answer.get('phase') in ('process_running', 'starting')
                return dict(action=action, accepted=accepted, manager=answer,
                            outcome_unknown=not accepted, automatic_retry=False)
            if action == 'initialize':
                controller = before['controller']
                if (before['phase'] != 'awaiting_confirmation' or not controller.get('controller_identity')
                        or controller.get('provider_matches_controller') is not True):
                    raise ValueError('initialization requires a live matching provider awaiting confirmation')
                identity = controller['controller_identity']
                self._require_r8(before, identity)
                receipt = dict(action=action, controller=identity,
                               controller_config_sha256=self.config['controller_config_sha256'],
                               vendor_pins=self.config['vendor_pins'], outcome='dispatch_reserved')
                # Key by controller instance, NOT configuration: changing a config
                # must never make a second initialization legal for that process.
                self._write('initialize-' + digest(identity) + '.json', receipt)
                latest = self._snapshot(history=False)
                if (latest['phase'] != 'awaiting_confirmation'
                        or latest['controller'].get('controller_identity') != identity
                        or latest['controller'].get('provider_matches_controller') is not True):
                    raise ValueError('controller changed before initialization; receipt retained, no retry')
                try:
                    answer = self.observer.initialize(identity)
                except PreparationRefused:
                    raise
                except Exception as exc:
                    raise PreparationOutcomeUnknown('initialization outcome unknown; receipt retained: ' + str(exc)) from exc
                return dict(action=action, accepted=answer.get('accepted') is True, result=answer,
                            automatic_retry=False, physical_robot_state_verified=False)
            if before['phase'] != 'ready' or not before['context']['controller']:
                raise ValueError('gripper preparation requires a live initialized controller')
            self._require_r8(before, before['context']['controller'])
            if not all(before['sockets'].get(side, {}).get('owned_by_controller') is True for side in ('left', 'right')):
                raise ValueError('left/right CAN sockets must belong to the current controller')
            apply = action == 'grippers_prepare'
            if apply:
                unresolved = self._unresolved_apply(before['context'])
                if unresolved:
                    raise ValueError('previous gripper write/cleanup outcome unresolved: ' + unresolved + '; inspect only, no retry')
                self._write('apply-' + request_id + '.json', dict(request_id=request_id,
                            observed_at=self.clock(), context=before['context'], outcome='dispatch_reserved'))
            try:
                answer = self.gripper_runner(self.config['gripper_document'],
                    apply=apply, accept_unverified_gripper_bus=True)
            except Exception as exc:
                raise PreparationOutcomeUnknown('gripper preparation did not return a complete report: ' + str(exc)) from exc
            try:
                return self._finish_grippers(answer, before, request_id, action, apply)
            except PreparationOutcomeUnknown:
                raise
            except Exception as exc:
                raise PreparationOutcomeUnknown('gripper result verification/recording incomplete: ' + str(exc)) from exc

    def _finish_grippers(self, answer, before, request_id, action, apply):
        if not isinstance(answer, dict):
            raise ValueError('invalid gripper preparation report')
        # Cross-check the actual IPC peer observed by each gripper client.
        # Missing peers are allowed only for explicitly failed preflights.
        expected_pid = before['context']['controller']['pid']
        for side in ('left', 'right'):
            detail = answer.get('sides', {}).get(side, {})
            peer = detail.get('owner_pid')
            if (peer is not None and peer != expected_pid) or (detail.get('ready') is True and peer != expected_pid):
                raise PreparationOutcomeUnknown(side + ': gripper IPC owner differs from the observed controller')
        both_ready = all(answer.get('sides', {}).get(side, {}).get('ready') is True
                         for side in ('left', 'right'))
        answer = dict(answer, ready=answer.get('ready') is True and both_ready)
        after = self._snapshot(history=False)
        if after['phase'] != 'ready' or after['context'] != before['context']:
            raise PreparationOutcomeUnknown('controller or socket ownership changed during gripper preparation')
        unknown = self._write_outcome_unknown(answer)
        if apply:
            self._write('apply-result-' + request_id + '.json', dict(request_id=request_id,
                        outcome_unknown=unknown, result=answer))
        record = dict(observed_at=self.clock(), action=action, context=before['context'], result=answer)
        self._write('grippers-' + uuid.uuid4().hex + '.json', record)
        return dict(action=action, result=answer, observed_at=record['observed_at'],
                    outcome_unknown=unknown, automatic_retry=False, physical_robot_state_verified=False)

    @staticmethod
    def _require_r8(snapshot, identity):
        report = snapshot['manager'].get('r8', {})
        if (report.get('result') != 'deployment_verified' or report.get('problems') != []
                or report.get('control_allowed') is not True
                or report.get('owner_pids') != [identity['pid']]):
            raise ValueError('live controller does not match the pinned r8 SDK/socket owner')
