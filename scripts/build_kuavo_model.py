#!/usr/bin/env python3
"""Build display-only meshes from a verified official checkout (no robot IO).

Build dependency: numpy, fast-simplification==0.1.13. Neither is added to the
frontend runtime. Joint origins/axes come from our unchanged control URDF.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import xml.etree.ElementTree as ET
import numpy as np
import fast_simplification
from kuavo_visual_tools import attach_tools, write_model

ROOT = Path(__file__).resolve().parents[1]
COMMIT = 'ba5ae219d86076c587e5973c44f5f645ca874a95'
SOURCE = 'https://github.com/LejuRobotics/kuavo-ros-opensource'
# These official meshes combine the wrist actuator and the original hand.
# Preserve complete actuator components crossing the joint's local Z=0 plane;
# palm, fingers and original hand adapters lie wholly below that plane.
WRIST_MESHES = {'l_hand_pitch_noHand.STL': 'l_wrist_pitch_shell',
               'r_hand_pitch_noHand.STL': 'r_wrist_pitch_shell'}


def wrist_components(vertices, faces):
    """Select original connected shells, without clipping or invented geometry."""
    parents = np.arange(len(vertices))
    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    for face in faces:
        roots = [root(index) for index in face]
        parents[roots[1]] = roots[0]
        parents[roots[2]] = roots[0]
    groups = {}
    for index, face in enumerate(faces):
        groups.setdefault(int(root(face[0])), []).append(index)
    selected = []
    retained = 0
    for indices in groups.values():
        points = vertices[faces[indices]].reshape(-1, 3)
        if points[:, 2].min() <= 0 <= points[:, 2].max():
            selected.extend(indices)
            retained += 1
    assert retained >= 2 and len(selected) < len(faces), 'Wrist/hand separation failed'
    selected_faces = faces[sorted(selected)]
    indices, inverse = np.unique(selected_faces, return_inverse=True)
    result = vertices[indices]
    assert result[:, 2].min() > -.07 and result[:, 2].max() > .015
    return result, inverse.reshape(-1, 3), dict(
        selection='complete_components_crossing_joint_z_zero',
        source_components=len(groups), retained_components=retained,
        selected_triangles=len(selected_faces),
        selected_bounds_m=[result.min(0).tolist(), result.max(0).tolist()])


def vector(element, key, default):
    return [float(v) for v in (element.get(key, default) if element is not None else default).split()]


def build(checkout, tree):
    urdf = ROOT/'teleop_control/assets/kuavo/full_robot.urdf'
    robot = ET.parse(urdf).getroot()
    entries = {v['path']:v for v in tree['tree']}
    assert tree['sha'] == COMMIT
    model = dict(source=SOURCE+'/tree/'+COMMIT+'/src/kuavo_assets/models/biped_s63',
                 root_link='base_link', meshes={},links=[],joints=[])
    manifest = dict(repository=SOURCE,commit=COMMIT,control_urdf_sha256=hashlib.sha256(urdf.read_bytes()).hexdigest(),
                    description='Official v63 shells with separated wrist actuators; palm, fingers and original hand adapters removed. Unchanged control URDF kinematics.',
                    files={}, original_triangles=0, display_triangles=0)
    for link in robot.findall('link'):
        visuals=[]
        for visual in link.findall('visual'):
            mesh=visual.find('geometry/mesh')
            if mesh is None:continue
            path=mesh.get('filename').replace('package://kuavo_assets/','src/kuavo_assets/')
            source_key=Path(path).name
            key=WRIST_MESHES.get(source_key, source_key)
            if key not in model['meshes']:
                data=(checkout/path).read_bytes()
                assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==entries[path]['sha'],path
                count=struct.unpack_from('<I',data,80)[0]
                assert len(data)==84+count*50
                raw=np.frombuffer(data,offset=84,dtype=np.dtype([('normal','<f4',3),('points','<f4',(3,3)),('attr','<u2')]))['points']
                vertices,inverse=np.unique(np.round(raw.reshape(-1,3).astype(float),8),axis=0,return_inverse=True)
                faces=inverse.reshape(-1,3)
                selection = {}
                if source_key in WRIST_MESHES:
                    vertices, faces, selection = wrist_components(vertices, faces)
                before=vertices.copy(); original_faces=faces.copy()
                # Higher-detail body shells for close-up viewing; tools retain source precision.
                # Preserve wrist shells and tool meshes at their source precision.
                target=14000 if key in ('torso.STL','chassis.STL','head_pitch.STL') else 10000
                if len(faces)>target:vertices,faces=fast_simplification.simplify(vertices,faces,target_count=target,agg=5.)
                # QEM can generate spikes on imperfect CAD triangulation. Keep
                # the source mesh if the simplification moves its bounds >5 mm.
                if not np.isfinite(vertices).all() or np.max(np.abs(np.stack((before.min(0),before.max(0)))-np.stack((vertices.min(0),vertices.max(0)))))>.005:
                    vertices,faces=before,original_faces
                vertices=np.round(vertices,6)
                model['meshes'][key]=dict(vertices=vertices.tolist(),faces=faces.tolist())
                manifest['files'][path]=dict(git_blob=entries[path]['sha'],sha256=hashlib.sha256(data).hexdigest(),
                    source_triangles=count,display_triangles=len(faces),
                    bounds_change_m=float(np.max(np.abs(np.stack((before.min(0),before.max(0)))-np.stack((vertices.min(0),vertices.max(0)))))),
                    **selection)
                manifest['original_triangles']+=count;manifest['display_triangles']+=len(faces)
            origin=visual.find('origin');color=visual.find('material/color')
            # Neutral shell plus dark tyres/sensors improves shape readability.
            rgba=vector(color,'rgba','0.86 0.89 0.92 1')
            if rgba[:3]==[1.,1.,1.]:rgba=[.86,.89,.92,1]
            if key.startswith('wheel_') or key in ('head_camera_depth.STL','head_radar.STL'):rgba=[.19,.22,.27,1]
            visuals.append(dict(mesh=key,xyz=vector(origin,'xyz','0 0 0'),rpy=vector(origin,'rpy','0 0 0'),scale=vector(mesh,'scale','1 1 1'),color=rgba))
        model['links'].append(dict(name=link.get('name'),visuals=visuals))
    for joint in robot.findall('joint'):
        origin=joint.find('origin');axis=joint.find('axis');limit=joint.find('limit');mimic=joint.find('mimic')
        model['joints'].append(dict(name=joint.get('name'),type=joint.get('type'),parent=joint.find('parent').get('link'),
            child=joint.find('child').get('link'),xyz=vector(origin,'xyz','0 0 0'),rpy=vector(origin,'rpy','0 0 0'),
            axis=vector(axis,'xyz','1 0 0'),mimic=None if mimic is None else dict(mimic.attrib),limit=None if limit is None else dict(limit.attrib)))
    attach_tools(model)
    write_model(model, manifest)
    shutil.copyfile(checkout/'LICENSE',ROOT/'public/kuavo-model.LICENSE')
    print(json.dumps({k:manifest[k] for k in ('original_triangles','display_triangles','display_bytes')}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('checkout',type=Path);parser.add_argument('tree',type=Path)
    args=parser.parse_args();build(args.checkout,json.loads(args.tree.read_text()))
