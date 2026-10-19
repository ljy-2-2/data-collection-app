#!/usr/bin/env python3
"""Offline CAD viewer for the active Tianji URDF TCP; no backend or hardware imports."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

os.environ.setdefault('QSG_RHI_BACKEND', 'opengl')
os.environ.setdefault('QSG_RENDER_LOOP', 'basic')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import numpy as np
from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer, QUrl, QCoreApplication, QEvent
from PySide6.QtGui import QGuiApplication, QVector3D, QQuaternion
from PySide6.QtQml import QQmlApplicationEngine
from qt_ui.visuals import RobotScene, transform, rotation, quat


class TcpViewer(QObject):
    changed = Signal()
    partsChanged = Signal()

    def __init__(self):
        super().__init__()
        self.scene = RobotScene(ROOT)
        self.selection = 'left'
        self.opening = .65
        self.yaw, self.pitch, self.zoom = -.65, .18, 1.
        self.revision_value = 0
        self.transforms = {}
        # Read actual control definitions; reject a stale display export.
        config_path = ROOT / 'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'
        config = json.loads(config_path.read_text())
        joints = {j['name']: j for j in self.scene.model['joints']}
        for side, letter in [('left', 'L'), ('right', 'R')]:
            source = config['tool_tcp'][side]
            tree = ET.parse(config_path.parent / source['urdf'])
            for name in (f'gripper_mount_{letter}', f'grasp_center_fixed_{letter}'):
                node = tree.find(f"joint[@name='{name}']/origin")
                for field in ('xyz', 'rpy'):
                    assert np.allclose(np.fromstring(node.get(field), sep=' '), joints[name][field], atol=1e-12), name
        self.update()

    def update(self):
        pending = self.scene.model['joints'][:]
        matrices = {'stand': np.eye(4)}
        angles = {j['name']: self.opening * float(j['limit']['upper']) for j in pending if j['name'].startswith('gripper_drive_')}
        while pending:
            remaining = []
            for joint in pending:
                if joint['parent'] not in matrices:
                    remaining.append(joint)
                    continue
                angle = angles.get(joint['name'], 0.)
                if joint['mimic']:
                    m = joint['mimic']; angle = angles.get(m['joint'], 0.) * float(m['multiplier']) + float(m['offset'])
                matrix = matrices[joint['parent']] @ transform(joint['xyz'], joint['rpy'])
                if joint['type'] in ('revolute', 'continuous'):
                    matrix = matrix @ rotation(joint['axis'], angle)
                matrices[joint['child']] = matrix
            if len(remaining) == len(pending):
                raise ValueError('Disconnected CAD joints')
            pending = remaining
        self.transforms = matrices
        for link, part in self.scene.by_link:
            part.update_matrix(matrices[link] @ transform(part.visual['xyz'], part.visual['rpy']))
        self.revision_value += 1
        self.changed.emit()

    def local(self, letter, point):
        return (self.transforms[f'gripper_{letter}'] @ np.r_[point, 1.])[:3]

    @Property(int, notify=changed)
    def revision(self): return self.revision_value
    @Property(str, notify=changed)
    def selected(self): return self.selection
    @Property('QVariantList', notify=partsChanged)
    def parts(self):
        letter = 'L' if self.selection == 'left' else 'R'
        return [p for link, p in self.scene.by_link if self.selection == 'all' or link.startswith(f'gripper_{letter}')]
    @Property('QVariantList', notify=changed)
    def markers(self):
        result = []
        for side, letter in [('left', 'L'), ('right', 'R')]:
            if self.selection not in ('all', side): continue
            p = self.transforms[f'grasp_center_{letter}'][:3, 3]
            result.append(dict(label=('左' if side == 'left' else '右') + ' P · 遥操作中心',
                               position=QVector3D(*p.tolist()), color='#168cff'))
        return result
    @Property('QVariantList', notify=changed)
    def lines(self):
        result = []
        for side, letter in [('left', 'L'), ('right', 'R')]:
            if self.selection not in ('all', side): continue
            root = self.transforms[f'gripper_{letter}']
            # Motor axis, then both real jaw pivot axes (not a false line between origins).
            segments = [(self.local(letter, [0, 0, 0]), self.local(letter, [0, 0, .19]), '#e7a33b')]
            for suffix in ('1', '17'):
                j = next(j for j in self.scene.model['joints'] if j['name'] == f'gripper_jaw_{suffix}_{letter}')
                frame = root @ transform(j['xyz'], j['rpy'])
                axis = frame[:3, :3] @ np.asarray(j['axis'])
                center = frame[:3, 3]
                segments.append((center - axis * .036, center + axis * .036, '#40d3a3'))
            for a, b, color in segments:
                vector = b-a
                result.append(dict(position=QVector3D(*((a+b)/2).tolist()),
                    orientation=QQuaternion.rotationTo(QVector3D(0, 1, 0), QVector3D(*vector.tolist())),
                    size=QVector3D(.000018, float(np.linalg.norm(vector))/100, .000018), color=color))
        return result

    def camera(self):
        if self.selection == 'all':
            target = np.array([0., 0., .85])
            up = np.array([0., 0., 1.])
            offset = np.array([math.cos(self.yaw)*math.cos(self.pitch), math.sin(self.yaw)*math.cos(self.pitch), math.sin(self.pitch)])
        else:
            letter = 'L' if self.selection == 'left' else 'R'
            frame = self.transforms[f'gripper_{letter}']
            target = self.local(letter, [0, 0, .115])
            up = frame[:3, :3] @ np.array([0., 0., 1.])
            offset = frame[:3, :3] @ np.array([math.cos(self.yaw)*math.cos(self.pitch), math.sin(self.yaw)*math.cos(self.pitch), math.sin(self.pitch)])
        right = np.cross(up, offset); right /= np.linalg.norm(right)
        corrected_up = np.cross(offset, right)
        matrix = np.eye(4); matrix[:3, :3] = np.column_stack([right, corrected_up, offset])
        matrix[:3, 3] = target + offset * 3
        return matrix
    @Property(QVector3D, notify=changed)
    def cameraPosition(self): return QVector3D(*self.camera()[:3, 3].tolist())
    @Property(QQuaternion, notify=changed)
    def cameraOrientation(self): return quat(self.camera())
    @Property(float, notify=changed)
    def viewHeight(self): return (2.1 if self.selection == 'all' else .30) / self.zoom
    @Slot(str)
    def select(self, side):
        if side not in ('left', 'right', 'all'): return
        self.selection = side; self.zoom = 1.; self.update(); self.partsChanged.emit()
    @Slot(float)
    def setOpening(self, value): self.opening = max(0., min(1., value)); self.update()
    @Slot(float, float)
    def drag(self, dx, dy):
        self.yaw += dx*.008; self.pitch = max(-1.3, min(1.3, self.pitch+dy*.006)); self.update()
    @Slot(float)
    def changeZoom(self, factor): self.zoom = max(.4, min(4., self.zoom*factor)); self.update()
    @Slot()
    def reset(self): self.yaw, self.pitch, self.zoom = -.65, .18, 1.; self.update()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshot', type=Path)
    parser.add_argument('--check', action='store_true', help='Check fixed TCP through simulated opening and exit')
    args = parser.parse_args()
    app = QGuiApplication(sys.argv[:1])
    app.setApplicationDisplayName('天机 TCP 三维仿真查看')
    viewer = TcpViewer()
    before = {s: viewer.transforms[f'grasp_center_{s}'].copy() for s in ('L','R')}
    for opening in (0., .5, 1.):
        viewer.setOpening(opening)
        for s in ('L','R'): np.testing.assert_allclose(viewer.transforms[f'grasp_center_{s}'], before[s], atol=1e-12)
    viewer.setOpening(.65)
    if args.check:
        print('PASS: CAD matches active URDF; both P points remain fixed through jaw opening.')
        return 0
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty('tcpViewer', viewer)
    engine.load(QUrl.fromLocalFile(str(ROOT/'scripts/qt_ui/qml/TcpInspector.qml')))
    if not engine.rootObjects(): return 2
    window = engine.rootObjects()[0]
    if args.screenshot:
        def capture():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            ok = window.grabWindow().save(str(args.screenshot))
            print('Screenshot:', args.screenshot, 'saved:', ok, flush=True)
            app.exit(0 if ok else 3)
        QTimer.singleShot(2200, capture)
    result = app.exec()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    return result


if __name__ == '__main__': raise SystemExit(main())
