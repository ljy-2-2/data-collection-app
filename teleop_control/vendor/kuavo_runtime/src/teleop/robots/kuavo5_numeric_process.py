"""Private official-math process. No ROS/control APIs; explicit construction only.

Robot construction belongs to the user-started execution entry, not a read-only
probe. Local official-library validation must use the network/device sandbox.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import threading
import time

from teleop.contracts.intent import CartesianPose
from teleop.robots.kuavo5_native_ik import OFFICIAL_UPPER_BODY_IK_PARAMS
from teleop.robots.kuavo5_numeric_source import SOURCE

VENDOR = Path('/home/lab/kuavo-ros-control/src/manipulation_nodes/motion_capture_ik')
CONFIG = Path('/home/lab/kuavo-ros-control/src/kuavo_assets/config/kuavo_v63/kuavo.json')
PINNED = {
    VENDOR/'lib/libplantIK.so': '2ac38e6c2a59b79bbee14c599b0d4a94fa8383f6f632eca99d17252e688381a4',
    Path('/opt/drake/lib/libdrake.so'): '79b7d2dd68b6de48c20400770963f2e117d43cae0a91a7edca5197b6849e2091',
    VENDOR/'include/plantIK.h': '7b2c7f016246078fe6e3b7493930bc29b69b0147a656b531db121d297ed84147',
    CONFIG: '9d9a641197b442702feecb96387ee06cdb73a8e1e9110eddc6aba2f2924154ad',
}


def finite_vector(values, length):
    if not isinstance(values, (list, tuple)) or len(values) != length or any(
            type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError('finite numeric vector of length '+str(length)+' required')


def validate_request(request):
    if not isinstance(request, dict) or type(request.get('frame')) is not int \
            or request['frame'] != 0 or request.get('joint_angles_as_q0') is not True:
        raise ValueError('fixed base and explicit 14-joint seed required')
    if request.get('use_custom_ik_param') is not True:
        raise ValueError('explicit audited official parameters required')
    params = request.get('ik_param')
    if not isinstance(params, dict) or type(params.get('constraint_mode')) is not int \
            or params['constraint_mode'] not in (0, 3) \
            or params != dict(OFFICIAL_UPPER_BODY_IK_PARAMS, constraint_mode=params['constraint_mode']) \
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in params.values()):
        raise ValueError('parameters differ from audited official profile')
    hands = request.get('hand_poses')
    if not isinstance(hands, dict) or set(hands) != {'left_pose','right_pose'}:
        raise ValueError('both hand targets required')
    for hand in hands.values():
        if not isinstance(hand, dict):
            raise ValueError('hand pose must be a mapping')
        for name, length in (('pos_xyz',3),('quat_xyzw',4),('elbow_pos_xyz',3),('joint_angles',7)):
            finite_vector(hand.get(name), length)
        if abs(sum(v*v for v in hand['quat_xyzw'])-1.) > 1e-5:
            raise ValueError('unit quaternion required')


class NumericProcess:
    """Single serialized request; timeout/corruption permanently poisons worker."""
    def __init__(self, process, *, check, timeout_s=.15, ready_timeout_s=10., arm_posture='baseline'):
        from teleop.robots.kuavo5_posture import posture_profile
        posture_profile(arm_posture, 'bounded_candidate')
        self.arm_posture = arm_posture
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
               for v in (timeout_s, ready_timeout_s)):
            raise ValueError('positive finite numerical RPC timeout required')
        self.process, self.check, self.timeout_s = process, check, timeout_s
        self.failed = False
        self.closed = False
        self.lock = threading.Lock()
        self.buffer = b''
        self.sequence = 0
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)
        os.set_blocking(process.stdin.fileno(), False)
        try:
            ready = self._read(time.monotonic()+ready_timeout_s)
            if ready != dict(event='ready', dof=14, frame='base_link'):
                raise ValueError('numeric worker identity mismatch')
        except BaseException:
            self.close()
            raise

    def _read(self, deadline):
        while True:
            if time.monotonic() >= deadline:
                raise ValueError('numeric worker timed out')
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n',1)
                if len(line) > 65536:
                    raise ValueError('numeric response too large')
                if line.startswith(b'RPC_JSON '):
                    if time.monotonic() >= deadline:
                        raise ValueError('numeric worker timed out')
                    return json.loads(line[9:])
            if len(self.buffer) > 65536:
                raise ValueError('numeric response too large')
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise ValueError('numeric worker timed out')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ValueError('numeric worker disconnected')
            self.buffer += chunk

    def _request(self, payload):
        with self.lock:
            if self.failed or self.closed:
                raise ValueError('numeric worker unusable after failure/close')
            # Observation/permission failures are handled by the existing
            # session gate. They are not corruption of this private RPC.
            self.check()
            try:
                self.sequence += 1
                data = (json.dumps(dict(id=self.sequence,payload=payload),allow_nan=False)+'\n').encode()
                if len(data) > 65536:
                    raise ValueError('numeric request too large')
                deadline = time.monotonic()+self.timeout_s
                while data:
                    if time.monotonic() >= deadline:
                        raise ValueError('numeric worker write timed out')
                    try:
                        count = os.write(self.process.stdin.fileno(), data)
                    except BlockingIOError:
                        time.sleep(.001)
                        continue
                    if count <= 0:
                        raise ValueError('numeric worker input closed')
                    data = data[count:]
                packet = self._read(deadline)
                if not isinstance(packet, dict) or type(packet.get('id')) is not int \
                        or packet['id'] != self.sequence or 'error' in packet \
                        or not isinstance(packet.get('result'), dict):
                    raise ValueError('numeric worker invalid response: '+str(packet)[:300])
            except Exception as exc:
                self.failed = True
                if isinstance(exc, ValueError):
                    raise
                raise ValueError('numeric worker failed: '+str(exc)) from exc
            self.check()  # Discard this answer if observations changed in flight.
            return packet['result']

    def forward(self, q):
        finite_vector(q,14)
        result = self._request(dict(op='fk',q=list(q)))
        try:
            if set(result) != {'left','right'}:
                raise ValueError('both FK hands required')
            for pose in result.values():
                if not isinstance(pose, dict) or pose.get('frame_id') != 'base_link':
                    raise ValueError('fixed-base FK required')
                finite_vector(pose.get('position_m'),3)
                finite_vector(pose.get('orientation_xyzw'),4)
                # CartesianPose normalizes quaternions, but must not turn a
                # malformed worker answer into apparently valid FK evidence.
                if abs(sum(v*v for v in pose['orientation_xyzw'])-1.) > 1e-5:
                    raise ValueError('numeric FK requires unit quaternion')
            return {side: CartesianPose(p['frame_id'],p['position_m'],p['orientation_xyzw'])
                    for side,p in result.items()}
        except (KeyError,TypeError,ValueError):
            self.failed = True
            raise ValueError('numeric worker invalid FK')

    def geometry(self, q):
        if self.arm_posture not in ('elbow_preference', 'elbow_preference_v2'):
            raise ValueError('geometry requires explicit elbow preference selection')
        from teleop.robots.kuavo5_posture import validate_geometry
        finite_vector(q, 14)
        result = self._request(dict(op='geometry', q=list(q)))
        try:
            return validate_geometry(result)
        except (TypeError, KeyError, ValueError):
            self.failed = True
            raise ValueError('numeric worker invalid geometry')

    def inverse(self, request):
        if self.arm_posture in ('elbow_preference', 'elbow_preference_v2'):
            from copy import deepcopy
            if not isinstance(request, dict) or not isinstance(request.get('ik_param'), dict) \
                    or type(request['ik_param'].get('elbow_cost_scale')) not in (int, float) \
                    or request['ik_param']['elbow_cost_scale'] != .1:
                raise ValueError('live elbow preference weight must be 0.1')
            ordinary = deepcopy(request)
            ordinary['ik_param']['elbow_cost_scale'] = 0.
            validate_request(ordinary)
        else:
            validate_request(request)
        return self._request(dict(op='ik',request=request))

    def close(self):
        if self.closed:
            return
        self.closed = True
        # Only the private mathematics child, never ROS or the robot main control.
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1.)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1.)
        self.selector.close()
        self.process.stdin.close()
        self.process.stdout.close()


def compiler_command(source, output, *, include, drake_include, library, drake_library,
                     json_include=None):
    # Match this robot's Drake CMake export: fmt::fmt-header-only 6.1.2.
    # Using the PC's system fmt silently builds a different ABI dependency.
    return ['/usr/bin/g++','-std=c++17','-O2','-pthread','-DFMT_HEADER_ONLY=1',
            str(source),'-o',str(output),'-I'+str(drake_include/'fmt'),
            '-I'+str(include),'-I'+str(drake_include),'-I'+str(drake_include/'eigen3'),
            '-I/usr/include/eigen3','-I'+str(json_include or include),
            '-L'+str(library),'-L'+str(drake_library),
            '-Wl,-rpath,'+str(library),'-Wl,-rpath,'+str(drake_library),
            '-lplantIK','-ldrake']


def run_compiler(command, *, env, cwd, check, timeout_s=60.):
    """Cancel only the new compiler process group if input/observations expire."""
    check()
    process = subprocess.Popen(command,env=env,cwd=cwd,stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,start_new_session=True)
    deadline = time.monotonic()+timeout_s
    try:
        while True:
            check()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise ValueError('private numeric compiler timed out before control')
            try:
                stdout, stderr = process.communicate(timeout=min(.05,remaining))
                return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.poll() is None:
            # start_new_session gave this compiler its own group. Never target
            # the caller, SSH master, ROS nodes or manufacturer controllers.
            try:
                os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=1.)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=1.)
        process.stdout.close()
        process.stderr.close()


class RobotNumericProcess(NumericProcess):
    """Build with existing robot vendor environment BEFORE any control service.

    Files are confined to a fresh owned /tmp directory. Original model, config,
    binaries and ROS nodes are not modified. No install or shell startup script.
    """
    def __init__(self, model, control_bytes, *, check, arm_posture='baseline'):
        from teleop.robots.kuavo5_posture_source import numeric_source_for
        from teleop.robots.kuavo5_posture import posture_profile
        profile = posture_profile(arm_posture, 'bounded_candidate')
        source = numeric_source_for(arm_posture)
        self.work = None
        self.stderr_file = None
        try:
            check()
            for path, digest in PINNED.items():
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError('official numeric dependency hash mismatch: '+str(path))
            self.work = tempfile.TemporaryDirectory(prefix='teleop-kuavo-numeric-')
            root = Path(self.work.name)
            (root/'worker.cpp').write_text(source)
            (root/'ik.urdf').write_text(model.xml)
            (root/'control.urdf').write_bytes(control_bytes)
            (root/'config.json').write_bytes(CONFIG.read_bytes())
            command = compiler_command(root/'worker.cpp',root/'worker',
                include=VENDOR/'include', drake_include=Path('/opt/drake/include'),
                library=VENDOR/'lib', drake_library=Path('/opt/drake/lib'))
            env = dict(PATH='/usr/bin:/bin',LANG='C',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',
                       LD_LIBRARY_PATH=str(VENDOR/'lib')+':/opt/drake/lib',
                       DRAKE_RESOURCE_ROOT='/opt/drake/share')
            compiled = run_compiler(command,env=env,cwd=str(root),check=check)
            if compiled.returncode:
                raise ValueError('official numeric build failed before control: '+compiled.stderr.decode(errors='replace')[-1800:])
            check()
            self.stderr_file = (root/'worker.stderr').open('wb')
            process = subprocess.Popen([str(root/'worker'),str(root/'ik.urdf'),str(root/'config.json'),str(root/'control.urdf')],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stderr_file,
                bufsize=0,env=env,cwd=str(root))
            super().__init__(process,check=check,arm_posture=arm_posture)
            self.identity = dict(model.identity, **profile, numeric_source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                                 plant_ik_sha256=PINNED[VENDOR/'lib/libplantIK.so'],
                                 drake_sha256=PINNED[Path('/opt/drake/lib/libdrake.so')])
        except BaseException:
            self.close()
            raise

    def close(self):
        if hasattr(self,'closed'):
            super().close()
        if self.stderr_file is not None:
            self.stderr_file.close()
        if self.work is not None:
            self.work.cleanup()
