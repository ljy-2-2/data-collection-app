"""Read-only URDF forward kinematics. No robot/vendor imports or network IO."""
from pathlib import Path
import json
import math
import xml.etree.ElementTree as ET
import numpy as np

ASSETS = Path(__file__).resolve().parents[2] / 'assets/kuavo'
ARM_NAMES = tuple(f'zarm_{side}{i}_joint' for side in ('l', 'r') for i in range(1, 8))


def rotation(axis, angle):
    axis = np.asarray(axis, dtype=float)
    x, y, z = axis / (np.linalg.norm(axis) or 1.)
    c, s, t = math.cos(angle), math.sin(angle), 1-math.cos(angle)
    out = np.eye(4)
    out[:3, :3] = [[t*x*x+c,t*x*y-s*z,t*x*z+s*y],
                   [t*x*y+s*z,t*y*y+c,t*y*z-s*x],
                   [t*x*z-s*y,t*y*z+s*x,t*z*z+c]]
    return out


class KuavoModel:
    def __init__(self):
        root = ET.parse(ASSETS/'full_robot.urdf').getroot()
        self.joints = []
        for j in root.findall('joint'):
            origin = j.find('origin')
            xyz = [float(v) for v in origin.get('xyz','0 0 0').split()] if origin is not None else [0,0,0]
            rpy = [float(v) for v in origin.get('rpy','0 0 0').split()] if origin is not None else [0,0,0]
            matrix = rotation((0,0,1),rpy[2]) @ rotation((0,1,0),rpy[1]) @ rotation((1,0,0),rpy[0])
            matrix[:3,3] = xyz
            axis = j.find('axis')
            self.joints.append(dict(name=j.get('name'),type=j.get('type'),parent=j.find('parent').get('link'),
                child=j.find('child').get('link'),matrix=matrix,
                axis=[float(v) for v in axis.get('xyz').split()] if axis is not None else [1,0,0]))
        self.reference = json.loads((ASSETS/'reference.json').read_text())

    def segments(self, values=None):
        values = values or {}
        matrices = {'base_link':np.eye(4)}
        pending, segments = self.joints[:], []
        while pending:
            remaining = []
            for j in pending:
                if j['parent'] not in matrices:
                    remaining.append(j); continue
                parent = matrices[j['parent']]
                matrix = parent @ j['matrix']
                if j['type'] in ('revolute','continuous'):
                    matrix = matrix @ rotation(j['axis'],values.get(j['name'],0.))
                matrices[j['child']] = matrix
                name = j['name']
                group = 'left' if name.startswith('zarm_l') else 'right' if name.startswith('zarm_r') else 'body'
                segments.append(dict(a=parent[:3,3].tolist(),b=matrix[:3,3].tolist(),group=group))
            if len(remaining) == len(pending):raise ValueError('KUAVO URDF disconnected')
            pending = remaining
        return segments


def measured_feedback(packet):
    """Accept measured arms only, never command_rad or IK targets."""
    if not isinstance(packet,dict) or packet.get('event') not in ('control_trace','ik_trace'):return None
    names, values = packet.get('joint_order'), packet.get('feedback_rad')
    stamp = packet.get('feedback_timestamp_ns')
    if not isinstance(names,list) or not isinstance(values,list) or len(names)!=len(values):return None
    if any(not isinstance(n,str) for n in names):return None
    if len(set(names))!=len(names) or not set(ARM_NAMES).issubset(names):return None
    if type(stamp) is not int or stamp<=0:return None
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):return None
    return dict(joints={n:float(v) for n,v in zip(names,values) if n in ARM_NAMES}, timestamp_ns=stamp)


def measured_grippers(packet):
    """Read canonical motor feedback, never trigger/target or raw CAN coordinates."""
    if not isinstance(packet, dict) or packet.get('event') != 'status':return {}
    grippers = packet.get('grippers')
    if not isinstance(grippers, dict) or grippers.get('mode') != 'injected_driver':return {}
    motors = grippers.get('motors')
    if not isinstance(motors, dict):return {}
    result = {}
    for side in ('left', 'right'):
        motor = motors.get(side)
        if not isinstance(motor, dict):continue
        feedback = motor.get('feedback')
        if not isinstance(feedback, dict):continue
        stamp = feedback.get('received_ns')
        position, opened, closed, age = (feedback.get('position_rad'), motor.get('open_rad'),
                                        motor.get('closed_rad'), motor.get('feedback_age_ms'))
        if type(stamp) is not int or stamp <= 0:continue
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (position, opened, closed, age)):continue
        if opened == closed or not 0 <= age < 1000:continue
        result[side] = dict(closure_fraction=max(0., min(1., (position-opened)/(closed-opened))),
                            position_rad=position, timestamp_ns=stamp, age_s=age/1000.)
    return result
