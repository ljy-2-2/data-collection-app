#!/usr/bin/env python3
"""Offline Tianji reset-pose comparison. No device or control client is loaded."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import sys

import numpy as np
os.environ.setdefault('QSG_RHI_BACKEND', 'opengl')
os.environ.setdefault('QSG_RENDER_LOOP', 'basic')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer, QUrl, QCoreApplication, QEvent
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from qt_ui.visuals import RobotScene, transform, rotation


def frames(model, joints):
    angles = {f'Joint{i+1}_{letter}': math.radians(v)
              for side, letter in [('left', 'L'), ('right', 'R')]
              for i, v in enumerate(joints.get(side, [0.] * 7))}
    result = {model.get('root_link', 'stand'): np.eye(4)}
    pending = model['joints'][:]
    while pending:
        remaining = []
        for joint in pending:
            if joint['parent'] not in result:
                remaining.append(joint)
                continue
            value = angles.get(joint['name'], 0.)
            if joint.get('mimic'):
                m = joint['mimic']; value = angles.get(m['joint'], 0.) * float(m['multiplier']) + float(m['offset'])
            matrix = result[joint['parent']] @ transform(joint['xyz'], joint['rpy'])
            if joint['type'] in ('revolute', 'continuous'):
                matrix = matrix @ rotation(joint['axis'], value)
            result[joint['child']] = matrix
        if len(remaining) == len(pending):
            raise ValueError('Disconnected model')
        pending = remaining
    return {name: np.asarray(model['world_from_display']) @ matrix for name, matrix in result.items()}


def metrics(model, joints, side='right'):
    f = frames(model, joints)
    letter = 'L' if side == 'left' else 'R'
    # URDF longitudinal directions: upper arm +Y in Link2, forearm -Y in
    # Link4. J4->J5 also has a real 18 mm transverse offset, so that line
    # is not the forearm body's longitudinal direction.
    upper = f[f'Link2_{letter}'][:3, 1].copy()
    forearm = -f[f'Link4_{letter}'][:3, 1].copy()
    upper /= np.linalg.norm(upper); forearm /= np.linalg.norm(forearm)
    tilt = math.degrees(math.acos(np.clip(-upper[2], -1, 1)))
    elevation = math.degrees(math.asin(np.clip(forearm[2], -1, 1)))
    a, b = upper[[0, 2]], forearm[[0, 2]]
    projected = (math.degrees(math.acos(np.clip(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)), -1, 1)))
                 if min(np.linalg.norm(a), np.linalg.norm(b)) > 1e-9 else None)
    return dict(upper_from_down_deg=tilt, forearm_elevation_deg=elevation,
                side_corner_deg=projected, tcp=f[f'grasp_center_{letter}'].tolist())


class Preview(QObject):
    changed = Signal()
    def __init__(self, scene, payload, candidate, symmetric=False):
        super().__init__()
        self.scene = scene
        self.original = {side: list(payload['joints'][side]) for side in ('left', 'right')}
        self.candidate = list(candidate)
        self.proposed = list(candidate)
        self.symmetric = symmetric
        self.selected = 'candidate'
        self.source = payload.get('source', '用户提供的关节角度')
        self.reference = metrics(scene.model, self.original)
        self.value = {}
        self.select('candidate')
        self.view('side')

    @Property('QVariantMap', notify=changed)
    def state(self): return self.value

    def update(self):
        joints = dict(self.original)
        joints['right'] = self.original['right'] if self.selected == 'original' else self.candidate
        # Offline candidate only: signs verified against both nominal URDFs.
        left_candidate = [sign * value for sign, value in
                          zip((-1, 1, -1, 1, -1, 1, -1), self.candidate)]
        if self.symmetric and self.selected != 'original':
            joints['left'] = left_candidate
        self.scene.update_pose(dict(joints=joints))
        m = metrics(self.scene.model, joints)
        ref = np.asarray(self.reference['tcp']); actual = np.asarray(m['tcp'])
        position = np.linalg.norm(actual[:3, 3] - ref[:3, 3]) * 1000
        orientation = math.degrees(math.acos(np.clip((np.trace(actual[:3, :3] @ ref[:3, :3].T)-1)/2, -1, 1)))
        corner = '投影退化' if m['side_corner_deg'] is None else f"{m['side_corner_deg']:.2f}°"
        left = metrics(self.scene.model, joints, 'left')
        lt, rt = np.asarray(left['tcp']), np.asarray(m['tcp'])
        h = np.diag([1., -1., 1.])
        mirror_position = np.linalg.norm(lt[:3, 3] - h @ rt[:3, 3]) * 1000
        mirror_angle = math.degrees(math.acos(np.clip(
            (np.trace(lt[:3, :3] @ (h @ rt[:3, :3] @ h).T) - 1) / 2, -1, 1)))
        self.value = dict(selected=self.selected, source=self.source,
            symmetric=self.symmetric, leftCandidate=left_candidate,
            leftOriginal=self.original['left'],
            original=self.original['right'], candidate=self.candidate[:], current=joints['right'],
            summary=f"上臂偏离竖直向下 {m['upper_from_down_deg']:.2f}°  ·  前臂仰角 {m['forearm_elevation_deg']:.2f}°  ·  主体侧面夹角 {corner}",
            difference=f'相对原姿态：夹爪中心偏移 {position:.2f} mm，朝向变化 {orientation:.2f}°')
        if self.symmetric:
            self.value['summary'] = (f"主体侧面夹角：左 {left['side_corner_deg']:.2f}° / 右 {m['side_corner_deg']:.2f}°"
                f"  ·  两侧 TCP 镜像位置误差 {mirror_position:.3f} mm，朝向误差 {mirror_angle:.3f}°")
            self.value['difference'] = ('以机器人中间的竖直平面为对称面；右臂角度微调时，左臂模型同步镜像。'
                                        '“原始姿态”显示之前读取的双臂快照。')
        self.changed.emit()

    @Slot(str)
    def select(self, name):
        if name not in ('original', 'candidate'): return
        self.selected = name; self.update()

    @Slot()
    def restore(self):
        self.candidate = self.proposed[:]; self.selected = 'candidate'; self.update()

    @Slot()
    def rounded(self):
        self.candidate = [round(v) for v in self.original['right']]; self.selected = 'candidate'; self.update()

    @Slot(int, int)
    def adjust(self, index, delta):
        if not 0 <= index < 7: return
        joint = next(j for j in self.scene.model['joints'] if j['name'] == f'Joint{index+1}_R')
        lower, upper = [math.degrees(float(joint['limit'][key])) for key in ('lower', 'upper')]
        value = round(self.candidate[index]) + delta
        if lower <= value <= upper:
            self.candidate[index] = value; self.selected = 'candidate'; self.update()

    @Slot(str)
    def view(self, name):
        self.scene.pitch = 0.; self.scene.zoom = 1.8; self.scene.pan_x = 0.; self.scene.pan_y = .16
        self.scene.yaw = {'side': -math.pi/2, 'front': math.pi, 'perspective': -2.35}.get(name, -math.pi/2)
        if name == 'perspective': self.scene.pitch = .18
        self.scene.update_camera()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('pose', type=Path, help='JSON containing joints.left/right in degrees')
    p.add_argument('--candidate', nargs=7, type=int, required=True)
    p.add_argument('--symmetric', action='store_true', help='Mirror the right candidate to the left arm in the offline model')
    p.add_argument('--screenshot', type=Path)
    p.add_argument('--view', choices=['side', 'front', 'perspective'], default='side')
    args = p.parse_args()
    payload = json.loads(args.pose.read_text())
    for values in [payload['joints']['left'], payload['joints']['right'], args.candidate]:
        if len(values) != 7 or not np.isfinite(values).all(): p.error('Each arm needs seven finite angles')
    app = QGuiApplication([])
    scene = RobotScene(ROOT)
    for link, part in scene.by_link:
        if link.endswith('_R'):
            part.visual = {**part.visual, 'color': [.16, .53, .88, 1.]}
        elif args.symmetric and link.endswith('_L'):
            part.visual = {**part.visual, 'color': [.10, .65, .47, 1.]}
    for index, value in enumerate(args.candidate):
        joint = next(j for j in scene.model['joints'] if j['name'] == f'Joint{index+1}_R')
        if not math.degrees(float(joint['limit']['lower'])) <= value <= math.degrees(float(joint['limit']['upper'])):
            p.error(f'J{index+1} is outside the URDF joint limit')
    preview = Preview(scene, payload, args.candidate, args.symmetric); preview.view(args.view)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty('robotScene', scene)
    engine.rootContext().setContextProperty('resetPreview', preview)
    engine.load(QUrl.fromLocalFile(str(ROOT / 'scripts/qt_ui/qml/ResetPoseViewer.qml')))
    if not engine.rootObjects(): return 2
    if args.screenshot:
        def capture():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            ok = engine.rootObjects()[0].grabWindow().save(str(args.screenshot))
            app.exit(0 if ok else 3)
        QTimer.singleShot(1800, capture)
    result = app.exec()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
