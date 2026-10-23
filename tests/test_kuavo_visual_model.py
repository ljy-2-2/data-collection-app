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
    assert model['root_link']=='base_link' and len(model['meshes'])==29
    assert manifest['display_bytes']<4_000_000
    assert all(v['bounds_change_m']<.005 for v in manifest['files'].values())
    assert 'Leju' in (ROOT/'public/kuavo-model.LICENSE').read_text()
    original={j.get('name'):j for j in ET.parse(urdf).getroot().findall('joint')}
    for joint in model['joints']:
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
    assert all(v['mesh'] in model['meshes'] for l in model['links'] for v in l['visuals'])


def test_web_and_qt_models_agree_for_feedback_and_tianji_defaults(tmp_path):
    # TypeScript math is executed with Node; Qt geometry needs no OpenGL context.
    node=r'''
import fs from 'node:fs'; import ts from 'typescript';
const src=fs.readFileSync('lib/tianjiModel.ts','utf8');
const js=ts.transpileModule(src,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const math=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
const model=JSON.parse(fs.readFileSync('public/kuavo-model.json','utf8'));
const angles={zarm_l1_joint:-.6,zarm_l4_joint:-.8};
const links=math.linkTransforms(model,angles);
const t=JSON.parse(fs.readFileSync('public/tianji-model.json','utf8'));
const tianji=math.linkTransforms(t,{Joint1_L:Math.PI/6});
console.log(JSON.stringify({links,tianji}));
'''
    result=subprocess.run(['node','--input-type=module','-e',node],cwd=ROOT,text=True,capture_output=True,check=True)
    expected=tmp_path/'transforms.json';expected.write_text(result.stdout)
    script=r'''
import json, numpy as np
from pathlib import Path
from PySide6.QtGui import QGuiApplication
from qt_ui.visuals import KuavoScene,RobotScene,transform
app=QGuiApplication([])
expected=json.loads(Path(__import__('sys').argv[1]).read_text())
k=KuavoScene(Path.cwd());t=RobotScene(Path.cwd())
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
t.update_pose({'joints':{'left':[30,0,0,0,0,0,0]}})
for name,part in t.by_link:
    if name.startswith('gripper'):continue
    matrix=np.array(expected['tianji'].get(name,np.eye(4).reshape(-1).tolist())).reshape(4,4)@transform(part.visual['xyz'],part.visual['rpy'])
    # Gripper links use their original calibration defaults; compare only arm links.
    if name in ('stand','Link1_L','Link2_L','Link3_L','Link1_R'):assert np.allclose(part.matrix,matrix)
assert len(k.parts)==29
'''
    env={**os.environ,'QT_QPA_PLATFORM':'offscreen','QT_QUICK_BACKEND':'software',
         'PYTHONPATH':str(ROOT/'scripts')+os.pathsep+os.environ.get('PYTHONPATH','')}
    result=subprocess.run([sys.executable,'-c',script,str(expected)],cwd=ROOT,env=env,text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
