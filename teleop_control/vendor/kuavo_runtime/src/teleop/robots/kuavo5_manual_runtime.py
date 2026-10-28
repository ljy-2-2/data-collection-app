"""Operator-started manual worker. Never imported by read-only camera service.

The fixed privileged entry and network owner ledger authorize this process.
All ROS imports and actuator operations are below run_manual().
"""
import hashlib
import json
import math
from pathlib import Path
import queue
import secrets
import sys
import threading
import time

from .kuavo5_manual_control import (PROTOCOL, ManualController, SENSOR_NAMES,
                                   ARMS, BODY, HEAD, BASE, BY_NAME)

URDF_SHA256 = 'fecef6f980806af7d2c4b1abe56d3fe1d0dbfbd69b3475303eb158bcc06c85dc'


class RosManualIO:
    def __init__(self, bundle):
        self.bundle = bundle
        self.handles = []
        self.owner = None
        self.pubs = {}
        self.lock = threading.Lock()
        self.sensor = self.odom = None
        self.fault = None
        self.mode = None
        self.mode_at = 0.
        self.previous_mode = None
        self.attempted = False
        self.outcome_unknown = False
        self.configured = False
        self.grip_targets = {}
        self.grip_last = time.monotonic()
        self.graph_at = 0.
        self.graph_error = None
        self.cancelled = lambda: False
        self.cleaning = False
        self.graph_stop = threading.Event()
        self.sensor_timing = None
        self.sensor_unchanged = None

    def start(self):
        import rospy
        import rosgraph
        from kuavo_msgs.msg import sensorsData, robotHeadMotionData
        from sensor_msgs.msg import JointState
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        from std_msgs.msg import Int8
        from kuavo_msgs.srv import changeTorsoCtrlMode, changeArmCtrlMode, changeLbQuickModeSrv
        from std_srvs.srv import SetBool
        self.ros = rospy
        self.JointState, self.Head, self.Twist = JointState, robotHeadMotionData, Twist
        rospy.init_node('galaxis_manual', anonymous=True, disable_signals=True, disable_rosout=True)
        if str(rospy.get_param('/robot_version')) != '63':
            raise ValueError('手动控制仅适用于已核验的 KUAVO 5-W v63')
        if rospy.get_param('/hardware/is_ready', False) is not True:
            raise ValueError('厂家主控尚未完成初始化，请先检查设备')
        if hashlib.sha256(Path(rospy.get_param('/urdfFile')).read_bytes()).hexdigest() != URDF_SHA256:
            raise ValueError('机器人模型与手动关节限位不一致')
        self.master = rosgraph.Master(rospy.get_name())
        self.handles = [rospy.Subscriber('/sensors_data_raw', sensorsData, self.on_sensor, queue_size=1, tcp_nodelay=True),
                        rospy.Subscriber('/odom', Odometry, self.on_odom, queue_size=1, tcp_nodelay=True),
                        rospy.Subscriber('/mobile_manipulator/lb_mpc_control_mode', Int8, self.on_mode, queue_size=1)]
        deadline = time.monotonic()+3.; quiet_since = None
        while time.monotonic() < deadline:
            try:
                self.feedback()
                if self.cancelled(): raise RuntimeError('手动控制已取消')
                if self.mode not in (1, 2, 3, 4): raise ValueError('MPC 模式未就绪')
                quiet = max(abs(v) for v in self.sensor[2]) < .02 and max(abs(v) for v in self.odom[1]) < .02
                quiet_since = (quiet_since or time.monotonic()) if quiet else None
                if quiet_since is not None and time.monotonic()-quiet_since >= .5: break
            except ValueError: quiet_since = None
            time.sleep(.02)
        self.feedback()
        if self.mode not in (1, 2, 3, 4): raise ValueError('MPC 模式未就绪')
        if quiet_since is None or time.monotonic()-quiet_since < .5:
            raise ValueError('请等待关节和底盘静止后进入手动控制')
        self.previous_mode = self.mode
        self.check_graph()
        def watch_graph():
            while not self.graph_stop.wait(.3):
                try: self.check_graph()
                except Exception as exc:
                    self.graph_error = str(exc); return
        threading.Thread(target=watch_graph,daemon=True,name='manual-graph').start()
        self.calls = {}
        for name, cls in (('/mobile_manipulator_mpc_control', changeTorsoCtrlMode),
                          ('/wheel_arm_change_arm_ctrl_mode', changeArmCtrlMode),
                          ('/change_arm_ctrl_mode', changeArmCtrlMode),
                          ('/enable_lb_arm_quick_mode', changeLbQuickModeSrv),
                          ('/enable_arm_traj_interpolator', SetBool)):
            rospy.wait_for_service(name, timeout=2.)
            self.calls[name] = rospy.ServiceProxy(name, cls, persistent=False)
        for name, cls in (('/kuavo_arm_traj', JointState), ('/lb_leg_traj', JointState),
                          ('/cmd_head_delta', robotHeadMotionData), ('/cmd_head_vel', robotHeadMotionData),
                          ('/cmd_vel', Twist)):
            self.pubs[name] = rospy.Publisher(name, cls, queue_size=1, latch=False, tcp_nodelay=True)
        deadline = time.monotonic()+2.
        while time.monotonic() < deadline and any(p.get_num_connections() < 1 for p in self.pubs.values()):
            time.sleep(.02)
        if any(p.get_num_connections() < 1 for p in self.pubs.values()):
            missing = [n for n,p in self.pubs.items() if p.get_num_connections() < 1]
            raise ValueError('厂家主控缺少手动接口订阅：'+', '.join(missing))
        from teleop.control.kuavo5_gripper_calibration import load_runtime_config
        from teleop.robots.kuavo5_gripper_owner import OwnerGripperSession
        settings = load_runtime_config(self.bundle['gripper_config'])
        self.owner = OwnerGripperSession(settings.owner_directory, settings.profiles,
            settings.occupied_ids, motor_records=settings.document['motors'])
        self.owner.start()
        # Seed current measured pose before selecting MPC joint control. Do not
        # enable fast/direct arm tracking or replace manufacturer calibration.
        self.hold()
        self.call('/enable_lb_arm_quick_mode', quickMode=0)
        self.call('/enable_arm_traj_interpolator', data=False)
        self.call('/change_arm_ctrl_mode', control_mode=0)
        self.call('/wheel_arm_change_arm_ctrl_mode', control_mode=2)
        self.call('/mobile_manipulator_mpc_control', control_mode=3)
        deadline = time.monotonic()+2.
        while time.monotonic() < deadline and self.mode != 3:
            self.feedback(); time.sleep(.02)
        if self.mode != 3: raise ValueError('MPC 联合控制模式回读未确认')
        self.configured = True

    def call(self, name, **fields):
        # No timeout retries: an ambiguous setter keeps ownership blocked.
        self.feedback()
        from teleop.robots.kuavo5_live_ros_binding import _call_bounded
        if self.cancelled() and not self.cleaning: raise RuntimeError('手动控制已取消')
        self.attempted = True
        if name == '/mobile_manipulator_mpc_control':
            result = queue.Queue(maxsize=1)
            def invoke():
                try: result.put(self.calls[name](**fields))
                except Exception as exc: result.put(exc)
            threading.Thread(target=invoke, daemon=True).start()
            try: reply = result.get(timeout=.25)
            except queue.Empty:
                self.outcome_unknown = True
                raise RuntimeError('MPC 模式请求结果未知')
            if isinstance(reply, Exception) or getattr(reply,'result',False) is not True:
                self.outcome_unknown = True
                raise RuntimeError('MPC 模式切换未确认')
        else:
            try: _call_bounded(lambda n,f:self.calls[n](**f), name, fields)
            except Exception:
                self.outcome_unknown = True
                raise

    def on_sensor(self, msg):
        try:
            q, v = list(msg.joint_data.joint_q), list(msg.joint_data.joint_v)
            stamp = msg.sensor_time.secs*1000000000 + msg.sensor_time.nsecs
            header = msg.header.stamp.to_nsec()
            wall, mono = self.ros.Time.now().to_nsec(), time.monotonic_ns()
            if not 0 <= wall-header <= 50000000:
                raise ValueError('关节 ROS 采样时间过期或超前')
            if len(q) != 20 or len(v) != 20 or not all(math.isfinite(x) for x in q+v):
                raise ValueError('关节反馈格式异常')
            with self.lock:
                if self.sensor is not None and stamp <= self.sensor[3]:
                    # Duplicate frames must not refresh freshness.
                    if stamp < self.sensor[3]: self.fault = '关节反馈时间倒退'
                    return
                if self.sensor_timing is not None:
                    previous_wall,previous_mono,previous_header = self.sensor_timing
                    if (header <= previous_header or abs((wall-previous_wall)-(mono-previous_mono)) > 50000000
                            or abs((stamp-self.sensor[3])-(header-previous_header)) > 50000000):
                        raise ValueError('关节采样时间不连续')
                sample = tuple(q+v)
                if self.sensor_unchanged is not None and self.sensor_unchanged[0] == sample:
                    if mono-self.sensor_unchanged[1] > 500000000 and max(abs(x) for x in v) > .03:
                        raise ValueError('关节非零速度反馈冻结')
                else: self.sensor_unchanged = (sample,mono)
                self.sensor_timing = (wall,mono,header)
                self.sensor = (time.monotonic(), dict(zip(SENSOR_NAMES,q)), v, stamp)
        except Exception as exc:
            self.fault = str(exc)

    def on_odom(self, msg):
        try:
            v = msg.twist.twist
            values = (v.linear.x, v.linear.y, v.angular.z)
            all_v = (v.linear.x,v.linear.y,v.linear.z,v.angular.x,v.angular.y,v.angular.z)
            stamp = msg.header.stamp.to_nsec()
            age = self.ros.Time.now().to_nsec()-stamp
            if not all(math.isfinite(x) for x in all_v) or not -10000000 <= age <= 50000000:
                raise ValueError('底盘反馈格式或采样时间异常')
            if max(abs(v.linear.z),abs(v.angular.x),abs(v.angular.y)) > .02:
                raise ValueError('底盘存在非平面运动')
            with self.lock:
                if self.odom is None or stamp > self.odom[2]: self.odom = (time.monotonic(),values,stamp)
                elif stamp < self.odom[2]: self.fault = '底盘反馈时间倒退'
        except Exception as exc: self.fault = str(exc)

    def on_mode(self, msg):
        self.mode, self.mode_at = int(msg.data), time.monotonic()

    def feedback(self):
        with self.lock:
            if self.fault: raise ValueError(self.fault)
            now = time.monotonic()
            if self.sensor is None or now-self.sensor[0] > .15: raise ValueError('关节实测反馈过期')
            if self.odom is None or now-self.odom[0] > .15: raise ValueError('底盘实测反馈过期')
            values = dict(self.sensor[1])
        if self.graph_error: raise ValueError(self.graph_error)
        if self.graph_at and time.monotonic()-self.graph_at > 1.:
            raise ValueError('控制话题所有者检查过期')
        if self.configured and (self.mode != 3 or time.monotonic()-self.mode_at > .2):
            raise ValueError('MPC 模式已变化或反馈过期')
        for n,q in values.items():
            spec = BY_NAME[n]
            if not spec['min_deg']-.5 <= math.degrees(q) <= spec['max_deg']+.5:
                raise ValueError('实测关节超出手动控制范围：'+n)
        return values

    def check_graph(self):
        from teleop.robots.kuavo5_live_guard import KNOWN_PUBLISHERS, COMMAND_TOPICS
        publishers, _, _ = self.master.getSystemState()
        pubs = dict(publishers)
        for topic in (*COMMAND_TOPICS, '/cmd_head_delta', '/cmd_head_vel', '/robot_head_motion_data'):
            allowed = KNOWN_PUBLISHERS.get(topic,set()) | {self.ros.get_name()}
            extra = set(pubs.get(topic,[])) - allowed
            if extra: raise ValueError('其他控制程序正在发布：'+topic+' '+','.join(sorted(extra)))
        if pubs.get('/sensors_data_raw') != ['/nodelet_manager'] or pubs.get('/odom') != ['/ukf_odom']:
            raise ValueError('实测反馈发布者与核验配置不一致')
        self.graph_at = time.monotonic()

    def publish(self, topic, msg):
        if self.cancelled() and not self.cleaning: raise RuntimeError('手动控制已取消')
        self.feedback()
        if self.pubs[topic].get_num_connections() < 1: raise RuntimeError('控制接口已断开：'+topic)
        self.attempted = True
        self.pubs[topic].publish(msg)

    def joints(self, names, targets):
        msg = self.JointState()
        msg.header.stamp = self.ros.Time.now()
        msg.name = list(names)
        msg.position = [targets[n] for n in names]
        self.publish('/kuavo_arm_traj' if names == ARMS else '/lb_leg_traj', msg)

    def head_delta(self, name, delta):
        msg = self.Head(); msg.joint_data = [delta if n == name else 0. for n in HEAD]
        self.publish('/cmd_head_delta', msg)

    def publish_zero(self, topic, msg):
        # This private path is called only with locally constructed zero velocity,
        # never a client payload or a stale position. Loss of feedback must not
        # prevent a best-effort zero; cleanup still needs fresh observations.
        if self.cancelled() and not self.cleaning: raise RuntimeError('手动控制已取消')
        if self.pubs[topic].get_num_connections() < 1: raise RuntimeError('停止接口已断开：'+topic)
        self.attempted = True
        self.pubs[topic].publish(msg)

    def base(self, values):
        msg = self.Twist(); msg.linear.x, msg.linear.y, msg.angular.z = values
        if tuple(values) == BASE['stop']: self.publish_zero('/cmd_vel',msg)
        else: self.publish('/cmd_vel', msg)

    def gripper_feedback(self, side):
        p = self.owner.driver.profiles[side]
        f = self.owner.driver.feedback(side)
        if f is None or not 0 <= time.monotonic_ns()-f.received_ns < p.feedback_timeout_ms*1000000:
            raise ValueError(side+' 夹爪反馈过期')
        if f.state not in (0,1) or max(f.mos_c,f.rotor_c) >= p.max_temperature_c:
            raise ValueError(side+' 夹爪故障或温度过高')
        if not min(p.open_rad,p.closed_rad) <= f.position_rad <= max(p.open_rad,p.closed_rad):
            raise ValueError(side+' 夹爪超出已标定行程')
        return p,f

    def gripper(self, side, closed):
        p,f = self.gripper_feedback(side)
        self.attempted = True
        if f.state == 0 and not self.owner.driver.enable(side,f.position_rad,deadline_ns=time.monotonic_ns()+50000000):
            raise ValueError('夹爪使能请求未接受，请检查设备')
        self.grip_targets[side] = dict(goal=p.closed_rad if closed else p.open_rad,
            q=f.position_rad,v=0.,enabling=f.state==0,started=time.monotonic())

    def tick_grippers(self):
        from teleop.control.kuavo5_gripper_teleop import ramp
        now = time.monotonic(); dt = min(.02,now-self.grip_last); self.grip_last = now
        for side,state in self.grip_targets.items():
            p,f = self.gripper_feedback(side)
            if state['enabling']:
                if f.state == 1: state['enabling'] = False
                elif now-state['started'] > .5: raise ValueError('夹爪使能回读未确认')
                else: continue
            if f.state != 1: raise ValueError('夹爪运动期间失去使能反馈')
            q,v = ramp(state['q'],state['v'],state['goal'],dt,min(p.speed_rad_s,.5),
                       min(p.accel_rad_s2,5.),*sorted((p.open_rad,p.closed_rad)))
            if self.owner.driver.position(side,q,deadline_ns=time.monotonic_ns()+50000000):
                state.update(q=q,v=v)


    def gripper_done(self, side):
        p,f = self.gripper_feedback(side)
        target = self.grip_targets.get(side)
        return target is not None and not target['enabling'] and abs(f.position_rad-target['goal']) < .015

    def hold(self):
        errors = []
        try: self.base(BASE['stop'])
        except Exception as exc: errors.append(str(exc))
        try:
            msg = self.Head(); msg.joint_data = [0.,0.]
            self.publish_zero('/cmd_head_vel',msg)
        except Exception as exc: errors.append(str(exc))
        for side in tuple(self.grip_targets):
            try:
                if not self.owner.driver.disable(side,deadline_ns=time.monotonic_ns()+50000000):
                    raise RuntimeError(side+' 夹爪停止请求未接受')
            except Exception as exc: errors.append(str(exc))
        self.grip_targets.clear()
        try:
            values = self.feedback()
            for names in (ARMS, BODY):
                try: self.joints(names,{n:math.degrees(values[n]) for n in names})
                except Exception as exc: errors.append(str(exc))
        except Exception as exc: errors.append(str(exc))
        if errors: raise RuntimeError('; '.join(errors))

    def close(self):
        self.cleaning = True
        errors = ['控制请求结果未知，不能自动释放控制权'] if self.outcome_unknown else []
        if self.attempted:
            try:
                self.hold()
                # Observe settled joints/base; publishing zero alone is no ACK.
                end, quiet = time.monotonic()+3., None
                while time.monotonic() < end:
                    self.feedback()
                    stopped = max(abs(v) for v in self.sensor[2]) < .02 and max(abs(v) for v in self.odom[1]) < .02
                    quiet = (quiet or time.monotonic()) if stopped else None
                    if quiet is not None and time.monotonic()-quiet >= .3: break
                    time.sleep(.01)
                else: raise RuntimeError('手动控制退出后实测保持未确认')
                self.configured = False
                self.call('/wheel_arm_change_arm_ctrl_mode',control_mode=0)
                self.call('/mobile_manipulator_mpc_control',control_mode=self.previous_mode)
            except Exception as exc: errors.append(str(exc))
        if self.owner is not None:
            try:
                result = self.owner.close()
                if not result['closed'] or result['errors'] or any(enabled and not result['disabled_acknowledged'].get(s,False) for s,enabled in result.get('possibly_enabled',{}).items()):
                    errors.append('夹爪退出确认失败：'+str(result))
            except Exception as exc: errors.append(str(exc))
        self.graph_stop.set()
        for item in [*self.handles,*self.pubs.values()]:
            try: item.unregister()
            except Exception as exc: errors.append(str(exc))
        if hasattr(self,'ros'): self.ros.signal_shutdown('manual worker closed')
        if errors: raise RuntimeError('; '.join(errors))


