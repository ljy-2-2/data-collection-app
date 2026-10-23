"""Package the independent base channel for the existing vendor Python/ROS."""
import ast
import hashlib
import json
import shlex
from teleop.retargeting.mobile_base import base_speed_limits

MODULES=('contracts.xr','contracts.base_motion','xr.wire','retargeting.mobile_base',
         'control.kuavo5_base_session','robots.kuavo5_live_protocol',
         'robots.kuavo5_base_datagram','robots.kuavo5_base_timing','robots.kuavo5_base_ros','robots.kuavo5_base_agent')


def build_bundle(src_root, *, execute=False, duration=60.,speed_profile='vendor_vr',input_transport='ssh'):
    base_speed_limits(speed_profile)
    if input_transport not in ('ssh','udp'): raise ValueError('invalid input transport')
    if type(execute) is not bool or type(duration) not in (int,float) or not 0<duration<=300:
        raise ValueError('invalid base execution options')
    sources={}
    for suffix in MODULES:
        source=(src_root/'teleop'/suffix.replace('.','/')).with_suffix('.py').read_text()
        ast.parse(source,feature_version=(3,8))
        sources['teleop.'+suffix]=source
    return dict(mode='execute' if execute else 'validate',duration=duration,sources=sources,speed_profile=speed_profile,input_transport=input_transport,
                sha256=hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest())


def bootstrap_program():
    return '''
import __future__, hashlib, importlib.abc, importlib.util, json, sys
line=sys.stdin.readline(1048577)
if len(line)>1048576: raise ValueError("base bundle too large")
bundle=json.loads(line)
sources=bundle["sources"]
if bundle["mode"] not in ("validate","execute") or hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest()!=bundle["sha256"]:
    raise ValueError("base bundle integrity mismatch")
duration=bundle["duration"]
if type(duration) not in (int,float) or not 0<duration<=300: raise ValueError("invalid duration")
packages={"teleop","teleop.contracts","teleop.xr","teleop.control","teleop.robots","teleop.retargeting"}
class MemoryModules(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def find_spec(self,fullname,path=None,target=None):
        if fullname in sources or fullname in packages:
            return importlib.util.spec_from_loader(fullname,self,is_package=fullname in packages)
    def create_module(self,spec): return None
    def exec_module(self,module):
        code=compile(sources.get(module.__name__,""),"<base-bundle:"+module.__name__+">","exec",flags=__future__.annotations.compiler_flag,dont_inherit=True)
        exec(code,module.__dict__)
sys.meta_path.insert(0,MemoryModules())
from teleop.robots.kuavo5_base_agent import run_robot
from teleop.retargeting.mobile_base import base_speed_limits
profile=bundle.get("speed_profile","vendor_vr")
base_speed_limits(profile)
transport=bundle.get("input_transport","ssh")
if transport not in ("ssh","udp"): raise ValueError("invalid input transport")
if bundle["mode"]=="validate":
    print(json.dumps(dict(event="base_bundle_validated",sha256=bundle["sha256"],speed_profile=profile,ros_imported="rospy" in sys.modules,hardware_io=False)),flush=True)
else:
    sys.exit(run_robot(motion_enabled=True,max_duration_s=duration+10,speed_profile=profile,
                      input_transport=transport,input_key=bundle.get("input_key")))
'''


def ssh_command(control_path):
    if not isinstance(control_path,str) or not control_path.startswith('/') or '\0' in control_path:
        raise ValueError('absolute existing SSH control socket required')
    remote=shlex.join(['/usr/bin/env','-i','PATH=/usr/bin:/bin',
        'PYTHONPATH=/home/lab/kuavo-ros-control/devel/lib/python3/dist-packages:/opt/ros/noetic/lib/python3/dist-packages',
        'ROS_MASTER_URI=http://127.0.0.1:11311','ROS_IP=192.168.26.1',
        '/usr/bin/python3','-B','-u','-c',bootstrap_program()])
    return ['ssh','-F','/dev/null','-T','-S',control_path,'-o','BatchMode=yes','-o',
            'ProxyCommand=false','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5',
            'lab@192.168.26.1',remote]
