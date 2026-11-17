"""Mechanical frame consistency and visible jaw motion for the Windows revision."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
from qt_ui.visuals import transform, rotation

ROOT = Path(__file__).resolve().parents[1]
CAD = json.loads((ROOT/'umi_collection/vendor/galaxis/docs/windows_end_effector_measurements.json').read_text())


def test_hand_and_motor_camera_poses_share_the_gear_datum():
    hand = CAD['assemblies']['right']
    for kind in ('left','motor'):
        np.testing.assert_allclose(CAD['assemblies'][kind]['camera_pose'], hand['camera_pose'], atol=1e-8)
    assert abs(hand['camera_rpy_rad'][1]-np.deg2rad(15)) < 1e-10


def test_motor_closure_feedback_reduces_pad_gap_to_mesh_tolerance():
    model = json.loads((ROOT/'public/tianji-model.json').read_text())
    joints = {j['name']:j for j in model['joints']}
    links = {l['name']:l for l in model['links']}
    # Check both assemblies and both jaws using actual exported TPU mesh vertices.
    data = CAD['assemblies']['motor']
    for side, letter in [('left','L'),('right','R')]:
        folder = next((ROOT/'teleop_control/vendor/tianji').glob(f'*/urdf/marvin_{side}_with_gripper.urdf')).parent.parent
        manifest = json.loads((folder/'meshes/gripper/manifest.json').read_text())
        for part in manifest['parts']:
            if '脚垫' not in part['name']:continue
            link = part['link'];num = link.rsplit('_',1)[1]
            visual = next(v for v in links[link]['visuals'] if v['mesh']==f"windows_{letter}_{part['index']:02d}")
            vertices = np.array(model['meshes'][visual['mesh']]['vertices'])
            j = joints[f'gripper_jaw_{num}_{letter}'];m = j['mimic']
            maximum = float(joints[m['joint']]['limit']['upper'])
            native_sign = -1 if num=='1' else 1
            gaps=[]
            for fraction in (0,.5,1):
                angle = fraction*maximum*float(m['multiplier'])+float(m['offset'])
                frame = np.array(data['assembly_from_flange']) @ transform(j['xyz'],j['rpy']) @ rotation(j['axis'],angle)
                points = vertices @ frame[:3,:3].T + frame[:3,3]
                gaps.append(np.min(native_sign*points[:,0]))
            assert gaps[0]>gaps[1]>gaps[2]
            assert abs(gaps[2])<.00025  # Display simplification tolerance; not real stop calibration.


def test_motor_visual_and_collision_mesh_references_resolve():
    for path in (ROOT/'teleop_control/vendor/tianji').glob('*/urdf/marvin_*_with_gripper.urdf'):
        tree=ET.parse(path)
        for link in tree.findall('link'):
            if not link.get('name').startswith('gripper_'):continue
            for mesh in link.findall('.//mesh'):
                filename=path.parent/mesh.get('filename')
                assert filename.is_file() and filename.stat().st_size>84


def test_hand_encoder_zero_visually_closes_and_positive_angle_opens():
    import xacro
    source=ROOT/'umi_collection/vendor/galaxis/galaxis_description/urdf/galaxis_hand.urdf.xacro'
    model=ET.fromstring(xacro.process_file(str(source)).toxml())
    for hand in ('right','left'):
        for side,sign in [('left',1),('right',-1)]:
            role=side+'_finger'
            part=next(p for p in CAD['hand_visual_parts'] if p['hand']==hand and p['role']==role and '脚垫' in p['name'])
            visual=model.find(f"link[@name='{hand}_{side}_finger_link']/visual[@name='{part['visual']}']")
            path=source.parent.parent/'meshes/windows'/part['mesh']
            dtype=np.dtype([('normal','<f4',(3,)),('vertices','<f4',(3,3)),('attribute','<u2')])
            raw=path.read_bytes();count=int.from_bytes(raw[80:84],'little')
            vertices=np.frombuffer(raw,dtype=dtype,count=count,offset=84)['vertices'].reshape(-1,3)
            origin=visual.find('origin');V=transform(np.fromstring(origin.get('xyz'),sep=' '),np.fromstring(origin.get('rpy'),sep=' '))
            joint=model.find(f"joint[@name='{hand}_{side}_finger_joint']")
            J=transform(np.fromstring(joint.find('origin').get('xyz'),sep=' '),[0,0,0])
            direction=(1 if hand=='left' else -1)*(1 if side==hand else -1)
            gaps=[]
            for q in (0,.2,.6):
                T=J@rotation([0,0,1],direction*q)@V
                points=vertices@T[:3,:3].T+T[:3,3]
                gaps.append(np.min(sign*points[:,1]))
            assert abs(gaps[0])<.0003
            assert gaps[2]>gaps[1]>gaps[0]
