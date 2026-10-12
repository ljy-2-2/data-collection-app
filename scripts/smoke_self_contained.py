#!/usr/bin/env python3
"""Install with dpkg in isolated Ubuntu 22.04/24.04, then open the real Qt UI."""
import argparse
from pathlib import Path
import subprocess

CHECKS = r'''
import os, re, shutil, sys
from pathlib import Path

def reject_builder_reference(value, label):
    assert '/home/ljy' not in str(value), f'{label} depends on the build machine /home/ljy'

def check_configuration_paths(package, workspace=None):
    # Inspect startup/configuration inputs, not vendored documentation or debug
    # strings: those may mention a build path without loading anything from it.
    files=[package/relative for relative in (
        'defaults.env', 'runtime/env.sh', 'runtime/docker-daemon.json',
        'runtime/docker-cli/config.json', 'runtime/fonts.conf',
        'runtime/python/pyvenv.cfg', 'runtime/bin/docker',
        'runtime/robot/bin/python3', 'runtime/robot/xr/runService.sh',
        'runtime/robot/xr/setting.ini')]
    robot_config=package/'runtime/robot/gdk/gdk/config'
    if robot_config.is_dir():
        files.extend(path for path in robot_config.rglob('*') if path.suffix in ('.json','.yml','.yaml','.conf','.ini'))
    for root in (package/'app', workspace):
        if root is None:continue
        files += [root/relative for relative in (
            '.env', 'umi_collection/vendor/galaxis/.env',
            'umi_collection/vendor/galaxis/docker-compose.yml',
            'umi_collection/vendor/galaxis/galaxis_bringup/config/galaxis_config.yaml',
            'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json',
            'teleop_control/vendor/tianji/teleop/gripper/config.json')]
    for pattern in ('*/site-packages/*.pth', '*/site-packages/*.egg-link'):
        files.extend((package/'runtime/python/lib').glob(pattern))
    for file in files:
        if file.is_file():reject_builder_reference(file.read_text(), str(file))
    for link in package.rglob('*'):
        if link.is_symlink():
            reject_builder_reference(os.readlink(link), f'symlink {link}')
            reject_builder_reference(link.resolve(), f'resolved symlink {link}')

def check_private_executable(executable, package):
    executable=Path(executable)
    assert executable.is_file() and os.access(executable,os.X_OK), f'Private executable missing: {executable}'
    assert executable.resolve().is_relative_to((package/'runtime').resolve()), f'Executable escaped private runtime: {executable}'

def check_private_module(module, package):
    origin=getattr(module,'__file__',None)
    assert origin, f'Cannot locate robot dependency: {module.__name__}'
    assert Path(origin).resolve().is_relative_to((package/'runtime').resolve()), f'Robot dependency escaped private runtime: {module.__name__} at {origin}'

def check_native_dependency_log(output):
    # SDK imports may return normally after a C++ dlopen failure. Device and
    # discovery errors are expected without hardware; loader failures are not.
    pattern=r'dlopen[^\n]*(?:fail|error)|cannot open shared object file|error while loading shared libraries|undefined symbol:|version [`\x27\x22][^\n]+not found'
    problem=re.search(pattern,output,re.IGNORECASE)
    assert problem is None, f'Bundled native dependency failed to load: {problem.group(0) if problem else ""}'

def check_brightness_routes(schema):
    route=schema.get('paths',{}).get('/umi/camera-controls/{camera_key}/brightness',{})
    assert {'get','post'} <= set(route), 'Installed backend is missing camera brightness GET/POST routes'
    resolution=schema.get('paths',{}).get('/umi/camera-controls/{camera_key}/resolution',{})
    assert {'get','post'} <= set(resolution), 'Installed backend is missing camera resolution GET/POST routes'

def check_running_paths(pid):
    pending=[pid];seen=set()
    while pending:
        current=pending.pop()
        if current in seen:continue
        seen.add(current);proc=Path('/proc')/str(current)
        if not proc.exists():continue
        for name in ('cmdline','environ','maps'):
            try:value=(proc/name).read_bytes().decode(errors='replace')
            except FileNotFoundError:continue
            reject_builder_reference(value,f'process {current} {name}')
        for name in ('exe','cwd'):
            try:value=os.readlink(proc/name)
            except FileNotFoundError:continue
            reject_builder_reference(value,f'process {current} {name}')
        try:children=(proc/'task'/str(current)/'children').read_text().split()
        except FileNotFoundError:children=[]
        pending.extend(int(child) for child in children)
'''

