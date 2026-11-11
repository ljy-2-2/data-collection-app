"""Export fixed gripper frames from the active URDFs to the display artifact."""
import json
import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def sync_frames(app_root):
    vendor = app_root/'teleop_control/vendor/tianji'
    sys.path.insert(0, str(vendor/'teleop'))
    from marvin_teleop.tool_frame import parse_tool_frame, resolve_urdf_tool
    config_path = vendor/'teleop/marvin_teleop/config.json'
    config = json.loads(config_path.read_text())
    model_path = app_root/'public/tianji-model.json'
    model = json.loads(model_path.read_text())
    if model.get('arm_geometry', 'nominal') != config.get('arm_geometry', 'nominal'):
        raise ValueError('URDF and preview geometry revisions differ; regenerate the full model first')
    joints = {joint['name']: joint for joint in model['joints']}
    for side, letter in [('left', 'L'), ('right', 'R')]:
        source = config['tool_tcp'][side]
        tree = ET.parse(config_path.parent/source['urdf'])
        for name in (f'gripper_mount_{letter}', f'grasp_center_fixed_{letter}'):
            node = tree.find(f"joint[@name='{name}']")
            if node is None or node.get('type') != 'fixed':
                raise ValueError(f'Missing fixed joint: {name}')
            joint = joints[name]
            if (joint['parent'] != node.find('parent').get('link')
                    or joint['child'] != node.find('child').get('link')):
                raise ValueError(f'Display topology differs from URDF: {name}')
            origin = node.find('origin')
            for field in ('xyz', 'rpy'):
                joint[field] = [float(value) for value in origin.get(field, '0 0 0').split()]
        # The startup consistency check also reads the exported TCP matrix.
        tool, name = parse_tool_frame(resolve_urdf_tool(source, config_path.parent))
        model['coordinate_mapping']['control_points'][side] = dict(name=name, flange_to_tcp=tool.tolist())
    model_path.write_text(json.dumps(model, separators=(',', ':'))+'\n')


if __name__ == '__main__':
    sync_frames(Path(__file__).resolve().parents[1])
