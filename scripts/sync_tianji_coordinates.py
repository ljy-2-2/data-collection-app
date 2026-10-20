#!/usr/bin/env python3
"""Export manufacturer arm frames and active mounting to the shared preview.

No robot connection. Existing simplified meshes retain their CAD dimensions.
The legacy reflected artifact is migrated once; subsequent runs are idempotent.
"""
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'teleop_control/vendor/tianji/teleop'))
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models


def rpy(matrix):
    # Rz(yaw) Ry(pitch) Rx(roll); arm mounts are away from pitch singularity.
    return [float(np.arctan2(matrix[2, 1], matrix[2, 2])),
            float(np.arctan2(-matrix[2, 0], np.hypot(matrix[0, 0], matrix[1, 0]))),
            float(np.arctan2(matrix[1, 0], matrix[0, 0]))]


def sync(root=ROOT):
    config = configuration(root / 'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json')
    models = Models(config)
    path = root / 'public/tianji-model.json'
    model = json.loads(path.read_text())
    if model.get('arm_geometry', 'nominal') not in ('nominal', 'front_back_mirror_v1'):
        raise ValueError('Unknown preview geometry')
    links = {link['name']: link for link in model['links']}
    joints = {joint['name']: joint for joint in model['joints']}
    arm_meshes = set()
    for side, arm in models.arms.items():
        tree = ET.parse(arm.path).getroot()
        for source in tree.findall('joint'):
            target = joints[source.get('name')]
            if (target['parent'] != source.find('parent').get('link') or
                    target['child'] != source.find('child').get('link')):
                raise ValueError('Preview topology differs from URDF')
            origin = source.find('origin')
            for field in ('xyz', 'rpy'):
                target[field] = [float(v) for v in origin.get(field, '0 0 0').split()]
            axis = source.find('axis')
            if axis is not None:
                target['axis'] = [float(v) for v in axis.get('xyz').split()]
        for source in tree.findall('link'):
            visuals = source.findall('visual')
            target = links[source.get('name')]['visuals']
            if len(visuals) != len(target):
                raise ValueError('Preview visual count differs from URDF')
            for node, visual in zip(visuals, target):
                origin = node.find('origin')
                for field in ('xyz', 'rpy'):
                    visual[field] = [float(v) for v in origin.get(field, '0 0 0').split()]
                arm_meshes.add(visual['mesh'])
    if arm_meshes & {v['mesh'] for v in links['stand']['visuals']}:
        raise ValueError('Arm and stand unexpectedly share a mesh')
    if model.get('arm_geometry') == 'front_back_mirror_v1':
        for key in arm_meshes:
            mesh = model['meshes'][key]
            mesh['vertices'] = [[-x, y, z] for x, y, z in mesh['vertices']]
            mesh['faces'] = [[a, c, b] for a, b, c in mesh['faces']]
    # The display's stand mesh has a fixed floor origin. Preserve its placement,
    # derive ONE translation for both arms; never register each arm independently.
    offset = np.asarray(joints['stand_to_L']['xyz']) - models.arms['left'].base[:3, 3]
    if not np.allclose(np.asarray(joints['stand_to_R']['xyz']) - models.arms['right'].base[:3, 3], offset, atol=1e-6):
        raise ValueError('Display shoulder spacing differs from the physical mount')
    world_display = np.eye(4)
    world_display[:3, 3] = -offset
    for side, letter in [('left', 'L'), ('right', 'R')]:
        joint = joints[f'stand_to_{letter}']
        joint['xyz'] = (models.arms[side].base[:3, 3] + offset).tolist()
        joint['rpy'] = rpy(models.arms[side].base[:3, :3])
    model.update(arm_geometry='nominal', coordinate_revision='vendor_cad_same_facing_v2',
                 world_from_display=world_display.tolist(),
                 source='Manufacturer arm URDFs with fixed gripper TCP; active physical mounting',
                 coordinate_mapping=models.mapping_report)
    path.write_text(json.dumps(model, separators=(',', ':'), ensure_ascii=False) + '\n')


if __name__ == '__main__':
    sync()
