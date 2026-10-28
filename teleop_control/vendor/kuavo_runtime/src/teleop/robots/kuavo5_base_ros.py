"""Explicitly enabled, base-only ROS binding. Never changes manufacturer modes.

Only /cmd_vel is published. No controller launch, IK, motor enable, mode/reset
service or direct /move_base/base_cmd_vel fallback exists in this module.
"""
import math
import hashlib
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET

from teleop.control.kuavo5_base_session import BaseFeedback, BaseHealth, InputDeadlineExpired
from teleop.retargeting.mobile_base import base_speed_limits


# Local engineering budget, not a vendor timing guarantee. /odom is produced
# on .22 and consumed on .1. Read-only samples showed about 11ms future skew.
# If the source clock leads by <= B, true_age <= (body_now - source_stamp) + B.
# Charge B to EVERY accepted sample, so allowing bounded skew cannot lengthen
# the existing 200ms feedback lifetime. Never infer B from the first sample.
ODOM_CLOCK_SKEW_BUDGET_NS=20_000_000
ODOM_FEEDBACK_MAX_AGE_NS=200_000_000

PROFILE_KEYS=('/vr_cmd_vel/linear_scale_x','/vr_cmd_vel/linear_scale_y','/vr_cmd_vel/angular_scale_z')
PROFILE=(.4,.4,.25)
LAUNCH='/home/lab/kuavo-ros-control/src/manipulation_nodes/noitom_hi5_hand_udp_python/launch/launch_quest3_ik.launch'
HANDHELD_SOURCE='/home/lab/kuavo-ros-control/src/humanoid-wheel-control/humanoid_wheel_interface_ros/src/MobileManipulatorJoyCommandNode.cpp'
HANDHELD_SHA256='b24ab958fecabe25f97764392c44d1aae413312a65605128e9ad32f01711baf8'
HANDHELD_KEYS=tuple('/mobile_manipulator_joy/'+name for name in
                    ('linear_scale_x','linear_scale_y','angular_scale_z','deadzone'))
COMMANDS=('/cmd_vel','/cmd_vel_world','/cmd_pose','/cmd_pose_world','/kuavo_arm_traj',
          '/mm/two_arm_hand_pose_cmd','/cmd_lb_torso_pose','/cmd_torso_vel','/cmd_torso_delta','/cmd_torso_pose_vr')
IDLE_PUBLISHERS={
 '/cmd_vel':{'/joy_customize_config','/mobile_manipulator_joy_command_node'},
 '/cmd_vel_world':{'/mobile_manipulator_joy_command_node'},
 '/cmd_lb_torso_pose':{'/mobile_manipulator_joy_command_node'},
 '/kuavo_arm_traj':{'/arms_ik_node','/humanoid_plan_arm_trajectory_node','/autostart_arm_trajectory_bezier_demo'},
}


def validate_vendor_profile(xml, parameters):
    root=ET.fromstring(xml)
    for name,expected in zip(PROFILE_KEYS,PROFILE):
        matches=[p.get('value') for p in root.iter('param') if p.get('name')==name]
        if len(matches)!=1 or float(matches[0])!=expected:
            raise ValueError('manufacturer VR launch profile changed: '+name)
        if name in parameters and (type(parameters[name]) not in (int,float) or parameters[name]!=expected):
            raise ValueError('active ROS speed differs from manufacturer profile: '+name)
    return PROFILE


def validate_handheld_profile(source, parameters):
    # Match the full audited source, not a loose regex that could miss overrides.
    if hashlib.sha256(source.encode()).hexdigest()!=HANDHELD_SHA256:
        raise ValueError('manufacturer handheld source changed; speed profile requires review')
    expected=base_speed_limits('vendor_handheld')+(.05,)
    for name,value in zip(HANDHELD_KEYS,expected):
        if name in parameters and (type(parameters[name]) not in (int,float) or parameters[name]!=value):
            raise ValueError('active ROS handheld profile differs: '+name)
    return expected[:3]


