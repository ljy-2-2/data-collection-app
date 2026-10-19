"""Compare disk configuration with the standard drawing, without robot I/O."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import numpy as np

APP_ROOT = Path(__file__).resolve().parents[1]
VENDOR = APP_ROOT / 'teleop_control/vendor/tianji'
os.environ['MARVIN_SDK_ROOT'] = str(VENDOR)
sys.path.insert(0, str(VENDOR / 'teleop'))

from marvin_teleop.configuration import configuration
from marvin_teleop.geometry import angle, inverse
from marvin_teleop.mapping import same_facing_map
from marvin_teleop.model import MOUNT, Models


DRAWING_WORLD_FROM_MOUNT = np.array([
    [0., -1., 0., 0.],
    [1., 0., 0., 0.],
    [0., 0., 1., 0.],
    [0., 0., 0., 1.],
])
DRAWING_WORLD_FROM_BASES = {
    'left': np.array([
        [1., 0., 0., 0.],
        [0., 0., 1., .0470005033993383],
        [0., -1., 0., .142],
        [0., 0., 0., 1.],
    ]),
    'right': np.array([
        [1., 0., 0., 0.],
        [0., 0., -1., -.0469994966006617],
        [0., 1., 0., .142],
        [0., 0., 0., 1.],
    ]),
}
NEUTRAL_WORLD_FROM_PICO = np.array([[0., 0., -1.], [-1., 0., 0.], [0., 1., 0.]])


def drawing_audit(config):
    models = Models(config)
    if not np.allclose(models.world_mount, DRAWING_WORLD_FROM_MOUNT, atol=1e-9, rtol=0):
        raise ValueError('CAD 到世界坐标与图纸基准不一致，禁止把报告作为方向依据')
    arms = {}
    for side, arm in models.arms.items():
        base_side = config.get('arm_base_sides', {}).get(side, side)
        reference = DRAWING_WORLD_FROM_BASES[base_side]
        if not np.allclose(arm.nominal_base, reference, atol=1e-9, rtol=0):
            raise ValueError(f'{side}: 厂家模型或名义安装不符合标准图纸，需单独核对换边安装')
        difference = inverse(reference) @ arm.base
        arms[side] = {
            'channel': config['arm_channels'][side],
            'manufacturer_model': arm.model_side,
            'fixture_base_side': base_side,
            'drawing_world_from_base': reference.tolist(),
            'configured_world_from_base': arm.base.tolist(),
            'configured_rotation_difference_deg': float(np.degrees(angle(difference[:3, :3]))),
            'configured_origin_difference_mm': float(np.linalg.norm(difference[:3, 3]) * 1000),
            'matches_standard_drawing': bool(np.allclose(arm.base, reference, atol=1e-9, rtol=0)),
            'flange_to_tcp': arm.tool.tolist(),
        }
    neutral = same_facing_map(np.eye(4), models.robot_from_operator)
    if not np.allclose(neutral, NEUTRAL_WORLD_FROM_PICO, atol=1e-9, rtol=0):
        raise ValueError('头显中立方向的映射不符合原生 PICO 前/左/上定义')
    return {
        'schema': 'tianji_standard_drawing_directions_v1',
        'scope': 'offline_disk_configuration_only',
        'physical_installation_verified': False,
        'live_configuration_changed': False,
        'installation_id': config.get('installation_id', 'nominal'),
        'teleop_translation_z_sign': config['teleop_translation_z_sign'],
        'teleop_translation_xy_sign': config['teleop_translation_xy_sign'],
        'teleop_translation_override_scope': 'world_displacement_about_clutch_anchor_only_not_base_or_rotation',
        'world_axes': ['forward', 'left', 'up'],
        'world_origin': 'fixture_urdf_origin_not_floor',
        'units': {'position': 'metres', 'joint_angle': 'degrees'},
        'drawing_world_from_mount': DRAWING_WORLD_FROM_MOUNT.tolist(),
        'neutral_world_from_pico': neutral.tolist(),
        'arms': arms,
        'source_sha256': {
            str(path.relative_to(VENDOR)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (MOUNT, VENDOR / 'CommonConfig/ccs_m6_40.MvKDCfg')
        },
        'physical_check_required': [
            '安装面缺口对应机械臂后面；核对实物缺口与图纸，而非依据屏幕左右。',
            '按机器人自身左右核对法兰 L/R 标记及 A/B 通道。',
            '图纸基准不是实物标定；存在安装旋转时须测量，不能自动应用方向翻转。',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=VENDOR / 'teleop/marvin_teleop/config.json')
    parser.add_argument('--output', type=Path, default=APP_ROOT / 'reports/tianji-drawing-directions.json')
    args = parser.parse_args()
    if args.output.resolve() == args.config.resolve():
        parser.error('报告不能覆盖控制配置')
    report = drawing_audit(configuration(args.config))
    report['config_source'] = str(args.config.resolve())
    report['config_sha256'] = hashlib.sha256(args.config.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'离线图纸核对报告：{args.output}；未连接真机、未修改控制配置。')


if __name__ == '__main__':
    main()
