"""Update App geometry/frames from an inspected Windows native snapshot.

Input is import_windows_end_effector.py's manifest. Preserve robot arm geometry,
mount clocking, motor travel, encoder calibration and user-selected Pico datum.
The 2026-11-17 accepted flange mounting is stored in the active URDFs:
left yaw 0, right yaw -pi/2 (clockwise 90 degrees from the original mounting,
viewed from wrist toward fingertips). Preserve it during native CAD updates.
"""
from pathlib import Path
import argparse, copy, hashlib, json, math, re, warnings
import xml.etree.ElementTree as ET
import numpy as np
import trimesh
from scipy.spatial.transform import Rotation

ROOT=Path(__file__).resolve().parents[1]
GAL=ROOT/'umi_collection/vendor/galaxis'
VENDOR=ROOT/'teleop_control/vendor/tianji'
NS='http://www.ros.org/wiki/xacro'
ET.register_namespace('xacro',NS)
def vec(x):return ' '.join(f'{v:.12g}' for v in np.asarray(x).ravel())
def pose(xyz=(0,0,0),rpy=(0,0,0)):
    t=np.eye(4);t[:3,3]=xyz;t[:3,:3]=Rotation.from_euler('xyz',rpy).as_matrix();return t
def rpy(t):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore');v=Rotation.from_matrix(t[:3,:3]).as_euler('xyz')
    v[np.abs(v)<1e-10]=0
    return v
def origin(node,t):node.set('xyz',vec(t[:3,3]));node.set('rpy',vec(rpy(t)))
def dump(p,d):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
def save_xml(p,root):ET.indent(root,space='  ');ET.ElementTree(root).write(p,encoding='utf-8',xml_declaration=True)
def transform_mesh(mesh,t):m=mesh.copy();m.apply_transform(t);return m
def compact(mesh,limit,split=True):
    m=mesh.copy();m.merge_vertices();m.update_faces(m.nondegenerate_faces());m.remove_unreferenced_vertices()
    if split and len(m.faces)>100000:
        parts=m.split(only_watertight=False)
        if len(parts)>1:
            return trimesh.util.concatenate([compact(p,max(100,int(limit*len(p.faces)/len(m.faces))),False) for p in parts])
    target=limit
    while len(m.faces)>target:
        candidate=m.simplify_quadric_decimation(face_count=target)
        if np.max(abs(candidate.bounds-m.bounds))<.00025:
            m=candidate;break
        target*=2
    return m

def pad_contact_angle(components,meshes,pivots):
    pad=next(c for c in components if '脚垫' in c['name'] and c.get('motion') and c['motion']['origin_mm'][0]>0)
    vertices=transform_mesh(meshes[pad['sha256']],pad['transform']).vertices
    pivot=np.array(pivots['right'])
    def clearance(angle):
        return (pivot[0]+(vertices[:,0]-pivot[0])*np.cos(angle)-(vertices[:,1]-pivot[1])*np.sin(angle)).min()
    lo,hi=0.,.5
    if not clearance(lo)>0>clearance(hi):raise ValueError('Cannot determine mirrored pad contact reference')
    for _ in range(50):
        mid=(lo+hi)/2
        if clearance(mid)>0:lo=mid
        else:hi=mid
    return (lo+hi)/2

