"""PC platform adapter. Construction/status/stop do not perform robot IO.

start_session explicitly launches the existing network-only arm entry in a
non-daemon thread. stop_session requests cooperative exit; it never kills a
process, sends a control topic or claims that physical motion has stopped.
"""
import copy
import importlib.util
import math
from pathlib import Path
import threading
import uuid


class SessionControl:
    def __init__(self):
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._status = dict(session_id=uuid.uuid4().hex, phase='starting', exit_code=None,
                            error=None, report=None, physical_stop_verified=False)

    def request_stop(self):
        with self._lock:
            self.stop_event.set()
            if self._status['phase'] not in ('finished', 'failed'):
                self._status['phase'] = 'stopping'

    def ready(self):
        with self._lock:
            if not self.stop_event.is_set():
                self._status['phase'] = 'running'

    def set_report(self, report):
        with self._lock:
            self._status['report'] = copy.deepcopy(report)

    def finish(self, code, error=None):
        with self._lock:
            self._status.update(phase='finished' if code in (0, 130) else 'failed',
                                exit_code=code, error=error)

    def status(self):
        with self._lock:
            return copy.deepcopy(self._status)


class KuavoArmSession:
    """Use one instance per platform process; no main-controller initialization.

Caller prepares PC network/PICO and initializes the manufacturer controller.
The caller must not run another SDK input client in this process. All robot
admission/ownership checks remain in the existing server and execution core.

use_service_configuration selects the compatible formal-service client. In
that mode the robot owns all gripper/return settings; the PC stores connection
credentials and capability requirements only. Legacy pinned callers keep their
existing local configuration path until explicitly migrated.
"""
    def __init__(self, config_path, *, runner=None, gripper_config_path=None,
                 accept_unverified_gripper_bus=False, return_home_config_path=None,
                 use_service_configuration=False):
        self.config_path = Path(config_path).resolve()
        if type(use_service_configuration) is not bool:
            raise ValueError('explicit service configuration boolean required')
        if use_service_configuration and (gripper_config_path is not None or
                                          return_home_config_path is not None):
            raise ValueError('service configuration mode rejects local gripper and return configurations')
        self.use_service_configuration = use_service_configuration
        if type(accept_unverified_gripper_bus) is not bool:
            raise ValueError('explicit shared-bus acceptance boolean required')
        if accept_unverified_gripper_bus and gripper_config_path is None and not use_service_configuration:
            raise ValueError('shared-bus acceptance requires gripper configuration')
        self.gripper_config_path = None if gripper_config_path is None else Path(gripper_config_path).resolve()
        self.return_home_config_path = (None if return_home_config_path is None
                                        else Path(return_home_config_path).resolve())
        self.accept_unverified_gripper_bus = accept_unverified_gripper_bus
        self._runner = runner or self._run
        self._lock = threading.Lock()
        self._thread = self._control = None

    def _run(self, control, duration):
        root = Path(__file__).resolve().parents[3]
        app = ('kuavo5_current_client.py' if self.use_service_configuration
               else 'kuavo5_network_client.py')
        spec = importlib.util.spec_from_file_location('kuavo_platform_network_client',
                                                    root/'apps'/app)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        options = []
        if self.use_service_configuration and self.accept_unverified_gripper_bus:
            options.append('--accept-unverified-gripper-bus')
        if self.gripper_config_path is not None:
            options = ['--gripper-config', str(self.gripper_config_path), '--allow-gripper-control']
            if self.accept_unverified_gripper_bus:
                options.append('--accept-unverified-gripper-bus')
        if self.return_home_config_path is not None:
            options.extend(['--return-home-config', str(self.return_home_config_path)])
        return module.main(['--execute', '--config', str(self.config_path),
                            '--duration', str(duration), *options], session_control=control)

    def start_session(self, *, confirm_motion=False, duration=0.):
        if confirm_motion is not True:
            raise ValueError('explicit operator start confirmation required')
        if type(duration) not in (float, int) or not math.isfinite(duration) or duration < 0:
            raise ValueError('finite nonnegative duration required; 0 means until stopped')
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError('previous session still running or cleaning up')
            control = SessionControl()
            self._control = control

            def run():
                try:
                    if control.stop_event.is_set():
                        control.finish(0)
                        return
                    code = self._runner(control, duration)
                    control.finish(code)
                except BaseException as exc:
                    control.finish(1, type(exc).__name__+': '+str(exc))

            self._thread = threading.Thread(target=run, name='kuavo-platform-session', daemon=False)
            try:
                self._thread.start()
            except BaseException as exc:
                control.finish(1, str(exc))
                raise
            return control.status()

    def stop_session(self, session_id):
        """Nonblocking. A stale button request cannot stop a later session."""
        with self._lock:
            if self._control is None or self._control.status()['session_id'] != session_id:
                raise ValueError('session identity mismatch')
            self._control.request_stop()
            return self._control.status()

    def get_status(self):
        with self._lock:
            if self._control is None:
                return dict(phase='idle', session_id=None, task_alive=False, physical_stop_verified=False)
            result = self._control.status()
            result['task_alive'] = self._thread is not None and self._thread.is_alive()
            return result
