"""Build an offline direction preview from the current control code, without I/O."""

import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / 'teleop_control/vendor/tianji'
os.environ['MARVIN_SDK_ROOT'] = str(VENDOR)
sys.path.insert(0, str(VENDOR / 'teleop'))
sys.path.insert(0, str(ROOT / 'scripts'))

from marvin_teleop.configuration import configuration
from marvin_teleop.control import Controller
from marvin_teleop.geometry import inverse, transform
from marvin_teleop.mapping import installed_base_from_nominal, same_facing_map
from marvin_teleop.model import Models
from marvin_teleop.robot import SimRobot
from sync_tianji_installation import build_installation


def build():
    config_path = VENDOR / 'teleop/marvin_teleop/config.json'
    config = configuration(config_path)
    models = Models(config)
    model = json.loads((ROOT / 'public/tianji-model.json').read_text())
    reference = next(joint for joint in model['joints'] if joint['name'] == 'stand_to_L')
    reference_base = installed_base_from_nominal(models.nominal_bases['left'], 'same_facing_y_half_turn')
    display = transform(reference['xyz'], reference['rpy']) @ inverse(reference_base)
    installation = build_installation(ROOT)
    for joint in model['joints']:
        joint.update(installation['joints'].get(joint['name'], {}))
    for mesh in model['meshes'].values():
        vertices, lookup, indices = [], {}, []
        for vertex in mesh['vertices']:
            key = tuple(round(value / .004) for value in vertex)
            if key not in lookup:
                lookup[key] = len(vertices)
                vertices.append([round(value * .004, 4) for value in key])
            indices.append(lookup[key])
        faces = dict.fromkeys(tuple(indices[index] for index in face) for face in mesh['faces'])
        mesh.update(vertices=vertices, faces=[face for face in faces if len(set(face)) == 3])
    directions = [('前', [0, 0, -1]), ('后', [0, 0, 1]), ('左', [-1, 0, 0]),
                  ('右', [1, 0, 0]), ('上', [0, 1, 0]), ('下', [0, -1, 0])]
    initial = np.zeros(7)
    traces = {}
    for side, arm in models.arms.items():
        if not arm.valid_joints(initial):
            raise ValueError(f'{side}: zero joints outside limits')
        traces[side] = []
        for label, direction in directions:
            controller = Controller(models, SimRobot(), config)
            state = controller.arms[side]
            anchor = arm.fk(initial)
            state.anchor = (np.eye(4), anchor.copy())
            state.previous_hand = np.eye(4)
            state.basis = same_facing_map(np.eye(4), models.robot_from_operator)
            state.commanded = initial.copy()
            state.joint_anchor = initial.copy()
            frames = []
            for step in range(301):
                hand = np.eye(4)
                hand[:3, 3] = np.array(direction) * .02 * min(step / 200, 1)
                joints = {name: initial.copy() for name in models.arms}
                joints[side] = state.commanded.copy()
                if step:
                    desired = controller._target(side, SimpleNamespace(hands={side: hand}, sequence=step), joints, .01)
                    state.velocity = state.next_velocity.copy()
                    state.commanded = desired.copy()
                actual = arm.fk(state.commanded)
                if step % 10 == 0:
                    frames.append(dict(q=state.commanded.round(6).tolist(),
                                       tcp=(display @ actual)[:3, 3].round(7).tolist()))
            displacement = state.target_pose[:3, 3] - anchor[:3, 3]
            expected = state.basis @ np.array(direction) * .02 * config['scale']
            expected[2] *= config['teleop_translation_z_sign'][side]
            expected[:2] *= config['teleop_translation_xy_sign'][side]
            np.testing.assert_allclose(displacement, expected, atol=1e-10)
            traces[side].append(dict(label=label, input=direction, delta=displacement.round(7).tolist(),
                                     start=(display @ anchor)[:3, 3].tolist(),
                                     end=(display @ state.target_pose)[:3, 3].tolist(), frames=frames,
                                     error_mm=float(np.linalg.norm(actual[:3, 3] - state.target_pose[:3, 3]) * 1000)))
    payload = dict(model=model, traces=traces, initial=initial.tolist(), installation=installation,
                   scale=config['scale'], display=display.flatten().tolist(),
                   config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
                   generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                   source_sha256={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in (VENDOR/'teleop/marvin_teleop/control.py',
                                               VENDOR/'teleop/marvin_teleop/mapping.py',
                                               VENDOR/'teleop/marvin_teleop/model.py',
                                               ROOT/'public/tianji-model.json',
                                               *(arm.path for arm in models.arms.values()))},
                   teleop_translation_xy_sign=config['teleop_translation_xy_sign'],
                   teleop_translation_z_sign=config['teleop_translation_z_sign'],
                   scope='offline_kinematics_only_no_collision_or_hardware_validation')
    compressed = base64.b64encode(gzip.compress(json.dumps(payload, separators=(',', ':')).encode())).decode()
    source = Path(__file__).with_name('six-directions.ts')
    bundled = subprocess.run([str(ROOT / 'node_modules/.bin/esbuild'), str(source), '--bundle', '--minify', '--format=iife'],
                             check=True, capture_output=True, text=True).stdout
    fragment = Path(__file__).with_name('six-directions.template.html').read_text()
    fragment = fragment.replace('__PAYLOAD__', compressed).replace('__SCRIPT__', bundled)
    assert len(fragment.encode()) < 1_000_000
    output = ROOT / 'reports/six-direction-preview.html'
    output.parent.mkdir(exist_ok=True)
    output.write_text(fragment)
    print(json.dumps({'output': str(output), 'bytes': output.stat().st_size,
                      'errors_mm': {side: [round(trace['error_mm'], 3) for trace in values]
                                    for side, values in traces.items()}}, ensure_ascii=False))


if __name__ == '__main__':
    build()
