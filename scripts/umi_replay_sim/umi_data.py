"""Read the collection App's ROS2 CDR MCAP without ROS or device access."""
import math
import struct
from pathlib import Path
import numpy as np

SIDES = ('left', 'right')


class CDR:
    def __init__(self, data):
        if len(data) < 4 or data[:2] not in (b'\x00\x01', b'\x00\x00'):
            raise ValueError('Only ROS2 plain CDR is supported')
        self.data, self.pos = data, 4
        self.endian = '<' if data[1] else '>'

    def read(self, code, align):
        self.pos += (-(self.pos - 4)) % align
        value = struct.unpack_from(self.endian + code, self.data, self.pos)[0]
        self.pos += struct.calcsize(code)
        return value

    def string(self):
        n = self.read('I', 4)
        if n < 1 or self.pos + n > len(self.data):
            raise ValueError('Invalid CDR string')
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b.rstrip(b'\0').decode('utf-8')

    def header(self):
        sec, nano = self.read('i', 4), self.read('I', 4)
        return sec * 1_000_000_000 + nano, self.string()


def quat_matrix(q):
    q = np.asarray(q, float)
    n = np.linalg.norm(q)
    if not np.isfinite(q).all() or not 0.95 < n < 1.05:
        raise ValueError('Invalid quaternion')
    x, y, z, w = q / n
    return np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                     [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                     [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])


def pose(p, q):
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = quat_matrix(q), p
    if not np.isfinite(T).all():
        raise ValueError('Nonfinite pose')
    return T


def slerp(a, b, u):
    a, b = np.asarray(a, float), np.asarray(b, float)
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    dot = float(a @ b)
    if dot < 0:
        b, dot = -b, -dot
    if dot > .9995:
        q = a + u * (b-a)
        return q / np.linalg.norm(q)
    theta = math.acos(np.clip(dot, -1, 1))
    return (math.sin((1-u)*theta)*a + math.sin(u*theta)*b) / math.sin(theta)


def bracket(times, t):
    if t < times[0]-1e-8 or t > times[-1]+1e-8:
        raise ValueError('Refusing timestamp extrapolation')
    i = int(np.clip(np.searchsorted(times, t, side='right')-1, 0, len(times)-2))
    return i, float(np.clip((t-times[i])/(times[i+1]-times[i]), 0, 1))


def check_series(rows, label, gap=.1):
    if len(rows) < 2:
        raise ValueError(f'{label}: fewer than two samples')
    stamps = np.array([r[0] for r in rows], dtype=np.int64)
    d = np.diff(stamps) / 1e9
    if np.any(d <= 0) or np.max(d) > gap:
        raise ValueError(f'{label}: reversed/duplicate timestamps or gap > {gap}s')