GROUP_PROBE = r'''
import errno, grp, importlib.util, os, socket, sys, time
from pathlib import Path
package=Path('/opt/xiaokai-lab-data-collection')
sys.path.insert(0,str(package/'scripts'))
import package_launcher
fixture=Path(__file__).resolve().parents[1]
primary=int(sys.argv[1]);legacy_pid=int(sys.argv[2])
required={grp.getgrnam(name).gr_gid for name in ('xiaokai-collection','dialout','video','plugdev')}
if not os.environ.get('XIAOKAI_GROUP_REFRESHED'):
    assert os.getgid()==primary and os.getegid()==primary
    assert not required.issubset(set(os.getgroups())), 'Test did not simulate an old desktop login'
package_launcher.ensure_collection_permissions(fixture)
collector_path=package/'app/umi_collection/vendor/galaxis/tools/galaxis_collection.py'
spec=importlib.util.spec_from_file_location('group_test_collector',collector_path)
collector=importlib.util.module_from_spec(spec);spec.loader.exec_module(collector)
collector.LEGACY_ROBOTICS_SERVICE=fixture/'RoboticsServiceProcess'
visible=collector.legacy_robotics_service_pids()
assert legacy_pid in visible, f'Refreshed App cannot identify its same-user PICO service: gid={os.getgid()}, expected={primary}'
assert visible==[legacy_pid], f'Unexpected process matched the disposable service: {visible}'
assert os.getgid()==primary and os.getegid()==primary, 'Group refresh changed the desktop primary group'
assert required.issubset({*os.getgroups(),os.getegid()}), 'Collection supplementary groups remain stale'
assert os.environ.get('XIAOKAI_GROUP_REFRESHED')=='1'
assert collector.terminate_legacy_robotics_service()==[legacy_pid]
deadline=time.monotonic()+3
while True:
    with socket.socket() as listener:
        try:listener.bind(('127.0.0.1',63901));listener.listen()
        except OSError as exc:
            if exc.errno!=errno.EADDRINUSE or time.monotonic()>=deadline:raise
        else:break
    time.sleep(.02)
print('PASS first-install group refresh preserves primary group and releases the simulated PICO tcp:63901 listener',flush=True)
'''

GROUP_VERIFY = r'''
import os, pwd, select, shutil, subprocess, tempfile
from pathlib import Path
assert os.getuid()==0, 'Group regression must run as root inside the isolated smoke container'
assert not Path('/home/ljy').exists(), 'Group regression must never run on the build host'
for path in ('/var/run/docker.sock','/run/dbus/system_bus_socket','/tmp/.X11-unix'):
    assert not Path(path).exists(), f'Host socket must not be mounted: {path}'
package=Path('/opt/xiaokai-lab-data-collection')
python=package/'runtime/python/bin/python3.12'
user=pwd.getpwnam('xiaokai-test')
with tempfile.TemporaryDirectory(prefix='xiaokai-group-regression-') as directory:
    fixture=Path(directory);fixture.chmod(0o755)
    for name in ('umi-runtime','scripts','runtime/python/bin'):
        (fixture/name).mkdir(parents=True)
    (fixture/'self-contained.json').write_text('{}')
    (fixture/'umi-runtime/manifest.json').write_text('{}')
    (fixture/'runtime/python/bin/python3.12').symlink_to(python)
    (fixture/'scripts/package_launcher.py').write_text(GROUP_PROBE)
    executable=fixture/'RoboticsServiceProcess'
    shutil.copy2(python.resolve(),executable)
    def desktop_identity():
        os.setgroups([user.pw_gid]);os.setgid(user.pw_gid);os.setuid(user.pw_uid)
    # This disposable exact-path service lives in the container's network/PID
    # namespace, with the desktop's primary group before any sg refresh.
    service_code='import os,socket,time; sock=socket.socket(); sock.bind(("127.0.0.1",63901)); sock.listen(); print(os.getpid(),flush=True); time.sleep(90)'
    service=subprocess.Popen([str(executable),'-c',service_code],preexec_fn=desktop_identity,
        env={**os.environ,'PYTHONHOME':str(package/'runtime/python')},
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert select.select([service.stdout],[],[],10)[0], 'Simulated listener did not start'
        ready=service.stdout.readline().strip()
        assert ready==str(service.pid), f'Simulated listener failed: {ready!r}'
        # Plain runuser refreshes initgroups, bypassing the first-install bug.
        command=['runuser','--user',user.pw_name,'--group',user.pw_name,
                 '--supp-group',user.pw_name,'--',str(python),
                 str(fixture/'scripts/package_launcher.py'),str(user.pw_gid),str(service.pid)]
        env=dict(os.environ);env.pop('XIAOKAI_GROUP_REFRESHED',None)
        subprocess.run(command,env=env,check=True,timeout=30)
        assert service.wait(timeout=5)!=0, 'Expected the collector to terminate its disposable legacy service'
    finally:
        if service.poll() is None:
            service.terminate()
            try:service.wait(timeout=5)
            except subprocess.TimeoutExpired:service.kill();service.wait()
'''

