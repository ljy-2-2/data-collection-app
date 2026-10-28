"""Fixed KUAVO 5-W manual protocol. Import is inert; tests inject all I/O.

Small measured-relative steps; commands are never accumulated or retried.
A robot-issued one-use challenge bounds all network input. Base movement also
requires a current UI press, independently of the connection heartbeat.
"""
import math
import time

PROTOCOL = 'kuavo_manual_v1'
JOINTS = [{'name': 'zarm_l1_joint', 'label': '肩部俯仰', 'group': 'left', 'min_deg': -180.0, 'max_deg': 90.0}, {'name': 'zarm_l2_joint', 'label': '肩部外展', 'group': 'left', 'min_deg': -20.0, 'max_deg': 200.0}, {'name': 'zarm_l3_joint', 'label': '肩部旋转', 'group': 'left', 'min_deg': -85.0, 'max_deg': 85.0}, {'name': 'zarm_l4_joint', 'label': '肘部屈伸', 'group': 'left', 'min_deg': -150.0, 'max_deg': 0.0}, {'name': 'zarm_l5_joint', 'label': '腕部旋转', 'group': 'left', 'min_deg': -90.0, 'max_deg': 90.0}, {'name': 'zarm_l6_joint', 'label': '腕部俯仰', 'group': 'left', 'min_deg': -75.0, 'max_deg': 40.0}, {'name': 'zarm_l7_joint', 'label': '腕部偏转', 'group': 'left', 'min_deg': -40.0, 'max_deg': 40.0}, {'name': 'zarm_r1_joint', 'label': '肩部俯仰', 'group': 'right', 'min_deg': -180.0, 'max_deg': 90.0}, {'name': 'zarm_r2_joint', 'label': '肩部外展', 'group': 'right', 'min_deg': -200.0, 'max_deg': 20.0}, {'name': 'zarm_r3_joint', 'label': '肩部旋转', 'group': 'right', 'min_deg': -85.0, 'max_deg': 85.0}, {'name': 'zarm_r4_joint', 'label': '肘部屈伸', 'group': 'right', 'min_deg': -150.0, 'max_deg': 0.0}, {'name': 'zarm_r5_joint', 'label': '腕部旋转', 'group': 'right', 'min_deg': -90.0, 'max_deg': 90.0}, {'name': 'zarm_r6_joint', 'label': '腕部俯仰', 'group': 'right', 'min_deg': -40.0, 'max_deg': 75.0}, {'name': 'zarm_r7_joint', 'label': '腕部偏转', 'group': 'right', 'min_deg': -40.0, 'max_deg': 40.0}, {'name': 'knee_joint', 'label': '下躯干关节', 'group': 'body', 'min_deg': 0.0, 'max_deg': 83.0}, {'name': 'leg_joint', 'label': '上躯干关节', 'group': 'body', 'min_deg': -161.0, 'max_deg': 0.0}, {'name': 'waist_pitch_joint', 'label': '腰部俯仰', 'group': 'body', 'min_deg': -9.0, 'max_deg': 171.0}, {'name': 'waist_yaw_joint', 'label': '腰部旋转', 'group': 'body', 'min_deg': -180.0, 'max_deg': 180.0}, {'name': 'zhead_1_joint', 'label': '头部旋转', 'group': 'head', 'min_deg': -88, 'max_deg': 88}, {'name': 'zhead_2_joint', 'label': '头部俯仰', 'group': 'head', 'min_deg': -25, 'max_deg': 25}]
NAMES = tuple(j['name'] for j in JOINTS)
BY_NAME = {j['name']: j for j in JOINTS}
BODY = tuple(j['name'] for j in JOINTS if j['group'] == 'body')
ARMS = tuple(j['name'] for j in JOINTS if j['group'] in ('left', 'right'))
HEAD = tuple(j['name'] for j in JOINTS if j['group'] == 'head')
SENSOR_NAMES = BODY + ARMS + HEAD
BASE = {'forward': (.1, 0., 0.), 'backward': (-.1, 0., 0.),
        'left': (0., .1, 0.), 'right': (0., -.1, 0.),
        'turn_left': (0., 0., .2), 'turn_right': (0., 0., -.2),
        'stop': (0., 0., 0.)}


