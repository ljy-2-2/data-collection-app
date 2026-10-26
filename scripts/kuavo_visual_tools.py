#!/usr/bin/env python3
"""Assemble local flange/gripper visuals; never alter robot control geometry.

After editing assets/kuavo/gripper_mounts.json, run this script to regenerate
public/kuavo-model.json using only assets already inside the App directory.
The full official-model builder calls attach_tools() too.
"""
from copy import deepcopy
import hashlib
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from kuavo_mesh_quality import prepare_normals
from kuavo_urdf_export import export_urdf

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'kuavo_tool_'


def fixed_joint(name, parent, child, placement):
    xyz, rpy = placement['translation_mm'], placement['rpy_deg']
    if len(xyz) != 3 or len(rpy) != 3 or not all(math.isfinite(v) for v in xyz+rpy):
        raise ValueError('Invalid visual mount transform')
    return dict(name=name, type='fixed', parent=parent, child=child,
                xyz=[v/1000 for v in xyz], rpy=[math.radians(v) for v in rpy],
                axis=[0, 0, 1], mimic=None, limit=None)


def attach_tools(model, root=ROOT):
    assets = root/'teleop_control/assets/kuavo'
    config_bytes = (assets/'gripper_mounts.json').read_bytes()
    source_bytes = (root/'public/tianji-model.json').read_bytes()
    flange_bytes = (assets/'flange/display-mesh.json').read_bytes()
    config, source, flange = map(json.loads, (config_bytes, source_bytes, flange_bytes))
    fraction = config['display_open_fraction']
    if not math.isfinite(fraction) or not 0 <= fraction <= 1:
        raise ValueError('Display gripper opening must be between 0 and 1')
    # Idempotent: regenerate only our tool subtree, preserving all robot parts.
    model['links'] = [v for v in model['links'] if not v['name'].startswith(PREFIX)]
    model['joints'] = [v for v in model['joints'] if not v['name'].startswith(PREFIX)]
    model['meshes'] = {k: v for k, v in model['meshes'].items() if not k.startswith(PREFIX)}
    model['display_joint_defaults'] = {k: v for k, v in model.get('display_joint_defaults', {}).items() if not k.startswith(PREFIX)}
    body_triangles = sum(len(model['meshes'][v['mesh']]['faces']) for l in model['links'] for v in l['visuals'])
    model['meshes'][PREFIX+'flange'] = flange
    excluded = {'L': 'm54', 'R': 'm116'}
    tcp_frames = {}
    for side, letter, short in [('left', 'L', 'l'), ('right', 'R', 'r')]:
        flange_link = PREFIX+'flange_'+short
        model['links'].append(dict(name=flange_link, visuals=[dict(
            mesh=PREFIX+'flange', xyz=[0, 0, 0], rpy=[0, 0, 0],
            scale=[1, 1, 1], color=[.67, .7, .74, 1])]))
        model['joints'].append(fixed_joint(PREFIX+'flange_mount_'+short,
            f'zarm_{short}7_link', flange_link, config[side]['flange_from_wrist']))
        root_link = 'gripper_'+letter
        source_names = {root_link, root_link+'_shaft', root_link+'_1', root_link+'_17'}
        links = [l for l in source['links'] if l['name'] in source_names]
        assert len(links) == 4
        assert any(v['mesh'] == excluded[letter] for l in links for v in l['visuals']), 'Source adapter identity changed'
        for original in links:
            link = deepcopy(original)
            link['name'] = PREFIX+link['name']
            link['visuals'] = [v for v in link['visuals'] if v['mesh'] != excluded[letter]]
            for visual in link['visuals']:
                key = visual['mesh']
                model['meshes'][PREFIX+key] = deepcopy(source['meshes'][key])
                visual['mesh'] = PREFIX+key
            model['links'].append(link)
        model['joints'].append(fixed_joint(PREFIX+'gripper_mount_'+letter,
            flange_link, PREFIX+root_link, config[side]['gripper_from_flange']))
        for original in source['joints']:
            if original['parent'] not in source_names or original['child'] not in source_names:continue
            joint = deepcopy(original)
            for field in ('name', 'parent', 'child'):joint[field] = PREFIX+joint[field]
            if joint['mimic']:joint['mimic']['joint'] = PREFIX+joint['mimic']['joint']
            model['joints'].append(joint)
            if original['name'] == 'gripper_drive_'+letter:
                model['display_joint_defaults'][joint['name']] = (1-fraction)*float(joint['limit']['upper'])
        # Reuse the authoritative Tianji nominal fixed TCP in the identical gripper.
        candidates=list((root/'teleop_control/vendor/tianji').glob(f'*/urdf/marvin_{side}_with_gripper.urdf'))
        if len(candidates)!=1:raise ValueError(f'Ambiguous Tianji TCP source for {side}')
        tcp_source=candidates[0];tree=ET.parse(tcp_source).getroot()
        src=next(j for j in tree.findall('joint') if j.get('name')=='grasp_center_fixed_'+letter)
        assert src.get('type')=='fixed' and src.find('parent').get('link')==root_link
        origin=src.find('origin');tcp_name=PREFIX+'grasp_center_'+letter
        joint=dict(name=PREFIX+'grasp_center_fixed_'+letter,type='fixed',parent=PREFIX+root_link,
                   child=tcp_name,xyz=[float(v) for v in origin.get('xyz').split()],
                   rpy=[float(v) for v in origin.get('rpy').split()],axis=[0,0,1],mimic=None,limit=None)
        model['links'].append(dict(name=tcp_name,visuals=[]));model['joints'].append(joint)
        tcp_frames[side]=dict(link=tcp_name,parent=joint['parent'],xyz_m=joint['xyz'],rpy_rad=joint['rpy'],
                             source=str(tcp_source.relative_to(root)),sha256=hashlib.sha256(tcp_source.read_bytes()).hexdigest(),
                             definition='Nominal fixed motor-axis / jaw-pivot-plane intersection; not physical TCP calibration.')
    metadata = dict(status='provisional_holes_aligned',
        tcp_frames=tcp_frames,
        config='teleop_control/assets/kuavo/gripper_mounts.json',
        config_sha256=hashlib.sha256(config_bytes).hexdigest(),
        tianji_source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        flange_mesh_sha256=hashlib.sha256(flange_bytes).hexdigest(),
        excluded_tianji_adapters=list(excluded.values()),
        opening='Static display reference only; not measured gripper feedback.',
        body_display_triangles=body_triangles,
        attachment_display_triangles=sum(len(model['meshes'][v['mesh']]['faces']) for l in model['links'] if l['name'].startswith(PREFIX) for v in l['visuals']))
    model['tool_attachment'] = metadata
    prepare_normals(model)
    return metadata


def write_model(model, manifest, root=ROOT):
    target = root/'public/kuavo-model.json'
    target.write_text(json.dumps(model, separators=(',', ':'))+'\n')
    manifest['assembly_urdf']=export_urdf(model,root)
    manifest.update(display_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                    display_bytes=target.stat().st_size, tool_attachment=model['tool_attachment'],
                    display_triangles=sum(len(model['meshes'][v['mesh']]['faces']) for l in model['links'] for v in l['visuals']))
    (root/'teleop_control/assets/kuavo/visual-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')


if __name__ == '__main__':
    model = json.loads((ROOT/'public/kuavo-model.json').read_text())
    manifest = json.loads((ROOT/'teleop_control/assets/kuavo/visual-manifest.json').read_text())
    attach_tools(model)
    write_model(model, manifest)
    print(json.dumps(model['tool_attachment'], indent=2))