ROBOT_VERIFY = CHECKS + r'''
import ctypes, importlib, json, math, socket, subprocess, time
package=Path('/opt/xiaokai-lab-data-collection')
assert not Path('/home/ljy').exists(), 'Robot smoke must only run in the isolated container'
for path in ('/var/run/docker.sock','/run/dbus/system_bus_socket','/tmp/.X11-unix'):
    assert not Path(path).exists(), f'Host socket must not be mounted: {path}'
for pattern in ('video*','ttyUSB*','ttyACM*'):
    assert not list(Path('/dev').glob(pattern)), 'Robot smoke must not receive hardware'
assert sys.version_info[:2]==(3,10), 'Robot extensions require their private Python 3.10 ABI'
check_private_executable(sys.executable,package)
for name in ('numpy','scipy','pydantic','cv2','yaml','h5py','pinocchio','agibot_gdk','xrobotoolkit_sdk'):
    module=importlib.import_module(name)
    check_private_module(module,package)
    print(f'PASS private Python 3.10 dependency {name}',flush=True)
# No explicit SDK init(), discover or connect calls. The GDK extension has a
# native DDS import initializer, which is why this entire test is isolated.
for name in ('libdynamic_aorta.so','libidl_proto_bridge.so','libcosine_bus_agibotdds.so'):
    library=package/'runtime/robot/gdk/lib'/name
    assert library.is_file(), f'Missing dynamically loaded GDK plugin: {name}'
    ctypes.CDLL(str(library))
print('PASS all required GDK dynamic plugins load from the private runtime',flush=True)
import numpy as np
runtime=package/'app/teleop_control/vendor/teleop_runtime'
vendor=package/'app/teleop_control/vendor'
for path in (package/'app/teleop_control/src',vendor,runtime,runtime/'src',vendor/'gdk_data_collection'):
    sys.path.insert(0,str(path))
# These production modules only declare their workers on import. Their main(),
# SDK init/connect calls and worker/collector constructors are never invoked.
# The actual movement worker is gdk_move.py (there is no gdk_move_worker.py).
worker_modules=('teleop_control.tianji_worker','teleop_control.gdk_preview_worker',
                'teleop_control.gdk_move','run_new_teleop_collection','remote_collector',
                'apps.pico_g2_dual_arm_live','apps.platform_collection',
                'collector.collector','collector.episode_writer','collector.gdk_io')
for name in worker_modules:
    module=importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(package/'app'), f'Worker loaded outside installed App: {name}'
    print(f'PASS Python 3.10 production worker import {name}',flush=True)
from teleop.robots.g2 import make_g2_joint_schema
from teleop.robots.g2_urdf_ik import G2URDFIKSolver
from teleop.contracts.robot_state import RobotState
from teleop.contracts.intent import EndEffectorPoseIntent
schema=make_g2_joint_schema(max_step_rad=.02)
solver=G2URDFIKSolver(joint_schema=schema)
stamp=time.monotonic_ns()
state=RobotState(positions={name:0.0 for name in schema.names},timestamp_ns=stamp)
poses=solver.forward_kinematics(state)
assert set(poses)=={'left','right'}
command=solver.solve(EndEffectorPoseIntent(targets=poses),state,sequence_id=1,now_ns=stamp,ttl_ms=100)
assert len(command.targets)==14 and all(math.isfinite(value) for value in command.targets.values())
assert solver._urdf_path.resolve().is_relative_to(runtime)
print('PASS packaged G2 URDF, forward kinematics and dual-arm IK in memory',flush=True)

tianji=package/'app/teleop_control/vendor/tianji'
os.environ['MARVIN_SDK_ROOT']=str(tianji)
sys.path.insert(0,str(tianji));sys.path.insert(0,str(tianji/'teleop'))
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models
from SDK_PYTHON.fx_robot import Marvin_Robot
native_robot=Marvin_Robot()  # Constructor only dlopens libMarvinSDK; no connect().
assert hasattr(native_robot.robot,'OnLinkTo')
models=Models(configuration(tianji/'teleop/marvin_teleop/config.json'))
assert set(models.arms)=={'left','right'}
for side,arm in models.arms.items():
    joints=np.array([0,-35,0,-65,0,0,0],dtype=float)
    pose=arm.fk(joints)
    reference=arm.chain(joints)[-1] @ arm.flange @ arm.tool
    assert pose.shape==(4,4) and np.isfinite(pose).all()
    assert np.allclose(pose,reference,atol=1e-4), f'{side} native FK differs from its packaged URDF'
print('PASS Tianji robot library, both native kinematics models and matching CAD/URDF poses without connect',flush=True)
check_running_paths(os.getpid())

def process_listeners(pid):
    inodes=set()
    try:fds=list((Path('/proc')/str(pid)/'fd').iterdir())
    except FileNotFoundError:return set()
    for fd in fds:
        try:target=os.readlink(fd)
        except FileNotFoundError:continue
        if target.startswith('socket:['):inodes.add(target[8:-1])
    ports=set()
    for table in ('tcp','tcp6'):
        try:rows=(Path('/proc/net')/table).read_text().splitlines()[1:]
        except FileNotFoundError:continue
        for line in rows:
            fields=line.split()
            if fields[3]=='0A' and fields[9] in inodes:ports.add(int(fields[1].rsplit(':',1)[1],16))
    return ports

xr=package/'runtime/robot/xr'
for port in (60061,63901):
    with socket.socket() as probe:probe.bind(('127.0.0.1',port))
log=Path('/tmp/xiaokai-xr-smoke.log')
with log.open('w') as output:
    service=subprocess.Popen([str(xr/'runService.sh')],stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
try:
    until=time.monotonic()+20
    while not {60061,63901}.issubset(process_listeners(service.pid)):
        if service.poll() is not None or time.monotonic()>until:raise RuntimeError(log.read_text())
        time.sleep(.1)
    assert (Path('/proc')/str(service.pid)/'exe').resolve()==xr/'RoboticsServiceProcess'
    check_running_paths(service.pid)
    qt=[line.split()[-1] for line in (Path('/proc')/str(service.pid)/'maps').read_text().splitlines() if '/libQt6' in line]
    assert qt and all(Path(path).is_relative_to(xr) for path in qt), 'XR loaded another application Qt version'
    print('PASS bundled XR service owns 60061/63901 with its private vendor Qt libraries',flush=True)
finally:
    if service.poll() is None:
        service.terminate()
        try:service.wait(timeout=10)
        except subprocess.TimeoutExpired:service.kill();service.wait()
for port in (60061,63901):
    with socket.socket() as probe:probe.bind(('127.0.0.1',port))
print('PASS test-owned XR process stops and releases both listener ports',flush=True)
'''