def prepare(manifest,history,old_properties):
    """Reconstruct frames; retain source transforms for independent checks."""
    models={a['kind']:a for a in manifest['assemblies']};meshes={}
    for a in models.values():
        for c in a['components']:
            if c['sha256'] not in meshes:meshes[c['sha256']]=trimesh.load(c['mesh'],force='mesh')
    old_root_from_pico=np.linalg.inv(history['assembly_from_hand_root'])@history['pico_ray_reference']['assembly_from_mounted_controller']
    old_mount=pose([old_properties['mount_'+s] for s in 'xyz'],[old_properties['mount_'+s] for s in ('roll','pitch','yaw')])
    pico_local=np.linalg.inv(old_root_from_pico)@old_mount
    report={'units':'m/rad','source_files':manifest['files'],'source':'Windows Desktop/end_effector',
            'old_mount_in_original_root':old_mount.tolist(),'pico_local_reference':pico_local.tolist(),
            'pico_definition':'Previously user-confirmed Quest-mapped point and attached frame, carried with current Pico component. Not SDK calibration.',
            'old_properties':old_properties,'assemblies':{}}
    B=np.array([[0,-1,0],[1,0,0],[0,0,1]])
    for kind,a in models.items():
        cs=a['components'];camera=next(c for c in cs if '_摄像头-' in c['name'])
        H=np.eye(4);H[:3,:3]=B
        if kind!='motor':
            gear=next(c for c in cs if '夹指齿轮A-' in c['name']);v=transform_mesh(meshes[gear['sha256']],gear['transform']).vertices
            pivot=np.array(gear['motion']['origin_mm'])/1000
            radius=np.linalg.norm(v[:,:2]-pivot[:2],axis=1)
            tooth=v[(radius>.017)&(radius<.022)&(v[:,1]<-.0001)]
            levels,counts=np.unique(np.round(tooth[:,2],6),return_counts=True)
            planes=np.sort(levels[np.argsort(counts)[-2:]])
            if not np.isclose(planes[1]-planes[0],.010,atol=1e-5):raise ValueError('Hand gear thickness changed')
            H[2,3]=planes.mean()
        else:
            # Output-axis intersection with the plane containing the two jaw axes (Y=0).
            shaft=next(c for c in cs if '中央齿轮轴-' in c['name']);S=np.array(shaft['transform']);v=S[:3,0]
            point=S[:3,3]-S[1,3]/v[1]*v
            if abs(point[0])>1e-7:raise ValueError('Motor output axis not centred')
            H[:3,3]=point
        A=np.linalg.inv(H);C=np.array(camera['transform'])
        camera_pose=np.eye(4);camera_pose[:3,3]=(A@C@[0,0,.023,1])[:3]
        camera_pose[:3,:3]=(A@C)[:3,[2,0,1]]
        pivots={}
        for c in cs:
            if '夹指齿轮' not in c['name']:continue
            pivot=np.array(c['motion']['origin_mm'])/1000;pivot[2]=H[2,3]
            pts=transform_mesh(meshes[c['sha256']],c['transform']).vertices
            radial=np.linalg.norm(pts[:,:2]-pivot[:2],axis=1)
            ring=pts[np.abs(radial-(.00315 if kind=='motor' else .0035))<.000015,:2]
            if len(ring)<20:raise ValueError('Cannot verify shaft hole '+c['name'])
            fitted=np.linalg.lstsq(np.column_stack([2*ring,np.ones(len(ring))]),(ring*ring).sum(1),rcond=None)[0][:2]
            if np.linalg.norm(fitted-pivot[:2])>.00003:raise ValueError('Shaft centre differs from design reference')
            pivots['left' if (A@np.r_[pivot,1])[1]>0 else 'right']=pivot
        span=float(np.linalg.norm(pivots['left']-pivots['right']))
        data=dict(file=a['file'],sha256=a['sha256'],assembly_from_root=H.tolist(),
                  camera_component_transform=C.tolist(),camera_reference_local_m=[0,0,.023],camera_pose=camera_pose.tolist(),
                  camera_xyz_m=camera_pose[:3,3].tolist(),camera_rpy_rad=rpy(camera_pose).tolist(),
                  jaw_pivots_assembly_m={k:v.tolist() for k,v in pivots.items()},shaft_spacing_m=span)
        if kind!='motor':
            pc=next(c for c in cs if 'Pico控制器-' in c['name']);local=pico_local.copy()
            if kind=='left':local=np.diag([-1,1,1,1])@local@np.diag([1,-1,1,1])
            mounted=A@pc['transform']@local
            data.update(pico_component_transform=pc['transform'],pico_pose=mounted.tolist(),
                        pico_xyz_m=mounted[:3,3].tolist(),pico_rpy_rad=rpy(mounted).tolist())
        else:
            flange=next(c for c in cs if '连接法兰-' in c['name']);F=np.array(flange['transform'])
            data.update(assembly_from_flange=F.tolist(),flange_from_root=(np.linalg.inv(F)@H).tolist(),flange_file=flange['file'])
        data['pad_contact_angle_from_native_rad']=pad_contact_angle(cs,meshes,data['jaw_pivots_assembly_m'])
        data['jaw_zero_definition']='Visual closed reference: first TPU contact at symmetry plane; not measured actuator calibration.'
        a['measurement']=data;report['assemblies'][kind]=data
    S=np.diag([1,-1,1,1])
    for key in ('pico_pose','camera_pose'):
        if not np.allclose(models['left']['measurement'][key],S@models['right']['measurement'][key]@S,atol=1e-8):
            raise ValueError('Left/right frame mirror mismatch: '+key)
    return models,meshes,report

