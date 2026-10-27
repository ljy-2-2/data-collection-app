"""Regenerate tool-only 35 mm reserves from CAD; no hardware connection."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "teleop_control/vendor/tianji/teleop"))
from marvin_teleop.gripper_collision import PREFIX, protection_bounds


def build():
    for side, letter in (("left", "L"), ("right", "R")):
        path = next((ROOT / "teleop_control/vendor/tianji").glob(
            f"*M6-S-{letter}*/urdf/marvin_{side}_with_gripper.urdf"))
        text = path.read_text()
        text = re.sub(r'    <collision name="safety_35mm_[^"]+">.*?</collision>\n',
                      '', text, flags=re.S)
        blocks = {}
        for name, (center, half) in protection_bounds(path, letter).items():
            xyz = ' '.join(f'{v:.12f}' for v in center)
            size = ' '.join(f'{v:.12f}' for v in half * 2)
            link = f'gripper_{letter}' if name.startswith('camera_') else name
            blocks.setdefault(link, []).append(f'    <collision name="{PREFIX}{name}">\n'
                          f'      <origin xyz="{xyz}" rpy="0 0 0" />\n'
                          f'      <geometry><box size="{size}" /></geometry>\n'
                          f'    </collision>\n')
        for link, contents in blocks.items():
            marker = f'  <link name="{link}">\n'
            text = text.replace(marker, marker + ''.join(contents))
        path.write_text(text)
        print(f'{side}: {sum(map(len, blocks.values()))} moving-link protection boxes (including camera)')


if __name__ == '__main__':
    build()