ROBOT_DRIVER = CHECKS + r'''
import subprocess
package=Path('/opt/xiaokai-lab-data-collection')
result=subprocess.run([str(package/'runtime/robot/bin/python3'),'/tmp/verify-robot-runtime.py'],
    stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
Path('/tmp/xiaokai-robot-runtime.log').write_text(result.stdout)
print(result.stdout,end='',flush=True)
check_native_dependency_log(result.stdout)
assert result.returncode==0, f'Robot runtime smoke failed ({result.returncode}); see /tmp/xiaokai-robot-runtime.log'
'''

APP_DEFAULTS = CHECKS + r'''
package=Path('/opt/xiaokai-lab-data-collection')
app_root=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(app_root/'teleop_control/src'))
from teleop_control import main as control
from teleop_control.gdk_env import build_gdk_env,add_xr_runtime_env
from teleop_control.runtime_paths import resolve_robot_python
from teleop_control.tianji_config import robot_configuration
sys.argv=['installed-defaults-smoke']
args=control.parse_args()
robot=package/'runtime/robot'
assert Path(args.gdk_python).resolve()==robot/'bin/python3', args.gdk_python
assert Path(args.gdk_app_root).resolve()==robot/'gdk', args.gdk_app_root
assert Path(args.xr_service).resolve()==robot/'xr/runService.sh', args.xr_service
assert Path(resolve_robot_python()).resolve()==robot/'bin/python3'
assert Path(robot_configuration()['service_dir']).resolve()==robot/'xr'
# This only reads loopback interface metadata; no SDK objects or connections.
env=add_xr_runtime_env(build_gdk_env(args.gdk_app_root,robot_interface='lo'),args.xr_service)
assert str(robot/'gdk/gdk/lib') in env['PYTHONPATH'].split(os.pathsep)
assert str(robot/'gdk/lib') in env['LD_LIBRARY_PATH'].split(os.pathsep)
assert str(robot/'xr/SDK/x64') in env['LD_LIBRARY_PATH'].split(os.pathsep)
assert env['QT_PLUGIN_PATH'].split(os.pathsep)[0]==str(robot/'xr/plugins')
assert Path(env['APP_CONF_PATH']).resolve().is_relative_to(robot/'gdk/gdk/config')
for name,value in env.items():reject_builder_reference(value,f'worker environment {name}')
print('PASS App Python 3.12 selects bundled G2/Tianji Python, SDK and XR service: '+str(app_root),flush=True)
'''