def part_role(c,root_inverse):
    if '_摄像头-' in c['name']:return 'camera'
    if c.get('motion'):
        p=np.r_[np.array(c['motion']['origin_mm'])/1000,1]
        return 'left_finger' if (root_inverse@p)[1]>0 else 'right_finger'
    return 'root'

def update_hand(models,meshes,report):
    desc=GAL/'galaxis_description';mesh_dir=desc/'meshes/windows';mesh_dir.mkdir(parents=True,exist_ok=True)
    xml=ET.Element('robot');macro=ET.SubElement(xml,'{'+NS+'}macro',name='windows_hand_visuals',params='hand_prefix role')
    exported={};report['hand_visual_parts']=[]
    for hand in ('right','left'):
        a=models[hand];d=a['measurement'];A=np.linalg.inv(d['assembly_from_root'])
        for i,c in enumerate(a['components']):
            if c['hidden']:continue
            key=c['sha256']
            if key not in exported:
                name=f'{hand}_{i:02d}.stl';limit=22000 if '脚垫' in c['name'] else (16000 if 'Pico' in c['name'] else 6500)
                compact(meshes[key],limit).export(mesh_dir/name);exported[key]=name
            role=part_role(c,A);link=np.eye(4)
            if role=='camera':link=np.array(d['camera_pose'])
            elif role.endswith('_finger'):link[:3,3]=(A@np.r_[d['jaw_pivots_assembly_m'][role.split('_')[0]],1])[:3]
            T=np.linalg.inv(link)@A@c['transform']
            if role.endswith('_finger'):
                # Encoder q=0 is calibrated closed. Native CAD is partly open.
                sign=1 if role=='right_finger' else -1
                T=pose(rpy=(0,0,sign*d['pad_contact_angle_from_native_rad']))@T
            report['hand_visual_parts'].append(dict(hand=hand,role=role,name=c['name'],visual=f'windows_{hand}_{i:02d}',mesh=exported[key],native_sha256=c['sha256']))
            cond=ET.SubElement(macro,'{'+NS+'}if',value="${hand_prefix == '"+hand+"' and role == '"+role+"'}")
            visual=ET.SubElement(cond,'visual',name=f'windows_{hand}_{i:02d}')
            origin(ET.SubElement(visual,'origin'),T)
            ET.SubElement(ET.SubElement(visual,'geometry'),'mesh',filename='package://galaxis_description/meshes/windows/'+exported[key])
            ET.SubElement(ET.SubElement(visual,'material',name=f'windows_{hand}_{i:02d}'),'color',rgba=vec(c['color']))
    save_xml(desc/'urdf/galaxis_hand_visuals.xacro',xml)
    source=desc/'urdf/galaxis_hand.urdf.xacro';s=source.read_text();right=report['assemblies']['right']
    values={'finger_offset_y':right['shaft_spacing_m']/2}
    for prefix,xyz,angles in [('camera',right['camera_xyz_m'],right['camera_rpy_rad']),('mount',right['pico_xyz_m'],right['pico_rpy_rad'])]:
        values.update({prefix+'_'+k:v for k,v in zip('xyz',xyz)})
        values.update({prefix+'_'+k:v for k,v in zip(('roll','pitch','yaw'),angles)})
    for name,value in values.items():
        s,n=re.subn(r'(<xacro:property name="'+name+r'" value=")[^"]+("/>)',lambda m:m[1]+f'{value:.12g}'+m[2],s)
        if n!=1:raise ValueError('Property missing '+name)
    start=s.index('  <!--');end=s.index('  <!-- JointState')
    properties='\n'.join(re.findall(r'  <xacro:property[^\n]+',s[start:end]))
    header='''  <!-- Windows Desktop/end_effector current native snapshot, 2026-11-16.
       Units m/rad; root = jaw-axis midpoint at the gear mid-plane.
       +X forward, +Y left, +Z up. Native left/right geometry is retained.
       Camera frame = measured lens-front mechanical reference (not calibrated
       optical centre). Pico is the user-confirmed physical point/frame carried
       with its new mounting; it is not an independently calibrated SDK origin.
       Finger tip = nominal closed TPU contact at the gear mid-plane (2026-11-17).
       This is a CAD reference, not physical contact/encoder calibration.
       See docs/windows_end_effector_measurements.json for transforms and hashes. -->
  <xacro:include filename="galaxis_hand_visuals.xacro"/>
'''
    s=s[:start]+header+properties+'\n\n'+s[end:]
    for link,role in [('${hand_prefix}_hand_root','root'),('${hand_prefix}_hand_cam_link','camera'),('${hand_prefix}_${finger_side}_finger_link','${finger_side}_finger')]:
        old='<link name="'+link+'"/>';new='<link name="'+link+'">\n      <xacro:windows_hand_visuals hand_prefix="${hand_prefix}" role="'+role+'"/>\n    </link>'
        if old in s:s=s.replace(old,new)
        elif new not in s:raise ValueError('Unexpected visual link '+link)
    source.write_text(s);report['new_hand_properties']=values

