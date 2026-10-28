"""Official display meshes, unchanged kinematics and two independent renderers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
import numpy as np

ROOT=Path(__file__).resolve().parents[1]


def test_visual_assets_match_control_urdf_and_have_bounded_simplification():
    data=(ROOT/'public/kuavo-model.json').read_bytes()
    model=json.loads(data)
    manifest=json.loads((ROOT/'teleop_control/assets/kuavo/visual-manifest.json').read_text())
    urdf=ROOT/'teleop_control/assets/kuavo/full_robot.urdf'
    assert hashlib.sha256(data).hexdigest()==manifest['display_sha256']
    assert hashlib.sha256(urdf.read_bytes()).hexdigest()==manifest['control_urdf_sha256']
    assert model['root_link']=='base_link'
    assert len([k for k in model['meshes'] if not k.startswith('kuavo_tool_')])==29
    links={link['name']:link for link in model['links']}
    for side in ('l','r'):
        visual=links[f'zarm_{side}7_link']['visuals']
        assert len(visual)==1 and visual[0]['mesh']==f'{side}_wrist_pitch_shell'
        points=np.array(model['meshes'][visual[0]['mesh']]['vertices'])
        # Actuator shell extends across its pivot; original palm/fingers below
        # the wrist must not return when restoring its visual.
        assert -.07 < points[:,2].min() < -.05 and points[:,2].max() > .015
        source=manifest['files'][f'src/kuavo_assets/models/biped_s63/meshes/{side}_hand_pitch_noHand.STL']
        assert source['retained_components'] >= 2
        assert source['selected_triangles'] < source['source_triangles']
    assert not any('pitch_noHand' in name for name in model['meshes'])
    assert manifest['display_bytes']<35_000_000
    assert 275_000 <= model['tool_attachment']['body_display_triangles'] <= 300_000
    assert manifest['display_triangles']==sum(len(model['meshes'][v['mesh']]['faces']) for l in model['links'] for v in l['visuals'])
    assert all(v['bounds_change_m']<.005 for v in manifest['files'].values())
    assert 'Leju' in (ROOT/'public/kuavo-model.LICENSE').read_text()
    original={j.get('name'):j for j in ET.parse(urdf).getroot().findall('joint')}
    control_joints=[j for j in model['joints'] if not j['name'].startswith('kuavo_tool_')]
    assert {j['name'] for j in control_joints}==set(original)
    for joint in control_joints:
        src=original[joint['name']]
        assert joint['parent']==src.find('parent').get('link')
        assert joint['child']==src.find('child').get('link')
        for field in ('xyz','rpy'):
            node=src.find('origin');expected=[float(v) for v in node.get(field,'0 0 0').split()] if node is not None else [0,0,0]
            assert joint[field]==expected
        axis=src.find('axis')
        assert joint['axis']==([float(v) for v in axis.get('xyz').split()] if axis is not None else [1,0,0])
    for mesh in model['meshes'].values():
        vertices=np.array(mesh['vertices']);faces=np.array(mesh['faces'])
        assert np.isfinite(vertices).all() and vertices.shape[1]==3
        assert faces.min()>=0 and faces.max()<len(vertices)
        normals=np.array(mesh['normals']);indices=np.array(mesh['normal_indices'])
        assert indices.shape==faces.shape and indices.min()>=0 and indices.max()<len(normals)
        assert np.isfinite(normals).all() and np.allclose(np.linalg.norm(normals,axis=1),1,atol=2e-6)
    assert all(v['mesh'] in model['meshes'] for l in model['links'] for v in l['visuals'])


def test_web_and_qt_models_agree_for_feedback_and_tianji_defaults(tmp_path):
    # TypeScript math is executed with Node; Qt geometry needs no OpenGL context.
    node=r'''
import fs from 'node:fs'; import ts from 'typescript';
const src=fs.readFileSync('lib/tianjiModel.ts','utf8');
const js=ts.transpileModule(src,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const math=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
const model=JSON.parse(fs.readFileSync('public/kuavo-model.json','utf8'));
const previewSrc=fs.readFileSync('lib/kuavoPreview.ts','utf8');
const previewJS=ts.transpileModule(previewSrc,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const preview=await import('data:text/javascript;base64,'+Buffer.from(previewJS).toString('base64'));
const gripperAngles=preview.kuavoPreviewAngles(model,{}, {left:{closure_fraction:1},right:{closure_fraction:0}});
const angles={...model.display_joint_defaults,zarm_l1_joint:-.6,zarm_l4_joint:-.8};
const links=math.linkTransforms(model,angles);
const t=JSON.parse(fs.readFileSync('public/tianji-model.json','utf8'));
const tianji=math.linkTransforms(t,{Joint1_L:Math.PI/6});
console.log(JSON.stringify({links,tianji,grippers:math.linkTransforms(model,gripperAngles),body:math.linkTransforms(model,{...model.display_joint_defaults,waist_yaw_joint:.3,zhead_1_joint:.2,zhead_2_joint:-.1})}));
'''
    result=subprocess.run(['node','--input-type=module','-e',node],cwd=ROOT,text=True,capture_output=True,check=True)
    expected=tmp_path/'transforms.json';expected.write_text(result.stdout)
    script=r'''
import json, numpy as np
from pathlib import Path
from PySide6.QtGui import QGuiApplication
from qt_ui.visuals import KuavoScene,RobotScene,transform,rotation
app=QGuiApplication([])
expected=json.loads(Path(__import__('sys').argv[1]).read_text())
k=KuavoScene(Path.cwd());t=RobotScene(Path.cwd())
assert k.cameraPosition.x()>0 and t.cameraPosition.x()>0
initial={name:part.matrix.copy() for name,part in k.by_link}
k.update_pose({'telemetry':{'joints':{'zarm_l1_joint':-.6,'zarm_l4_joint':-.8}}})
for name,part in k.by_link:
    matrix=np.array(expected['links'][name]).reshape(4,4)@transform(part.visual['xyz'],part.visual['rpy'])
    assert np.allclose(part.matrix,matrix)
    if name.startswith('zarm_r'):assert np.array_equal(part.matrix,initial[name])
assert any(not np.array_equal(part.matrix,initial[name]) for name,part in k.by_link if name.startswith('zarm_l'))
# No command/target field may move the measured display.
before=[p.matrix.copy() for _,p in k.by_link]
k.update_pose({'telemetry':{'joints':{'zarm_l1_joint':-.6,'zarm_l4_joint':-.8}},'command_rad':[2]*14})
assert all(np.array_equal(p.matrix,m) for (_,p),m in zip(k.by_link,before))
# Each restored seventh-axis shell follows only its own measured rotation.
for side in ('l','r'):
    k.update_pose({})
    zero={id(part):part.matrix.copy() for name,part in k.by_link}
    wrist=next(part for name,part in k.by_link if name==f'zarm_{side}7_link')
    world_delta=zero[id(wrist)]@rotation((0,1,0),.4)@np.linalg.inv(zero[id(wrist)])
    k.update_pose({'telemetry':{'joints':{f'zarm_{side}7_joint':.4}}})
    for name,part in k.by_link:
        affected=name==f'zarm_{side}7_link' or name==f'kuavo_tool_flange_{side}' or name.startswith('kuavo_tool_gripper_'+side.upper())
        if affected:
            assert not np.allclose(part.matrix,zero[id(part)])
            assert np.allclose(part.matrix,world_delta@zero[id(part)])
        else:assert np.array_equal(part.matrix,zero[id(part)])
k.update_pose({'telemetry':{'joints':{'waist_yaw_joint':.3,'zhead_1_joint':.2,'zhead_2_joint':-.1}}})
for name,part in k.by_link:
    matrix=np.array(expected['body'][name]).reshape(4,4)@transform(part.visual['xyz'],part.visual['rpy'])
    assert np.allclose(part.matrix,matrix),name
assert k._pose_angles['waist_yaw_joint']==.3 and k._pose_angles['zhead_1_joint']==.2
# Actual left closed/right open move only their jaws/shafts, identically to web.
k.update_pose({})
initial={id(part):part.matrix.copy() for name,part in k.by_link}
k.update_pose({'telemetry':{'grippers':{'left':{'closure_fraction':1},'right':{'closure_fraction':0}}}})
for name,part in k.by_link:
    matrix=np.array(expected['grippers'][name]).reshape(4,4)@transform(part.visual['xyz'],part.visual['rpy'])
    assert np.allclose(part.matrix,matrix),name
    if name in ('kuavo_tool_gripper_L_1','kuavo_tool_gripper_R_1'):
        assert not np.allclose(part.matrix,initial[id(part)])
    elif not name.startswith('kuavo_tool_gripper_'):
        assert np.array_equal(part.matrix,initial[id(part)])
assert k._pose_angles['kuavo_tool_gripper_drive_L']==0
assert k._pose_angles['kuavo_tool_gripper_drive_R']>0

t.update_pose({'joints':{'left':[30,0,0,0,0,0,0]}})
for name,part in t.by_link:
    if name.startswith('gripper'):continue
    matrix=np.array(expected['tianji'].get(name,np.eye(4).reshape(-1).tolist())).reshape(4,4)@transform(part.visual['xyz'],part.visual['rpy'])
    # Gripper links use their original calibration defaults; compare only arm links.
    if name in ('stand','Link1_L','Link2_L','Link3_L','Link1_R'):assert np.allclose(part.matrix,matrix)
assert len(k.parts)==137
for scene in (t,k):
    scene.changeZoom(100);assert scene.zoom==4.4
    scene.changeZoom(.00001);assert scene.zoom==.5
    scene.resetView();assert scene.zoom==scene.default_zoom and scene.cameraPosition.x()>0
'''
    env={**os.environ,'QT_QPA_PLATFORM':'offscreen','QT_QUICK_BACKEND':'software',
         'PYTHONPATH':str(ROOT/'scripts')+os.pathsep+os.environ.get('PYTHONPATH','')}
    result=subprocess.run([sys.executable,'-c',script,str(expected)],cwd=ROOT,env=env,text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


def test_provisional_tool_mounts_align_holes_and_omit_tianji_adapters():
    from copy import deepcopy
    from qt_ui.visuals import transform
    from kuavo_visual_tools import attach_tools
    model=json.loads((ROOT/'public/kuavo-model.json').read_text())
    config_path=ROOT/'teleop_control/assets/kuavo/gripper_mounts.json'
    assert hashlib.sha256(config_path.read_bytes()).hexdigest()==model['tool_attachment']['config_sha256']
    assert hashlib.sha256((ROOT/'public/tianji-model.json').read_bytes()).hexdigest()==model['tool_attachment']['tianji_source_sha256']
    assert 'kuavo_tool_m54' not in model['meshes'] and 'kuavo_tool_m116' not in model['meshes']
    joints={j['name']:j for j in model['joints']}
    # Flange CAD bore axes pass through (0,0,29.5 mm), at +/-45 degrees.
    bore_center=np.array([0,0,.0295,1])
    bore_axes=[np.array([1,1,0])/np.sqrt(2),np.array([1,-1,0])/np.sqrt(2)]
    for side,letter in [('l','L'),('r','R')]:
        mount=joints['kuavo_tool_flange_mount_'+side]
        assert mount['type']=='fixed' and mount['parent']==f'zarm_{side}7_link'
        matrix=transform(mount['xyz'],mount['rpy'])
        assert np.allclose(matrix@bore_center,[0,0,-.0586,1])
        axes=[abs(matrix[:3,:3]@a).round(9).tolist() for a in bore_axes]
        assert sorted(axes)==sorted([[1,0,0],[0,1,0]])
        mount=joints['kuavo_tool_gripper_mount_'+letter]
        grip_matrix=matrix@transform(mount['xyz'],mount['rpy'])
        # Matching four-hole patterns on the flange's large face and gripper base.
        holes=[np.array([x,y,.0527,1]) for x,y in [(0,.019),(.019,0),(0,-.019),(-.019,0)]]
        flange_points=np.array([matrix@p for p in holes])
        for p in holes:assert np.min(np.linalg.norm(flange_points-(grip_matrix@p),axis=1))<1e-9
        # Jaw pivot separation must run from left arm to right arm (-base Y)
        # at the zero pose, rather than along the robot's fore/aft X axis.
        pivot_a=np.array(joints['kuavo_tool_gripper_jaw_1_'+letter]['xyz'])
        pivot_b=np.array(joints['kuavo_tool_gripper_jaw_17_'+letter]['xyz'])
        separation=grip_matrix[:3,:3]@(pivot_b-pivot_a)
        assert np.allclose(separation/np.linalg.norm(separation),[0,-1,0],atol=2e-5)
        drive=joints['kuavo_tool_gripper_drive_'+letter]
        assert model['display_joint_defaults'][drive['name']]==.5*float(drive['limit']['upper'])
    rebuilt=deepcopy(model)
    attach_tools(rebuilt)
    assert rebuilt==model, 'Repeated rebuild must not duplicate or shift the tools'


def test_exported_urdf_matches_display_assembly_and_uses_local_meshes():
    assets=ROOT/'teleop_control/assets/kuavo'
    model=json.loads((ROOT/'public/kuavo-model.json').read_text())
    manifest=json.loads((assets/'visual-manifest.json').read_text())['assembly_urdf']
    path=ROOT/manifest['path']
    assert hashlib.sha256(path.read_bytes()).hexdigest()==manifest['sha256']
    robot=ET.parse(path).getroot()
    assert not robot.findall('.//inertial') and not robot.findall('.//collision')
    links={n.get('name'):n for n in robot.findall('link')}
    assert set(links)=={l['name'] for l in model['links']}
    for link in model['links']:
        visuals=links[link['name']].findall('visual')
        assert len(visuals)==len(link['visuals'])
        for actual,expected in zip(visuals,link['visuals']):
            mesh=actual.find('geometry/mesh');file=path.parent/mesh.get('filename')
            assert file.resolve().is_relative_to(assets.resolve()) and file.is_file()
            assert hashlib.sha256(file.read_bytes()).hexdigest()==manifest['meshes'][file.name]
            lines=file.read_text().splitlines()
            source=model['meshes'][expected['mesh']]
            assert sum(l.startswith('v ') for l in lines)==len(source['vertices'])
            assert sum(l.startswith('vn ') for l in lines)==len(source['normals'])
            assert sum(l.startswith('f ') for l in lines)==len(source['faces'])
            for key in ('xyz','rpy'):
                assert np.allclose([float(v) for v in actual.find('origin').get(key).split()],expected[key])
    joints={n.get('name'):n for n in robot.findall('joint')}
    assert set(joints)=={j['name'] for j in model['joints']}
    for expected in model['joints']:
        actual=joints[expected['name']]
        assert actual.get('type')==expected['type']
        for key in ('parent','child'):assert actual.find(key).get('link')==expected[key]
        for key in ('xyz','rpy'):
            assert np.allclose([float(v) for v in actual.find('origin').get(key).split()],expected[key])
        if expected['mimic']:assert actual.find('mimic').attrib==expected['mimic']
        if expected['limit']:assert actual.find('limit').attrib==expected['limit']
    assert manifest['default_joint_positions']==model['display_joint_defaults']


def test_smooth_lighting_keeps_sharp_edges_and_geometry():
    from copy import deepcopy
    from kuavo_mesh_quality import smooth_normals
    # Two coplanar triangles and a perpendicular wall sharing an edge.
    mesh=dict(vertices=[[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1]],
              faces=[[0,1,2],[0,2,3],[1,0,4],[1,4,5]])
    before=deepcopy(mesh);smooth_normals(mesh)
    assert mesh['vertices']==before['vertices'] and mesh['faces']==before['faces']
    normals=np.array(mesh['normals'])[mesh['normal_indices']]
    assert np.allclose(normals[:2],[0,0,1])
    assert np.allclose(normals[2:],[0,1,0])
    # A shallow bend should blend across the edge instead of exposing triangles.
    mesh=dict(vertices=[[0,0,0],[1,0,0],[1,1,0],[0,1,.2]],faces=[[0,1,2],[0,2,3]])
    smooth_normals(mesh);normals=np.array(mesh['normals'])[mesh['normal_indices']]
    assert np.allclose(normals[0,0],normals[1,0])
    assert not np.allclose(normals[0,0],[0,0,1])


def test_kuavo_tcp_frames_reuse_authoritative_tianji_urdf():
    model=json.loads((ROOT/'public/kuavo-model.json').read_text())
    joints={j['name']:j for j in model['joints']}
    for side,letter in [('left','L'),('right','R')]:
        metadata=model['tool_attachment']['tcp_frames'][side]
        source=ROOT/metadata['source']
        assert hashlib.sha256(source.read_bytes()).hexdigest()==metadata['sha256']
        src=next(j for j in ET.parse(source).getroot().findall('joint') if j.get('name')=='grasp_center_fixed_'+letter)
        actual=joints['kuavo_tool_grasp_center_fixed_'+letter]
        assert actual['type']=='fixed' and actual['mimic'] is None
        assert actual['parent']=='kuavo_tool_gripper_'+letter
        assert actual['child']==metadata['link']
        for field in ('xyz','rpy'):
            assert actual[field]==[float(v) for v in src.find('origin').get(field).split()]
        assert actual['xyz']==[0,0,.142225549599]