VERIFY = 'APP_DEFAULTS = ' + repr(APP_DEFAULTS) + '\n' + CHECKS + r'''
import json, signal, socket, subprocess, time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
package=Path('/opt/xiaokai-lab-data-collection')
assert not Path('/home/ljy').exists(), 'Build-machine home must not be mounted'
for path in ('/var/run/docker.sock','/run/dbus/system_bus_socket','/tmp/.X11-unix'):
    assert not Path(path).exists(), f'Host socket must not be mounted: {path}'
for pattern in ('video*','ttyUSB*','ttyACM*'):
    assert not list(Path('/dev').glob(pattern)), 'Hardware must not be passed into this smoke test'
qml=package/'app/scripts/qt_ui/qml/CameraBrightness.qml'
assert qml.is_file() and qml.stat().st_size, 'Installed package is missing CameraBrightness.qml'
check_private_executable(sys.executable,package)
check_private_executable(os.environ['XIAOKAI_SECRET_PYTHON'],package)
v4l2=package/'runtime/bin/v4l2-ctl'
check_private_executable(v4l2,package)
assert Path(shutil.which('v4l2-ctl')).resolve()==v4l2.resolve()
subprocess.run([str(v4l2),'--version'],check=True)
check_configuration_paths(package)
subprocess.run([sys.executable,'-c',APP_DEFAULTS,str(package/'app')],check=True,timeout=20)
import PySide6.QtQuick, PySide6.QtQuick3D, cv2, httpx
for value in (sys.executable,sys.prefix,sys.base_prefix,*sys.path):reject_builder_reference(value,'Python search path')
for name,module in tuple(sys.modules.items()):
    if getattr(module,'__file__',None):reject_builder_reference(module.__file__,f'imported module {name}')
for name,value in os.environ.items():reject_builder_reference(value,f'environment {name}')
check_running_paths(os.getpid())
print('PASS private Python/Qt/graphics libraries', flush=True)
print('PASS packaged brightness UI, private v4l2-ctl and clean runtime/configuration paths',flush=True)
subprocess.run([os.environ['XIAOKAI_SECRET_PYTHON'], '-I', '-c', 'import gi; gi.require_version("Secret","1"); from gi.repository import Secret; print("PASS private libsecret helper")'], check=True)
for command in [['docker','compose','version'],['adb','version'],['dockerd','--validate','--config-file',str(package/'runtime/docker-daemon.json')],['containerd','--version'],['runc','--version']]:
    check_private_executable(shutil.which(command[0]),package)
    subprocess.run(command, check=True)
env={**os.environ,'QT_QPA_PLATFORM':'offscreen','QSG_RHI_BACKEND':'opengl','QSG_RENDER_LOOP':'basic','QT_SCALE_FACTOR':'1','LIBGL_ALWAYS_SOFTWARE':'1'}
log=Path('/tmp/xiaokai-ui-test.log')
with log.open('w') as output:
    app=subprocess.Popen(['/usr/bin/xiaokai-lab-data-collection','--software-rendering'],env=env,stdout=output,stderr=subprocess.STDOUT)
try:
    until=time.monotonic()+70
    while 'NATIVE_SERVICE_READY' not in log.read_text():
        if app.poll() is not None or time.monotonic()>until: raise RuntimeError(log.read_text())
        time.sleep(.2)
    with urlopen('http://127.0.0.1:8011/platform/auth',timeout=5) as response:
        assert not json.load(response)['authenticated']
    with urlopen('http://127.0.0.1:8011/umi/status',timeout=5) as response:
        assert json.load(response)['record_format']=='rosbag'
    for profile in ('dual','single-left','single-right'):
        request=Request('http://127.0.0.1:8011/umi/check?collection_profile='+profile,data=b'',method='POST')
        with urlopen(request,timeout=40) as response:
            checked=json.load(response)
            assert response.status==200
            assert checked['collection_profile']==profile
            devices=checked['monitor']['device_check']
            assert devices['expected_cameras']==(3 if profile=='dual' else 2)
            assert isinstance(devices['camera_candidates'],list)
            assert not devices['ready'], 'No hardware in isolated container must not report ready'
    print('PASS installed real UMI hardware-check HTTP endpoint for dual and both single-arm profiles',flush=True)
    with urlopen('http://127.0.0.1:8011/openapi.json',timeout=5) as response:
        check_brightness_routes(json.load(response))
    print('PASS running installed backend exposes camera brightness GET/POST',flush=True)
    build_id=json.loads((package/'package-build.json').read_text())['build_id']
    workspace=Path.home()/'.local/share/xiaokai-lab-data-collection/workspaces'/build_id
    assert (workspace/'scripts/qt_ui/qml/CameraBrightness.qml').read_bytes()==qml.read_bytes()
    check_configuration_paths(package,workspace)
    subprocess.run([sys.executable,'-c',APP_DEFAULTS,str(workspace)],check=True,timeout=20)
    check_running_paths(app.pid)
    project=workspace/'umi_collection/vendor/galaxis'
    resolved=json.loads(subprocess.check_output(['docker','compose','-f',str(project/'docker-compose.yml'),'config','--format','json'],text=True))
    assert resolved['services']['galaxis']['environment']['ROBOT_VARIANT']=='portable'
    assert resolved['services']['web_video_server']['environment']['WEB_VIDEO_SERVER_PORT']=='9091'
    reject_builder_reference(json.dumps(resolved),'resolved collection Compose configuration')
    print('PASS running UI/backend and workspace do not load from build-machine home',flush=True)
    print('PASS installed UMI monitoring Compose configuration resolves in user workspace',flush=True)
    request=Request('http://127.0.0.1:8011/platform/auth/login',data=json.dumps({'username':'offline-smoke','password':'invalid-test-password'}).encode(),headers={'Content-Type':'application/json'},method='POST')
    try:
        urlopen(request,timeout=20)
        raise AssertionError('Unavailable cloud allowed login')
    except HTTPError as exc:
        assert exc.code==502,exc.code
    assert app.poll() is None,log.read_text()
    assert 'QQmlApplicationEngine failed' not in log.read_text()
    print('PASS real Qt startup, backend, strict authentication and rosbag default',flush=True)
    app.send_signal(signal.SIGTERM)
    assert app.wait(timeout=35)==0,log.read_text()
    with socket.socket() as sock: assert sock.connect_ex(('127.0.0.1',8011))!=0
    print('PASS graceful shutdown releases backend port',flush=True)
finally:
    if app.poll() is None: app.kill();app.wait()
fixture=package/'app/scripts/smoke_qt_ui.py'
assert fixture.is_file(), 'Package is missing the Qt page integration fixture'
subprocess.run([sys.executable,str(fixture)],env=env,cwd=Path.home(),check=True,timeout=60)
print('PASS installed Qt login, Tianji, G2, UMI and theme/resize fixture flows',flush=True)
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('deb',type=Path)
    parser.add_argument('--ubuntu',choices=('22.04','24.04'),default='22.04',
                        help='Locally cached Ubuntu image to test (default: 22.04; never pulls)')
    args=parser.parse_args()
    if not args.deb.is_file():parser.error('deb must be an existing package file')
    script=r'''