def update_motor(models,meshes,report):
    a=models['motor'];d=a['measurement'];F=np.array(d['assembly_from_flange']);A=np.linalg.inv(F)
    # Native CAD is saved partially open. Derive a visual closed reference from
    # the mirrored TPU pads' first contact with the symmetry plane; keep the
    # existing drive stroke/gear ratio, never rewrite hardware endpoint values.
    closing_angle=d['pad_contact_angle_from_native_rad']
    public=ROOT/'public/tianji-model.json';display=json.loads(public.read_text())
    old_keys={v['mesh'] for l in display['links'] if l['name'].startswith('gripper_') for v in l['visuals']}
    links={l['name']:l for l in display['links']};joints={j['name']:j for j in display['joints']}
    artifacts=[]
    for side,letter,flange_key in [('left','L','m54'),('right','R','m116')]:
        path=next(VENDOR.glob(f'*/urdf/marvin_{side}_with_gripper.urdf'));tree=ET.parse(path);root=tree.getroot();mesh_dir=path.parent.parent/'meshes/gripper'
        old_parts=list(mesh_dir.glob('part_*.stl'))
        for suffix in ('','_shaft','_1','_17'):
            name='gripper_'+letter+suffix;node=root.find(f"link[@name='{name}']")
            for v in list(node.findall('visual'))+list(node.findall('collision')):node.remove(v)
            links[name]['visuals']=[]
        frames={'root':np.eye(4)}
        for native_side,role,num in [('left','jaw1','1'),('right','jaw17','17')]:
            t=np.eye(4);t[:3,3]=(A@np.r_[d['jaw_pivots_assembly_m'][native_side],1])[:3];frames[role]=t
            name=f'gripper_jaw_{num}_{letter}';node=root.find(f"joint[@name='{name}']");origin(node.find('origin'),t)
            axis=A[:3,:3]@[0,0,1];node.find('axis').set('xyz',vec(axis))
            joints[name].update(xyz=t[:3,3].tolist(),rpy=[0,0,0],axis=axis.tolist())
            ratio=abs(float(node.find('mimic').get('multiplier')))
            drive_max=float(root.find(f"joint[@name='gripper_drive_{letter}']/limit").get('upper'))
            sign=-1 if native_side=='left' else 1
            multiplier=sign*ratio;offset=sign*(closing_angle-ratio*drive_max)
            node.find('mimic').set('multiplier',str(multiplier));node.find('mimic').set('offset',str(offset))
            lower,upper=sorted([offset,offset+multiplier*drive_max])
            node.find('limit').set('lower',str(lower));node.find('limit').set('upper',str(upper))
            joints[name]['mimic'].update(multiplier=str(multiplier),offset=str(offset))
            joints[name]['limit'].update(lower=str(lower),upper=str(upper))
        shaft=next(c for c in a['components'] if '中央齿轮轴-' in c['name']);shaftT=A@shaft['transform'];t=np.eye(4);t[:3,3]=shaftT[:3,3];frames['shaft']=t
        name=f'gripper_drive_{letter}';node=root.find(f"joint[@name='{name}']");origin(node.find('origin'),t);axis=shaftT[:3,0]
        node.find('axis').set('xyz',vec(axis));joints[name].update(xyz=t[:3,3].tolist(),rpy=[0,0,0],axis=axis.tolist())
        motor_parts=[]
        for i,c in enumerate(a['components']):
            if c['hidden']:continue
            role='root'
            if '中央齿轮轴-' in c['name']:role='shaft'
            elif c.get('motion'):role='jaw1' if c['motion']['origin_mm'][0]<0 else 'jaw17'
            suffix={'root':'','shaft':'_shaft','jaw1':'_1','jaw17':'_17'}[role];name='gripper_'+letter+suffix
            T=np.linalg.inv(frames[role])@A@c['transform'];m=transform_mesh(meshes[c['sha256']],T)
            m=compact(m,22000 if '脚垫' in c['name'] else 6500)
            filename=f'windows_{i:02d}.stl';m.export(mesh_dir/filename)
            node=root.find(f"link[@name='{name}']");visual=ET.SubElement(node,'visual',name=f'windows_{i:02d}')
            ET.SubElement(visual,'origin',xyz='0 0 0',rpy='0 0 0')
            ET.SubElement(ET.SubElement(visual,'geometry'),'mesh',filename='../meshes/gripper/'+filename)
            ET.SubElement(ET.SubElement(visual,'material',name=f'windows_{i:02d}'),'color',rgba=vec(c['color']))
            # Conservative geometric collision reference, not contact dynamics.
            if any(k in c['name'] for k in ('上壳体','下壳体','夹指齿轮','脚垫','电机外壳')):
                hull=m.convex_hull;collision_file=f'windows_{i:02d}_collision.stl';hull.export(mesh_dir/collision_file)
                col=ET.SubElement(node,'collision');ET.SubElement(col,'origin',xyz='0 0 0',rpy='0 0 0')
                ET.SubElement(ET.SubElement(col,'geometry'),'mesh',filename='../meshes/gripper/'+collision_file)
            key=flange_key if '连接法兰-' in c['name'] else f'windows_{letter}_{i:02d}'
            display['meshes'][key]=dict(vertices=np.round(m.vertices,9).tolist(),faces=m.faces.tolist())
            links[name]['visuals'].append(dict(mesh=key,xyz=[0,0,0],rpy=[0,0,0],scale=[1,1,1],color=c['color']))
            motor_parts.append(dict(index=i,name=c['name'],native_file=c['file'],native_sha256=c['sha256'],
                                    mesh=filename,link=name,link_from_part=T.tolist(),triangles=len(m.faces)))
        tcp=np.array(d['flange_from_root']);name=f'grasp_center_fixed_{letter}'
        node=root.find(f"joint[@name='{name}']");origin(node.find('origin'),tcp)
        joints[name].update(xyz=tcp[:3,3].tolist(),rpy=rpy(tcp).tolist())
        save_xml(path,root)
        for p in old_parts:p.unlink()
        detail=dict(source=a['file'],source_sha256=a['sha256'],parts=motor_parts,units='m',
                    flange_from_root=tcp.tolist(),note='Geometry and kinematics updated. Existing uncalibrated inertias and real motor endpoint calibration retained; collision meshes are conservative per-part convex hulls.')
        dump(mesh_dir/'manifest.json',detail);artifacts.append(str(path.relative_to(ROOT)))
    used={v['mesh'] for l in display['links'] for v in l['visuals']}
    for k in old_keys-used:display['meshes'].pop(k,None)
    display['end_effector_revision']='windows_native_20261116'
    display['source']='Manufacturer arm frames, current Windows end_effector geometry and derived tool frames'
    public.write_text(json.dumps(display,separators=(',',':'))+'\n')
    from sync_gripper_frames import sync_frames
    sync_frames(ROOT)
    report['updated_motor_urdfs']=artifacts