class Recording:
    def __init__(self, path, sides=SIDES, point='controller', max_gap=.1):
        if point not in ('controller', 'hand-root'):
            raise ValueError('Unknown source control point: '+str(point))
        from mcap.reader import make_reader
        self.path = Path(path).expanduser().resolve()
        if self.path.is_dir():
            candidates = [self.path / 'signals.mcap', self.path / 'media/signals.mcap']
            self.path = next((p for p in candidates if p.is_file()), candidates[0])
        self.sides, self.point = tuple(sides), point
        self.poses = {s: [] for s in sides}
        self.joints = {s: [] for s in sides}
        self.head, self.head_frames = [], set()
        self.static, self.offsets, self.frames = {}, {}, set()
        controller_topics = {f'/pico/controller/{s}/state': s for s in sides}
        joint_names = {f'{s}_{s}_finger_joint': s for s in sides}
        # Use /joint_states once; don't combine it with duplicate per-side streams.
        with self.path.open('rb') as f:
            for schema, channel, msg in make_reader(f, validate_crcs=True).iter_messages(log_time_order=False):
                topic = channel.topic
                if topic not in controller_topics and topic not in ('/joint_states', '/tf_static', '/pico/hmd/state') and not topic.endswith('/encoder_offset'):
                    continue
                if channel.message_encoding != 'cdr' or schema is None:
                    raise ValueError(f'{topic}: expected ROS2 CDR schema')
                r = CDR(msg.data)
                if topic in controller_topics or topic == '/pico/hmd/state':
                    head = topic == '/pico/hmd/state'
                    expected = 'galaxis_pico_msgs/msg/PicoHmd' if head else 'galaxis_pico_msgs/msg/PicoController'
                    if schema.name != expected:
                        raise ValueError(f'{topic}: unexpected schema {schema.name}')
                    ns, frame = r.header()
                    r.read('Q', 8); r.read('Q', 8); r.read('I', 4); r.string()
                    values = [r.read('d', 8) for _ in range(7)]
                    pose(values[:3], values[3:])
                    row = (ns, np.array(values[:3]), np.array(values[3:]))
                    if head:
                        self.head_frames.add(frame)
                        self.head.append(row)
                    else:
                        tracked, valid = r.read('B', 1), r.read('B', 1)
                        if not tracked or not valid:
                            raise ValueError(f'{topic}: invalid tracking at {ns}')
                        self.frames.add(frame)
                        self.poses[controller_topics[topic]].append(row)
                elif topic == '/joint_states':
                    if schema.name != 'sensor_msgs/msg/JointState':
                        raise ValueError('Unexpected JointState schema')
                    ns, _ = r.header()
                    names = [r.string() for _ in range(r.read('I', 4))]
                    positions = [r.read('d', 8) for _ in range(r.read('I', 4))]
                    if len(names) != len(positions):
                        raise ValueError('JointState name/position length mismatch')
                    for name, q in zip(names, positions):
                        if name in joint_names:
                            if not math.isfinite(q):
                                raise ValueError('Invalid encoder value')
                            self.joints[joint_names[name]].append((ns, q))
                elif topic == '/tf_static':
                    for _ in range(r.read('I', 4)):
                        _, parent = r.header()
                        child = r.string()
                        v = [r.read('d', 8) for _ in range(7)]
                        T = pose(v[:3], v[3:])
                        key = (parent, child)
                        if key in self.static and not np.allclose(T, self.static[key], atol=1e-9):
                            raise ValueError('Static transform changed inside recording')
                        self.static[key] = T
                else:
                    offset = r.read('d', 8)
                    if not math.isfinite(offset) or (topic in self.offsets and abs(self.offsets[topic]-offset) > 1e-9):
                        raise ValueError('Encoder calibration changed inside recording')
                    self.offsets[topic] = offset
        if len(self.frames) != 1 or not next(iter(self.frames)):
            raise ValueError('Missing or inconsistent PICO origin frames')
        for s in sides:
            check_series(self.poses[s], s+' poses', max_gap)
            check_series(self.joints[s], s+' encoder', max_gap)
        streams = list(self.poses.values()) + list(self.joints.values())
        # HMD is also needed at the reference time: start only once all recorded
        # streams are available, rather than extrapolating an earlier head pose.
        if self.head:
            check_series(self.head, 'HMD reference', max_gap)
            streams.append(self.head)
        self.start_ns = max(rows[0][0] for rows in streams)
        end_ns = min(rows[-1][0] for rows in streams)
        self.duration = (end_ns-self.start_ns)/1e9
        if self.duration <= 0:
            raise ValueError('No shared timestamp range')
        self.pt, self.jt, self.extra = {}, {}, {}
        for s in sides:
            self.pt[s] = np.array([(r[0]-self.start_ns)/1e9 for r in self.poses[s]])
            self.jt[s] = np.array([(r[0]-self.start_ns)/1e9 for r in self.joints[s]])
            self.extra[s] = np.eye(4)
            if point == 'hand-root':
                key = (s+'_hand_root', 'pico_'+s+'_controller')
                if key not in self.static:
                    raise ValueError(f'Missing recorded static transform {key}')
                self.extra[s] = np.linalg.inv(self.static[key])
            points = np.array([self.at(s, t)[0][:3, 3] for t in self.pt[s] if 0 <= t <= self.duration])
            if len(points) > 1 and np.max(np.linalg.norm(np.diff(points, axis=0), axis=1)) > .05:
                raise ValueError(f'{s}: pose jump > 5 cm/sample; check tracking/recenter')
            for a, b in zip(self.poses[s][:-1], self.poses[s][1:]):
                d = abs(float(a[2] @ b[2])/(np.linalg.norm(a[2])*np.linalg.norm(b[2])))
                if 2*math.acos(np.clip(d, -1, 1)) > .5:
                    raise ValueError(f'{s}: orientation jump > 0.5 rad/sample')

    def at(self, side, t):
        i, u = bracket(self.pt[side], t)
        a, b = self.poses[side][i:i+2]
        T = pose(a[1]+u*(b[1]-a[1]), slerp(a[2], b[2], u)) @ self.extra[side]
        j, v = bracket(self.jt[side], t)
        c, d = self.joints[side][j:j+2]
        return T, float(c[1]+v*(d[1]-c[1]))

    def head_at(self, t):
        if not self.head or self.head_frames != self.frames:
            raise ValueError('遥操作方向模式需要同一 pico_origin 下的 /pico/hmd/state 记录')
        check_series(self.head, 'HMD reference', gap=.1)
        times = np.array([(row[0]-self.start_ns)/1e9 for row in self.head])
        i, u = bracket(times, t)
        a, b = self.head[i:i+2]
        return pose(a[1]+u*(b[1]-a[1]), slerp(a[2], b[2], u))

    def summary(self):
        return dict(file=str(self.path), point=self.point, origin=sorted(self.frames),
                    shared_duration_s=self.duration, hmd_samples=len(self.head), encoder_zero_offsets=self.offsets,
                    sides={s: dict(pose_samples=len(self.poses[s]), encoder_samples=len(self.joints[s]),
                                   encoder_min_rad=min(r[1] for r in self.joints[s]),
                                   encoder_max_rad=max(r[1] for r in self.joints[s]),
                                   first_encoder_rad=self.at(s, 0)[1],
                                   first_pose=self.at(s, 0)[0].tolist()) for s in self.sides})


def gripper_trigger(q, closed, opened):
    if not all(math.isfinite(v) for v in (q, closed, opened)) or abs(opened-closed) < .01:
        raise ValueError('UMI closed/open endpoints must be finite and distinct (>=0.01 rad)')
    opening = float(np.clip((q-closed)/(opened-closed), 0, 1))
    return 1-opening  # Tianji trigger: 0=open, 1=closed


def relative_target(first, current, anchor, C, scale=1.0):
    if not math.isfinite(scale) or not 0 < scale <= 1:
        raise ValueError("位移比例必须在 (0, 1] 内")
    T = np.eye(4)
    T[:3, 3] = anchor[:3, 3] + scale * (C @ (current[:3, 3]-first[:3, 3]))
    T[:3, :3] = C @ current[:3, :3] @ first[:3, :3].T @ C.T @ anchor[:3, :3]
    return T