class BaseRuntimeGuard:
    def __init__(self, own_node, *, clock=time.monotonic_ns, shared_arms=False):
        if type(shared_arms) is not bool:
            raise ValueError('shared_arms must be bool')
        self.shared_arms=shared_arms
        self.own_node=own_node; self.clock=clock
        self.lock=threading.RLock()
        self.error=None; self.graph_ns=None; self.feedback=None
        self.control=None; self.velocity=None; self.mpc=False
        self._odom_stamp=None; self._ros_time=None
        self.last_odometry_timing=None
        self.downstream_ns=None
        self.downstream_velocity=None

    def fail(self, reason):
        with self.lock: self.error=self.error or str(reason)

    def graph(self, publishers, subscribers, *, version, mpc, simulated):
        with self.lock:
            if type(version) is not int or version!=63 or mpc is not True or simulated is not False:
                raise ValueError('requires physical v63 manufacturer MPC, no automatic reconfiguration')
            for topic in COMMANDS:
                allowed=IDLE_PUBLISHERS.get(topic,set()) | ({self.own_node} if topic=='/cmd_vel'
                    or (self.shared_arms and topic=='/kuavo_arm_traj') else set())
                if self.shared_arms and topic=='/cmd_vel':
                    from teleop.robots.kuavo5_live_guard import KNOWN_PUBLISHERS
                    allowed=allowed | KNOWN_PUBLISHERS[topic]
                if set(publishers.get(topic,[]))-allowed:
                    raise ValueError('competing command publisher on '+topic)
            expected={'/odom':{'/ukf_odom'}, '/enable_control_state':{'/nodelet_manager'},
                      '/enable_vel_control_state':{'/nodelet_manager'},
                      '/move_base/base_cmd_vel':{'/nodelet_manager'}}
            for topic,nodes in expected.items():
                if set(publishers.get(topic,[]))!=nodes:
                    raise ValueError('unexpected state/command owner: '+topic)
            consumers=set(subscribers.get('/cmd_vel',[]))
            required={'/nodelet_manager','/mobile_manipulator_sqp_mpc_node'}
            if not required<=consumers<=required|{'/joy_customize_config',self.own_node}:
                raise ValueError('unreviewed /cmd_vel consumers')
            downstream=set(subscribers.get('/move_base/base_cmd_vel',[]))
            if not {'/leju_node','/nodelet_manager'}<=downstream<= {'/leju_node','/nodelet_manager',self.own_node}:
                raise ValueError('unreviewed downstream base consumers')
            self.graph_ns=self.clock(); self.mpc=True

    def mode(self, topic, value, caller):
        with self.lock:
            if caller!='/nodelet_manager' or type(value) is not bool:
                self.fail('untrusted mode feedback'); return
            if topic=='/enable_control_state': self.control=value
            elif topic=='/enable_vel_control_state': self.velocity=value
            else: self.fail('unknown mode feedback')

    def odometry(self, *, stamp_ns, ros_now_ns, velocity, caller, frame_id, child_frame_id):
        with self.lock:
            if caller!='/ukf_odom' or frame_id!='odom' or child_frame_id!='base_footprint':
                raise ValueError('unreviewed odometry source/frame')
            if type(stamp_ns) is not int or stamp_ns<=0 or type(ros_now_ns) is not int:
                raise ValueError('invalid odometry timestamp')
            age_ns=ros_now_ns-stamp_ns
            conservative_age_ns=age_ns+ODOM_CLOCK_SKEW_BUDGET_NS
            self.last_odometry_timing=dict(raw_age_ms=age_ns/1e6,
                conservative_age_ms=conservative_age_ns/1e6,
                clock_skew_budget_ms=ODOM_CLOCK_SKEW_BUDGET_NS/1e6,
                max_feedback_age_ms=ODOM_FEEDBACK_MAX_AGE_NS/1e6,
                source_stamp_ns=stamp_ns,ros_now_ns=ros_now_ns,
                accepted=False)
            if not 0 <= conservative_age_ns < ODOM_FEEDBACK_MAX_AGE_NS:
                raise ValueError('odometry source stale or future: age_ms={:.6f}; '
                    'conservative_age_ms={:.6f}; clock_skew_budget_ms=20; '
                    'allowed_age_ms=[-20,180); source_stamp_ns={}; ros_now_ns={}'.format(
                        age_ns/1e6,conservative_age_ns/1e6,stamp_ns,ros_now_ns))
            if self._ros_time is not None and ros_now_ns<self._ros_time:
                raise ValueError('ROS clock moved backwards')
            if self._odom_stamp is not None and stamp_ns<=self._odom_stamp:
                raise ValueError('odometry source repeated or reordered')
            # Include queue age plus clock uncertainty; do not refresh old
            # source data to receipt time, including a slightly future stamp.
            self.feedback=BaseFeedback(self.clock()-conservative_age_ns,*velocity)
            self._odom_stamp=stamp_ns; self._ros_time=ros_now_ns
            self.last_odometry_timing['accepted']=True

    def command(self, topic, caller, values=None):
        with self.lock:
            if topic=='/cmd_vel' and caller==self.own_node: return
            if self.shared_arms and topic=='/kuavo_arm_traj' and caller==self.own_node: return
            if self.shared_arms and topic=='/cmd_vel' and caller=='/humanoid_joy_control_auto_gait_with_vel' \
                    and values is not None and len(values)==6 \
                    and all(type(v) in (float,int) and math.isfinite(v) and v==0. for v in values):
                return
            if topic in ('/cmd_vel','/cmd_vel_world') and caller in IDLE_PUBLISHERS[topic] \
                    and values is not None and len(values)==6 \
                    and all(type(v) in (float,int) and math.isfinite(v) and v==0. for v in values):
                return
            self.fail('competing command observed: '+topic+' caller='+str(caller))

    def downstream(self, caller, values):
        with self.lock:
            if caller!='/nodelet_manager' or len(values)!=6 or any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
                self.fail('invalid downstream controller output'); return
            self.downstream_ns=self.clock()
            self.downstream_velocity=tuple(values)

    def snapshot(self):
        with self.lock:
            now=self.clock()
            error=self.error or ''
            if self.downstream_ns is None or not 0 <= now-self.downstream_ns<200_000_000:
                error=error or 'manufacturer base output heartbeat unavailable'
            downstream_quiet=(not error and self.downstream_velocity is not None
                              and all(abs(v)<=.01 for v in self.downstream_velocity))
            return BaseHealth(self.graph_ns if self.graph_ns is not None else -1,
                              self.control,self.velocity,self.mpc,error,downstream_quiet,
                              self.downstream_ns),self.feedback