def main(args):
    manifest=json.loads(args.manifest.read_text());history=json.loads((GAL/'docs/galaxis_hand_cad_measurements.json').read_text())
    xacro=GAL/'galaxis_description/urdf/galaxis_hand.urdf.xacro'
    props={m[0]:float(m[1]) for m in re.findall(r'<xacro:property name="([^"]+)" value="([^"]+)"',xacro.read_text())}
    # The selected physical datum is defined in the ORIGINAL assembly. Reuse
    # that baseline on subsequent runs; never transform an already moved point.
    previous=GAL/'docs/windows_end_effector_measurements.json'
    if previous.exists():
        props=json.loads(previous.read_text())['old_properties']
    models,meshes,report=prepare(manifest,history,props)
    dump(args.report,report)
    if args.measure_only:return
    update_hand(models,meshes,report);update_motor(models,meshes,report)
    report['notes']=['No real robot command issued.','Motor endpoints and encoder zero unchanged.',
                     'Pico and camera frames are mechanical/user-defined, not measured tracking/optical calibration.',
                     'Saved native tessellation used; no feature rebuild.']
    dump(GAL/'docs/windows_end_effector_measurements.json',report);dump(args.report,report)
    print('Updated hand meshes/Xacro and both motor URDFs/shared display',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True);p.add_argument('--measure-only',action='store_true')
    main(p.parse_args())