set -eu
useradd -m -u 2000 xiaokai-test
SUDO_USER=xiaokai-test SUDO_UID=2000 dpkg -i /input/app.deb
test -z "$(dpkg --audit)"
test "$(dpkg-query -W -f='${Status}' xiaokai-lab-data-collection)" = 'install ok installed'
test ! -x /usr/bin/python3
test ! -x /usr/bin/docker
test ! -x /usr/bin/adb
printf '%s\n' 'PASS dpkg -i completed without host Python, Docker, ADB or apt installation'
cat > /tmp/verify-first-install-groups.py <<'PY'
''' + 'GROUP_PROBE = ' + repr(GROUP_PROBE) + '\n' + GROUP_VERIFY + r'''
PY
sh -c '. /opt/xiaokai-lab-data-collection/runtime/env.sh; exec /opt/xiaokai-lab-data-collection/runtime/python/bin/python3.12 /tmp/verify-first-install-groups.py'
cat > /tmp/verify-robot-runtime.py <<'PY'
''' + ROBOT_VERIFY + r'''
PY
cat > /tmp/verify-robot-driver.py <<'PY'
''' + ROBOT_DRIVER + r'''
PY
runuser -u xiaokai-test -- sh -c '. /opt/xiaokai-lab-data-collection/runtime/env.sh; exec /opt/xiaokai-lab-data-collection/runtime/python/bin/python3.12 /tmp/verify-robot-driver.py'
cat > /tmp/verify-private-runtime.py <<'PY'
''' + VERIFY + r'''
PY
runuser -u xiaokai-test -- sh -c '. /opt/xiaokai-lab-data-collection/runtime/env.sh; exec /opt/xiaokai-lab-data-collection/runtime/python/bin/python3.12 /tmp/verify-private-runtime.py'
dpkg -r xiaokai-lab-data-collection
test ! -e /usr/bin/xiaokai-lab-data-collection
test ! -e /etc/systemd/system/multi-user.target.wants/xiaokai-collection-docker.service
printf '%s\n' 'PASS uninstall removes launcher and service links'
'''
    # Only the package is mounted. Never pass host devices, a Docker socket,
    # host namespaces, or network access; the image must already be cached.
    subprocess.run(['docker','run','--rm','--pull=never','--network','none','--interactive',
                    '--mount',f'type=bind,source={args.deb.resolve()},target=/input/app.deb,readonly',
                    'ubuntu:'+args.ubuntu,'bash','-s'],input=script,text=True,check=True)


if __name__=='__main__': main()