def run_manual(bundle):
    """Called ONLY after fixed-entry ownership, manifest and execution lock checks."""
    io = RosManualIO(bundle)
    controller = None
    error = ''; clean = False
    messages = queue.Queue(maxsize=8)
    def read_input():
        try:
            for line in sys.stdin:
                if len(line) > 4096: raise ValueError('manual packet too large')
                messages.put_nowait(json.loads(line))
        except Exception as exc:
            try: messages.put_nowait({'invalid':str(exc)})
            except queue.Full: pass
        finally:
            # EOF is an independent flag, so a full queue cannot hide disconnect.
            ended.set()
    ended = threading.Event()
    def emit(value): print(json.dumps(value,allow_nan=False),flush=True)
    try:
        io.cancelled = ended.is_set
        threading.Thread(target=read_input,daemon=True).start()
        io.start()
        controller = ManualController(io)
        emit(dict(event='ready',manual_protocol=PROTOCOL))
        challenge = None; deadline = 0.; next_challenge = 0.; next_feedback = 0.
        while not ended.is_set():
            now = time.monotonic()
            if challenge is not None and now >= deadline:
                raise TimeoutError('manual challenge expired')
            if challenge is None and now >= next_challenge:
                challenge = secrets.token_hex(16); deadline = now+.25; next_challenge = now+.10
                emit(dict(event='manual_challenge',challenge_id=challenge))
            try: packet = messages.get_nowait()
            except queue.Empty: packet = None
            if packet is not None:
                if set(packet) != {'event','challenge_id','command'} or packet.get('event') != 'manual_input':
                    raise ValueError('invalid manual input envelope')
                if packet['challenge_id'] != challenge or now >= deadline:
                    raise ValueError('manual input expired or replayed')
                challenge = None
                try:
                    controller.command(packet['command'])
                    emit(dict(event='manual_ack',ok=True))
                except ValueError as exc:
                    emit(dict(event='manual_ack',ok=False,error=str(exc)))
            controller.tick()
            if now >= next_feedback:
                next_feedback = now+.1
                grips = {}
                for side in ('left','right'):
                    p,f = io.gripper_feedback(side)
                    grips[side] = dict(position_rad=f.position_rad,closure_fraction=max(0.,min(1.,(f.position_rad-p.open_rad)/(p.closed_rad-p.open_rad))))
                emit(dict(event='manual_feedback',joints=io.feedback(),grippers=grips,busy=controller.pending is not None))
            time.sleep(.01)
    except Exception as exc:
        error = type(exc).__name__+': '+str(exc)
    finally:
        try: io.close(); clean = True
        except Exception as exc: error += '; cleanup: '+str(exc)
    report = dict(event='closed',manual_protocol=PROTOCOL,exit_code=0 if clean else 1,
                  cleanup_confirmed=clean,error=error,physical_stop_verified=False)
    emit(report)
    return report['exit_code']
