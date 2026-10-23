"""Local, network/device-isolated official mathematics for posture experiments.

Uses the shared geometry extension. Build is explicit, into a new output directory;
the opaque library is executed ONLY behind bwrap --unshare-all. No fallback.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess

from teleop.evaluation.kuavo_posture import validate_geometry
from teleop.robots.kuavo5_model_limits import intersect_arm_limits
from teleop.robots.kuavo5_numeric_process import (
    NumericProcess, PINNED, compiler_command, finite_vector, validate_request)
from teleop.robots.kuavo5_numeric_source import SOURCE


from teleop.robots.kuavo5_posture_source import geometry_source


def validate_posture_request(request):
    if not isinstance(request, dict) or not isinstance(request.get('ik_param'), dict):
        raise ValueError('explicit audited parameters required')
    weight = request['ik_param'].get('elbow_cost_scale')
    if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= .3:
        raise ValueError('offline elbow weight must be in [0, 0.3]')
    ordinary = deepcopy(request)
    ordinary['ik_param']['elbow_cost_scale'] = 0.
    validate_request(ordinary)


def isolated_command(build, archive):
    return ['bwrap', '--unshare-all', '--die-with-parent', '--new-session',
            '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
            '--ro-bind', '/lib64', '/lib64', '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--chdir', '/tmp', '--clearenv',
            '--setenv', 'PATH', '/usr/bin:/bin',
            '--setenv', 'LD_LIBRARY_PATH', '/replay/vendor/lib',
            '--setenv', 'DRAKE_RESOURCE_ROOT', '/replay/vendor/share',
            '--setenv', 'OMP_NUM_THREADS', '1', '--setenv', 'OPENBLAS_NUM_THREADS', '1',
            '--ro-bind', str(archive.resolve()), '/replay',
            '--ro-bind', str(build.resolve()), '/candidate',
            '/candidate/worker', '/candidate/ik.urdf',
            '/replay/vendor/source/kuavo.json', '/replay/full_robot.urdf']


def refinement_source(refinement='vendor'):
    source = geometry_source()
    if refinement == 'vendor':
        return source
    if refinement == 'retain_successful':
        from teleop.robots.kuavo5_refinement_source import retain_successful_source
        return retain_successful_source(source)
    raise ValueError('unknown offline refinement policy')


def build_worker(output, archive, *, refinement='vendor'):
    """Compile only. Do not run or ldd the opaque vendor library on the host."""
    source = refinement_source(refinement)
    output, archive = Path(output).resolve(), Path(archive).resolve()
    vendor = archive/'vendor'
    expected = {p.name: h for p, h in PINNED.items()}
    files = [vendor/'lib/libplantIK.so', vendor/'lib/libdrake.so',
             vendor/'include/plantIK.h', vendor/'source/kuavo.json']
    for p in files:
        if hashlib.sha256(p.read_bytes()).hexdigest() != expected[p.name]:
            raise ValueError('archived official dependency identity mismatch: '+str(p))
    config = json.loads(files[-1].read_text())
    if config['end_frames_name_ik'] != ['base_link', 'zarm_l7_end_effector',
            'zarm_r7_end_effector', 'zarm_l4_link', 'zarm_r4_link']:
        raise ValueError('audited v63 geometry frame names required')
    output.mkdir(parents=True, exist_ok=False)
    xml, changes = intersect_arm_limits((archive/'official_ik.urdf').read_text(),
                                       (archive/'full_robot.urdf').read_text())
    (output/'ik.urdf').write_text(xml)
    (output/'worker.cpp').write_text(source)
    runtime = Path(__file__).resolve().parents[3]
    json_include = runtime.parents[2]/'dependencies/leju-kuavo5-w/kuavo-ros-opensource/src/manipulation_nodes/motion_capture_ik/include'
    command = compiler_command(output/'worker.cpp', output/'worker',
        include=vendor/'include', drake_include=vendor/'include',
        library=vendor/'lib', drake_library=vendor/'lib', json_include=json_include)
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=120,
                              env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
    (output/'build.txt').write_text(compiled.stdout+compiled.stderr)
    if compiled.returncode:
        raise ValueError('offline build failed: '+compiled.stderr[-2000:])
    identity = {str(p.relative_to(archive)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in files+[archive/'official_ik.urdf', archive/'full_robot.urdf']}
    identity['json_header_sha256'] = hashlib.sha256((json_include/'motion_capture_ik/json.hpp').read_bytes()).hexdigest()
    identity.update(refinement=refinement, source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                    intersection_sha256=hashlib.sha256(xml.encode()).hexdigest(),
                    binary_sha256=hashlib.sha256((output/'worker').read_bytes()).hexdigest(),
                    limit_changes=changes)
    (output/'identity.json').write_text(json.dumps(identity, indent=2)+'\n')
    return output


def verify_build(build, archive):
    build, archive = Path(build).resolve(), Path(archive).resolve()
    identity = json.loads((build/'identity.json').read_text())
    expected = {
        'source_sha256': hashlib.sha256(refinement_source(identity.get('refinement', 'vendor')).encode()).hexdigest(),
        'binary_sha256': hashlib.sha256((build/'worker').read_bytes()).hexdigest(),
        'intersection_sha256': hashlib.sha256((build/'ik.urdf').read_bytes()).hexdigest(),
    }
    if hashlib.sha256((build/'worker.cpp').read_bytes()).hexdigest() != expected['source_sha256']:
        raise ValueError('worker source differs from current experiment')
    for key, digest in expected.items():
        if identity.get(key) != digest:
            raise ValueError('worker identity mismatch: '+key)
    for key in ('vendor/lib/libplantIK.so', 'vendor/lib/libdrake.so',
                'vendor/include/plantIK.h', 'vendor/source/kuavo.json',
                'official_ik.urdf', 'full_robot.urdf'):
        if hashlib.sha256((archive/key).read_bytes()).hexdigest() != identity.get(key):
            raise ValueError('archived dependency changed: '+key)
    xml, _ = intersect_arm_limits((archive/'official_ik.urdf').read_text(),
                                 (archive/'full_robot.urdf').read_text())
    if (build/'ik.urdf').read_text() != xml:
        raise ValueError('private model is not the unchanged limit intersection')


class OfflinePostureNumeric(NumericProcess):
    def __init__(self, build, archive, stderr):
        self.command = isolated_command(Path(build), Path(archive))
        child = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=stderr, bufsize=0)
        super().__init__(child, check=lambda: None, timeout_s=10., ready_timeout_s=15.)

    def geometry(self, q):
        finite_vector(q, 14)
        result = self._request(dict(op='geometry', q=list(q)))
        try:
            return validate_geometry(result)
        except ValueError:
            self.failed = True
            raise

    def inverse(self, request):
        validate_posture_request(request)
        return self._request(dict(op='ik', request=request))
