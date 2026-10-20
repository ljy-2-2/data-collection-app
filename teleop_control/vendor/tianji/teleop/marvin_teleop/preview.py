"""Reject a display artifact whose arm frames differ from live kinematics."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

from .geometry import transform


def validate_preview(models, path):
    model = json.loads(Path(path).read_text())
    report = models.mapping_report
    if model.get('coordinate_revision') != report['coordinate_revision'] or model.get('arm_geometry') != report['arm_geometry']:
        raise ValueError('预览坐标版本与遥操不一致，请运行 scripts/sync_tianji_coordinates.py')
    world_display = np.asarray(model['world_from_display'], dtype=float)
    joints = {joint['name']: joint for joint in model['joints']}
    for side, letter in [('left', 'L'), ('right', 'R')]:
        arm = models.arms[side]
        base = joints[f'stand_to_{letter}']
        if not np.allclose(world_display @ transform(base['xyz'], base['rpy']), arm.base, atol=1e-8):
            raise ValueError(f'{side}: 预览安装方向与控制模型不一致，请重新导出预览')
        for source in ET.parse(arm.path).getroot().findall('joint'):
            joint = joints[source.get('name')]
            origin = source.find('origin')
            xyz, rpy = [np.fromstring(origin.get(k, '0 0 0'), sep=' ') for k in ('xyz', 'rpy')]
            if (joint['parent'] != source.find('parent').get('link') or
                    joint['child'] != source.find('child').get('link') or
                    not np.allclose(transform(joint['xyz'], joint['rpy']), transform(xyz, rpy), atol=1e-10)):
                raise ValueError(f'{side}: 预览关节与活动 URDF 不一致')
            axis = source.find('axis')
            if axis is not None and not np.allclose(joint['axis'], np.fromstring(axis.get('xyz'), sep=' '), atol=1e-10):
                raise ValueError(f'{side}: 预览旋转轴与活动 URDF 不一致')
        exported = model['coordinate_mapping']['control_points'][side]
        if not np.allclose(exported['flange_to_tcp'], arm.tool, atol=1e-10):
            raise ValueError(f'{side}: 预览 TCP 与控制点不一致，请重新导出预览')
    return {'coordinate_revision': report['coordinate_revision'], 'arms': 2, 'consistent': True}
