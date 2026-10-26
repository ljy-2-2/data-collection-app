"""Export the displayed Kuavo assembly as a portable visual/kinematic URDF.

Nominal gripper TCPs are included; no invented inertias or collision bodies. Arm joint
origins/axes/limits and the copied gripper mimic joints are retained.
"""
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET


def vector(values):return ' '.join(format(float(v),'.12g') for v in values)


def export_urdf(model, root):
    assets=root/'teleop_control/assets/kuavo'
    directory=assets/'gripper_model_meshes';directory.mkdir(exist_ok=True)
    names={key:f'mesh_{i:03}.obj' for i,key in enumerate(model['meshes'])}
    mesh_hashes={}
    for key,mesh in model['meshes'].items():
        path=directory/names[key]
        with path.open('w') as output:
            output.write('# Kuavo display assembly; coordinates in metres\n')
            for p in mesh['vertices']:output.write('v '+vector(p)+'\n')
            for n in mesh['normals']:output.write('vn '+vector(n)+'\n')
            for face,normals in zip(mesh['faces'],mesh['normal_indices']):
                output.write('f '+' '.join(f'{v+1}//{n+1}' for v,n in zip(face,normals))+'\n')
        mesh_hashes[names[key]]=hashlib.sha256(path.read_bytes()).hexdigest()
    robot=ET.Element('robot',name='kuavo_5w_with_grippers')
    robot.append(ET.Comment(' Visual/kinematic assembly with nominal fixed grasp-center TCPs. No calibrated mass, collision model or physical TCP calibration. '))
    robot.append(ET.Comment(' Generated from public/kuavo-model.json; mount settings: gripper_mounts.json. '))
    for item in model['links']:
        link=ET.SubElement(robot,'link',name=item['name'])
        for index,visual in enumerate(item['visuals']):
            node=ET.SubElement(link,'visual',name=f"{item['name']}_visual_{index}")
            ET.SubElement(node,'origin',xyz=vector(visual['xyz']),rpy=vector(visual['rpy']))
            geometry=ET.SubElement(node,'geometry')
            ET.SubElement(geometry,'mesh',filename='gripper_model_meshes/'+names[visual['mesh']],scale=vector(visual['scale']))
            material=ET.SubElement(node,'material',name=f"{item['name']}_material_{index}")
            ET.SubElement(material,'color',rgba=vector(visual['color']))
    for item in model['joints']:
        joint=ET.SubElement(robot,'joint',name=item['name'],type=item['type'])
        ET.SubElement(joint,'origin',xyz=vector(item['xyz']),rpy=vector(item['rpy']))
        ET.SubElement(joint,'parent',link=item['parent']);ET.SubElement(joint,'child',link=item['child'])
        if item['type']!='fixed':ET.SubElement(joint,'axis',xyz=vector(item['axis']))
        if item['limit']:ET.SubElement(joint,'limit',**{k:str(v) for k,v in item['limit'].items()})
        if item['mimic']:ET.SubElement(joint,'mimic',**{k:str(v) for k,v in item['mimic'].items()})
    ET.indent(robot,space='  ')
    target=assets/'full_robot_with_grippers.urdf'
    ET.ElementTree(robot).write(target,encoding='utf-8',xml_declaration=True)
    return dict(path=str(target.relative_to(root)),sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                purpose='visual_and_kinematic_only',meshes=mesh_hashes,
                tcp_frames=model['tool_attachment']['tcp_frames'],
                default_joint_positions=model.get('display_joint_defaults',{}))