def _caller(message):
    return (getattr(message,'_connection_header',None) or {}).get('callerid')


def _twist_values(msg):
    return (msg.linear.x,msg.linear.y,msg.linear.z,msg.angular.x,msg.angular.y,msg.angular.z)


class BaseRosBinding:
    def __init__(self, *, motion_enabled, ros=None, master=None, clock=time.monotonic_ns,speed_profile='vendor_vr',
                 shared_arms=False, master_factory=None):
        if motion_enabled is not True:
            raise ValueError('explicit motion permission required before ROS initialization')
        self.limits=base_speed_limits(speed_profile)
        self.speed_profile=speed_profile
        if ros is None:
            import rospy
            import rosgraph
            import uuid
            ros=rospy
            # Unique name cannot evict an existing ROS node with the same name.
            ros.init_node('teleop_kuavo5_base_'+uuid.uuid4().hex[:10],disable_signals=True,disable_rosout=True)
            master=rosgraph.Master(ros.get_name())
        self.ros=ros; self.master=master; self.clock=clock
        # The unified arm guard also polls the supplied Master. rosgraph.Master
        # owns an xmlrpc ServerProxy whose HTTP connection is not thread safe.
        # Keep this monitor's graph AND parameter requests on a private client;
        # a lock only around this binding would not protect the arm guard.
        # Construct it lazily so fake-interface tests never import/discover ROS.
        self._master_factory=master_factory
        self._graph_master=None
        self._graph_lock=threading.Lock()
        self.guard=BaseRuntimeGuard(ros.get_name(),clock=clock,shared_arms=shared_arms)
        self.handles=[]; self.publisher=None; self.stop=threading.Event(); self.thread=None
        self.input_deadline_ns=None

    def refresh_graph(self):
        with self._graph_lock:
            if self._graph_master is None:
                factory=self._master_factory
                if factory is None:
                    from rosgraph import Master
                    factory=Master
                self._graph_master=factory(self.ros.get_name(),master_uri=self.master.master_uri)
            master=self._graph_master
            pubs,subs,_=master.getSystemState()
            keys=HANDHELD_KEYS if self.speed_profile=='vendor_handheld' else PROFILE_KEYS
            parameters={key:master.getParam(key) for key in keys if master.hasParam(key)}
            if self.speed_profile=='vendor_handheld':
                validate_handheld_profile(Path(HANDHELD_SOURCE).read_text(),parameters)
            else:
                validate_vendor_profile(Path(LAUNCH).read_text(),parameters)
            simulated=master.getParam('/use_sim_time') if master.hasParam('/use_sim_time') else False
            self.guard.graph(dict(pubs),dict(subs),version=master.getParam('/robot_version'),
                mpc=master.getParam('/use_external_mpc'),simulated=simulated)

    def start(self):
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        from std_msgs.msg import Bool
        self.Twist=Twist
        self.refresh_graph()
        def safe(function):
            def receive(message):
                try: function(message)
                except Exception as exc: self.guard.fail(str(exc))
            return receive
        def subscribe(topic, cls, function):
            self.handles.append(self.ros.Subscriber(topic,cls,safe(function),queue_size=1,tcp_nodelay=True))
        subscribe('/odom',Odometry,lambda msg:self.guard.odometry(stamp_ns=msg.header.stamp.to_nsec(),
            ros_now_ns=self.ros.Time.now().to_nsec(),velocity=(msg.twist.twist.linear.x,msg.twist.twist.linear.y,msg.twist.twist.angular.z),
            caller=_caller(msg),frame_id=msg.header.frame_id,child_frame_id=msg.child_frame_id))
        for topic in ('/enable_control_state','/enable_vel_control_state'):
            subscribe(topic,Bool,lambda msg,t=topic:self.guard.mode(t,msg.data,_caller(msg)))
        for topic in COMMANDS:
            cls=Twist if topic in ('/cmd_vel','/cmd_vel_world') else self.ros.AnyMsg
            subscribe(topic,cls,lambda msg,t=topic:self.guard.command(t,_caller(msg),
                _twist_values(msg) if t in ('/cmd_vel','/cmd_vel_world') else None))
        subscribe('/move_base/base_cmd_vel',Twist,lambda msg:self.guard.downstream(_caller(msg),_twist_values(msg)))
        def monitor():
            while not self.stop.wait(.1):
                try: self.refresh_graph()
                except Exception as exc: self.guard.fail(str(exc))
        self.thread=threading.Thread(target=monitor,name='base-graph-readonly',daemon=True)
        self.thread.start()

    def create_publisher(self):
        # Called only after state/ownership preflight; initialization never sends.
        if self.publisher is None:
            self.publisher=self.ros.Publisher('/cmd_vel',self.Twist,queue_size=1,latch=False,tcp_nodelay=True)

    def publish(self, velocity):
        if len(velocity)!=3 or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>bound+1e-12 for v,bound in zip(velocity,self.limits)):
            raise ValueError('base command outside exact vendor profile')
        with self.guard.lock:
            if self.publisher is None: raise RuntimeError('publisher not prepared')
            if any(velocity) and self.publisher.get_num_connections()<2:
                raise ValueError('base subscriber connections unavailable')
            msg=self.Twist()
            msg.linear.x,msg.linear.y,msg.angular.z=velocity
            if any(velocity):
                health,feedback=self.guard.snapshot(); now=self.clock()
                if health.error or not 0<=now-health.observed_ns<500_000_000 or not all(v is True for v in (health.control_enabled,health.velocity_enabled,health.mpc_enabled)) \
                        or feedback is None or not 0<=now-feedback.observed_ns<200_000_000:
                    raise ValueError('authority or feedback lost before publication')
                if self.input_deadline_ns is None: raise ValueError('input lease unavailable')
                if now>=self.input_deadline_ns: raise InputDeadlineExpired(now)
            self.publisher.publish(msg)

    def close(self):
        # Session owns zero-command/feedback handling before resource cleanup.
        self.stop.set()
        if self.publisher is not None: self.publisher.unregister()
        for handle in self.handles: handle.unregister()
        if self.thread is not None: self.thread.join(timeout=.2)