def validate_command(value):
    if not isinstance(value, dict):
        raise ValueError('manual command must be an object')
    action = value.get('action')
    fields = {'heartbeat': {'action'}, 'stop': {'action'},
              'joint': {'action', 'joint', 'delta_deg'},
              'gripper': {'action', 'side', 'closed'},
              'base': {'action', 'direction'}}
    if not isinstance(action,str) or action not in fields or set(value) != fields[action]:
        raise ValueError('invalid manual command fields')
    if action == 'joint':
        delta = value['delta_deg']
        if (not isinstance(value['joint'],str) or value['joint'] not in BY_NAME or type(delta) not in (int, float)
                or not math.isfinite(delta) or not 0 < abs(delta) <= 1.):
            raise ValueError('manual joint step must be finite and within one degree')
    if action == 'gripper' and (value['side'] not in ('left', 'right') or type(value['closed']) is not bool):
        raise ValueError('invalid gripper command')
    if action == 'base' and (not isinstance(value['direction'],str) or value['direction'] not in BASE):
        raise ValueError('invalid base direction')
    return dict(value)


def joint_target(command, positions):
    command = validate_command(command)
    name = command['joint']
    spec = BY_NAME[name]
    names = ARMS if spec['group'] in ('left', 'right') else BODY if spec['group'] == 'body' else HEAD
    values = {}
    for n in names:
        q = positions[n]
        if type(q) not in (float, int) or not math.isfinite(q):
            raise ValueError('fresh finite complete feedback required')
        values[n] = math.degrees(q)
    values[name] += command['delta_deg']
    # Validate all held joints too: an out-of-bounds observation is no hold seed.
    for n, q in values.items():
        s = BY_NAME[n]
        if not s['min_deg'] - 1e-5 <= q <= s['max_deg'] + 1e-5:
            raise ValueError(s['label'] + '超出关节限位')
    return names, values


class ManualController:
    """Motion policy independent of ROS; one active command and leased base."""
    def __init__(self, io, *, clock=time.monotonic):
        self.io, self.clock = io, clock
        self.base_until = 0.
        self.base_direction = 'stop'
        self.pending = None
        self.last_input = clock()
        self.closed = False

    def command(self, command):
        command = validate_command(command)
        now = self.clock()
        self.last_input = now
        action = command['action']
        if action == 'heartbeat':
            return
        if action == 'stop':
            self.io.hold()
            self.pending = None
            self.base_until = 0.
            self.base_direction = 'stop'
            return
        if action == 'base' and command['direction'] == 'stop':
            if self.base_direction != 'stop': self.io.base(BASE['stop'])
            self.base_until = 0.
            self.base_direction = 'stop'
            return
        positions = self.io.feedback()
        if self.pending is not None:
            raise ValueError('上一动作尚未完成，请等待实测姿态到位')
        if action == 'base':
            self.base_direction = command['direction']
            self.base_until = now + .20
            self.io.base(BASE[self.base_direction])
            return
        if self.base_direction != 'stop':
            raise ValueError('请先松开底盘控制')
        if action == 'joint':
            names, target = joint_target(command, positions)
            if names == HEAD:
                self.io.head_delta(command['joint'], command['delta_deg'])
            else:
                self.io.joints(names, target)
            self.pending = ('joint', target, now, min(.25,abs(command['delta_deg'])/4.))
        elif action == 'gripper':
            self.io.gripper(command['side'], command['closed'])
            self.pending = ('gripper', command['side'], now, .015)

    def tick(self):
        now = self.clock()
        if self.closed:
            return
        # Robot-side deadline; PC wall-clock adjustments cannot extend it.
        if now - self.last_input > .5:
            raise TimeoutError('手动控制输入连接超时')
        positions = self.io.feedback()
        if self.base_direction != 'stop':
            if now >= self.base_until:
                self.io.base(BASE['stop'])
                self.base_direction = 'stop'
            else:
                self.io.base(BASE[self.base_direction])
        self.io.tick_grippers()
        if self.pending:
            kind, target, started, tolerance = self.pending
            done = (all(abs(math.degrees(positions[n])-q) < tolerance for n, q in target.items())
                    if kind == 'joint' else self.io.gripper_done(target))
            if done and now-started >= .10:
                self.pending = None
            elif now - started > 15.:
                raise TimeoutError('手动动作未到位；停止本次控制，请检查设备')

    def close(self):
        self.closed = True
        self.io.close()
