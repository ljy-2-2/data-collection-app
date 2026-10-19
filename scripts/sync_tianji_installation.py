"""Export the same installed bases used by FK/IK, without robot connections."""
import argparse
import json
import math
import os
from pathlib import Path
import sys

def build_installation(app_root):
    vendor = app_root/'teleop_control/vendor/tianji'
    os.environ['MARVIN_SDK_ROOT'] = str(vendor)
    sys.path.insert(0, str(vendor/'teleop'))
    from marvin_teleop.configuration import configuration
    from marvin_teleop.geometry import inverse, transform
    from marvin_teleop.mapping import installed_base_from_nominal
    from marvin_teleop.model import Models
    config = configuration(vendor/'teleop/marvin_teleop/config.json')
    models = Models(config)
    display = json.loads((app_root/'public/tianji-model.json').read_text())
    reference = next(joint for joint in display['joints'] if joint['name'] == 'stand_to_L')
    reference_base = installed_base_from_nominal(models.nominal_bases['left'], 'same_facing_y_half_turn')
    display_from_world = transform(reference['xyz'], reference['rpy']) @ inverse(reference_base)
    joints = {}
    for side, arm in models.arms.items():
        letter = 'L' if arm.model_side == 'left' else 'R'
        frame = display_from_world @ arm.base
        rotation = frame[:3, :3]
        pitch = math.atan2(-rotation[2, 0], math.hypot(rotation[0, 0], rotation[1, 0]))
        if abs(math.cos(pitch)) < 1e-8:
            raise ValueError('Display mounting Euler conversion is singular')
        angles = [math.atan2(rotation[2, 1], rotation[2, 2]), pitch,
                  math.atan2(rotation[1, 0], rotation[0, 0])]
        joints['stand_to_'+letter] = dict(xyz=frame[:3, 3].tolist(), rpy=angles)
    return dict(installation_id=config.get('installation_id', 'nominal'),
                arm_models=config['arm_models'], arm_base_sides=config['arm_base_sides'],
                arm_channels=config['arm_channels'], joints=joints)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root/'public/tianji-installation.json'
    output.write_text(json.dumps(build_installation(root), indent=2)+'\n')
