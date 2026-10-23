"""Build a small in-memory Python3.8 bundle; never write or launch on the robot."""

import ast
import hashlib
import json
from pathlib import Path
import shlex


MODULES = (
    'contracts.capabilities', 'contracts.command', 'contracts.intent',
    'contracts.robot_state', 'contracts.schema', 'contracts.xr', 'xr.wire',
    'robots.base', 'robots.kuavo5', 'robots.kuavo5_arm_joint_encoder',
    'robots.kuavo5_native_ik', 'robots.kuavo5_posture', 'robots.kuavo5_diagnostics', 'robots.kuavo5_native_rpc_backend',
    'robots.kuavo5_arm_ros_transport', 'robots.kuavo5_live_ros_binding',
    'robots.kuavo5_live_guard', 'robots.kuavo5_live_protocol', 'robots.kuavo5_live_agent',
    'control.joint_motion_gate', 'control.kuavo5_tracking_governor',
    'control.kuavo5_arm_execution_session',
    'control.kuavo5_live_controller', 'retargeting.dual_arm',
    'retargeting.kuavo_target_progress',
    'robots.damiao_gripper', 'control.kuavo5_gripper_teleop',
)


def build_bundle(src_root, *, mode='validate', target_recovery='baseline', speed_profile='steady', gripper_mode='off', arm_posture='baseline', gripper_config=None, return_home_config=None, standalone_return=False):
    from teleop.robots.kuavo5_posture import posture_profile
    posture_profile(arm_posture, target_recovery)
    from teleop.control.kuavo5_gripper_teleop import validate_gripper_mode
    validate_gripper_mode(gripper_mode)
    gripper_document = None
    if gripper_mode == 'damiao':
        from teleop.control.kuavo5_gripper_calibration import load_runtime_config
        if gripper_config is None:
            raise ValueError('damiao requires a calibrated --gripper-config; no hardware was opened')
        gripper_document = load_runtime_config(gripper_config).document
    elif gripper_config is not None:
        raise ValueError('gripper configuration requires grippers=damiao')
    if type(standalone_return) is not bool or (standalone_return and return_home_config is None):
        raise ValueError("standalone return requires an explicit return configuration")
    return_document = None
    if return_home_config is not None:
        if gripper_mode != 'damiao':
            raise ValueError('return-home requires grippers=damiao and a calibrated gripper configuration')
        from teleop.robots.kuavo5_return_config import load_return_config
        return_document = load_return_config(return_home_config)
    if mode not in ('validate', 'execute'):
        raise ValueError('bundle mode must be validate or execute')
    if target_recovery not in ('baseline', 'fixed_anchor', 'bounded_candidate'):
        raise ValueError('unknown target recovery policy')
    from teleop.robots.kuavo5_live_agent import motion_profile
    motion_profile(target_recovery, speed_profile)
    sources = {}
    extra = ('robots.kuavo5_model_limits', 'robots.kuavo5_bounded_profile',
             'robots.kuavo5_numeric_source', 'robots.kuavo5_numeric_process', 'robots.kuavo5_posture_source', 'robots.kuavo5_refinement_source',
             'retargeting.kuavo_workspace_bound') if target_recovery == 'bounded_candidate' else ()
    if gripper_mode == 'damiao':
        extra += ('control.kuavo5_gripper_calibration', 'robots.kuavo5_gripper_live',
                  'robots.kuavo5_gripper_owner')
    if return_document is not None:
        extra += ('robots.kuavo5_return_config', 'control.kuavo5_arm_return',
                  'control.kuavo5_return_coordinator')
    for suffix in MODULES + extra:
        source = (src_root/'teleop'/suffix.replace('.', '/')).with_suffix('.py').read_text()
        if suffix == 'retargeting.dual_arm':
            # Only runtime TYPE ALIASES need rewriting on 3.8; function/class
            # annotations use the interpreter's postponed-annotations flag.
            # This affects the transmitted copy, never the shared/G2 source.
            source = source.replace('Matrix3 = tuple[', 'from typing import Tuple\n\nMatrix3 = Tuple[')
            source = source.replace('    tuple[float, float, float],', '    Tuple[float, float, float],')
            source = source.replace('Vector3 = tuple[float, float, float]', 'Vector3 = Tuple[float, float, float]')
        ast.parse(source, filename=suffix, feature_version=(3, 8))
        sources['teleop.'+suffix] = source
    bundle = dict(mode=mode, arm_posture=arm_posture, target_recovery=target_recovery, speed_profile=speed_profile, gripper_mode=gripper_mode, sources=sources, sha256=hashlib.sha256(
        json.dumps(sources, sort_keys=True).encode()).hexdigest())
    if gripper_document is not None:
        bundle['gripper_config'] = gripper_document
        bundle['gripper_config_sha256'] = hashlib.sha256(json.dumps(
            gripper_document, sort_keys=True, allow_nan=False).encode()).hexdigest()
    if return_document is not None:
        bundle['return_home_config'] = return_document
        bundle['return_home_config_sha256'] = hashlib.sha256(json.dumps(
            return_document, sort_keys=True, allow_nan=False).encode()).hexdigest()
    if standalone_return:
        bundle["standalone_return"] = True
    return bundle


def bootstrap_program():
    # The execution gate is transport infrastructure, outside the baseline
    # 34-module motion bundle. Embed it for SSH; robot installations carry the
    # identical source as a stdlib module. Validate mode never executes it.
    gate_source = Path(__file__).with_name('kuavo5_execution_lock.py').read_text()
    program = '''
import __future__, hashlib, importlib.abc, importlib.util, json, sys
line = sys.stdin.readline(1048577)
if len(line) > 1048576:
    raise ValueError("bundle too large")
bundle = json.loads(line)
target_recovery = bundle.get("target_recovery", "baseline")
if target_recovery not in ("baseline", "fixed_anchor", "bounded_candidate"):
    raise ValueError("unknown target recovery policy")
sources = bundle["sources"]
if bundle["mode"] not in ("validate", "execute") or hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest() != bundle["sha256"]:
    raise ValueError("bundle integrity mismatch")
packages = {"teleop", "teleop.contracts", "teleop.robots", "teleop.control", "teleop.xr", "teleop.retargeting"}
class MemoryModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in sources or fullname in packages:
            return importlib.util.spec_from_loader(fullname, self, is_package=fullname in packages)
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        text = sources.get(module.__name__, "")
        code = compile(text, "<kuavo-bundle:"+module.__name__+">", "exec", flags=__future__.annotations.compiler_flag, dont_inherit=True)
        exec(code, module.__dict__)
sys.meta_path.insert(0, MemoryModules())
from teleop.robots.kuavo5_live_agent import run_robot, motion_profile
from teleop.control.kuavo5_gripper_teleop import validate_gripper_mode
gripper_mode = validate_gripper_mode(bundle.get("gripper_mode", "off"))
gripper_config = bundle.get("gripper_config")
if gripper_mode == "damiao":
    from teleop.control.kuavo5_gripper_calibration import load_runtime_config
    gripper_config = load_runtime_config(gripper_config).document
    if hashlib.sha256(json.dumps(gripper_config, sort_keys=True, allow_nan=False).encode()).hexdigest() != bundle.get("gripper_config_sha256"):
        raise ValueError("gripper configuration integrity mismatch")
elif gripper_config is not None:
    raise ValueError("unexpected gripper configuration")
return_options = {}
if "return_home_config" in bundle:
    if gripper_mode != "damiao" or bundle["return_home_config"] is None:
        raise ValueError("return-home requires grippers=damiao and an explicit configuration")
    from teleop.robots.kuavo5_return_config import load_return_config
    return_config = load_return_config(bundle["return_home_config"])
    if hashlib.sha256(json.dumps(return_config, sort_keys=True, allow_nan=False).encode()).hexdigest() != bundle.get("return_home_config_sha256"):
        raise ValueError("return-home configuration integrity mismatch")
    return_options["return_home_config"] = return_config
elif "return_home_config_sha256" in bundle:
    raise ValueError("unexpected return-home configuration digest")
standalone_return = bundle.get("standalone_return", False)
if type(standalone_return) is not bool or (standalone_return and not return_options):
    raise ValueError("invalid standalone return capability")
if standalone_return:
    return_options["standalone_return"] = True
speed_profile = bundle.get("speed_profile", "steady")
motion_profile(target_recovery, speed_profile)
from teleop.robots.kuavo5_posture import posture_profile
arm_posture = bundle.get("arm_posture", "baseline")
posture_profile(arm_posture, target_recovery)
if bundle["mode"] == "validate":
    report = dict(event="bundle_validated", python=sys.version.split()[0], ros_imported="rospy" in sys.modules, hardware_io=False, arm_posture=arm_posture, target_recovery=target_recovery, sha256=bundle["sha256"])
    if return_options:
        report.update(return_home_enabled=True, return_home_config_sha256=bundle["return_home_config_sha256"])
    print(json.dumps(report), flush=True)
else:
    gate_namespace = {}
    exec(__KUAVO_EXECUTION_GATE_SOURCE__, gate_namespace)
    gate_options = {}
    if gripper_mode == "damiao":
        import os, pwd
        if os.getuid() != 0 or os.geteuid() != 0:
            raise PermissionError("root gripper execution must share the fixed lab operator lock")
        gate_options["trusted_operator_uid"] = pwd.getpwnam("lab").pw_uid
    with gate_namespace["ExecutionLock"](route="ssh", **gate_options):
        sys.exit(run_robot(motion_enabled=True, configure_vendor_profile=True, target_recovery=target_recovery, speed_profile=speed_profile, gripper_mode=gripper_mode, arm_posture=arm_posture, gripper_config=gripper_config, **return_options))
'''
    return program.replace('__KUAVO_EXECUTION_GATE_SOURCE__', repr(gate_source))


def build_execution_ssh_command(*, control_path, gripper_control=False):
    if not isinstance(control_path, str) or not control_path.startswith('/') or '\0' in control_path:
        raise ValueError('absolute existing SSH control socket required')
    if type(gripper_control) is not bool:
        raise ValueError('gripper_control must be an explicit boolean')
    # Native owner sockets are deliberately root-private. Only an explicitly
    # authorized gripper session uses noninteractive sudo; no permission widening
    # and no password/fallback path. Ordinary arm sessions retain the lab user.
    prefix = ['/usr/bin/sudo', '-n'] if gripper_control else []
    remote = shlex.join(prefix + ['/usr/bin/env', '-i', 'PATH=/usr/bin:/bin',
        'PYTHONPATH=/home/lab/kuavo-ros-control/devel/lib/python3/dist-packages:/opt/ros/noetic/lib/python3/dist-packages',
        'ROS_MASTER_URI=http://127.0.0.1:11311', 'ROS_IP=127.0.0.1',
        '/usr/bin/python3', '-B', '-u', '-c', bootstrap_program()])
    return ['ssh', '-F', '/dev/null', '-T', '-S', control_path, '-o', 'BatchMode=yes',
            '-o', 'ProxyCommand=false', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=5', 'lab@192.168.26.1', remote]
