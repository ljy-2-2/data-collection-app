"""Extract current part geometry and saved assembly placements from a hashed snapshot.

No CAD rebuild or robot connection. Mesh coordinates and placements are metres.
Only exact native/mesh SHA256 matches may reuse a verified graphics-only mesh.
"""
from pathlib import Path, PureWindowsPath
import argparse, hashlib, json, xml.etree.ElementTree as ET
import numpy as np
import trimesh
from cad_saved_mesh import streams, part_mesh
NS={'s':'http://www.solidworks.com/sw2003/schema'}
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def extract(snapshot, output, verified_scene):
    output.mkdir(parents=True,exist_ok=True);(output/'meshes').mkdir(exist_ok=True)
    records=json.loads((snapshot/'source_files.json').read_text(encoding='utf-8-sig'))
    records=[r for r in records if Path(r['file']).suffix.lower() in ('.sldprt','.sldasm')]
    by_file={r['file']:r for r in records};source=snapshot/'source'
    for rel,r in by_file.items():
        if sha(source/rel)!=r['sha256']:raise ValueError('Snapshot hash mismatch: '+rel)
    previous=json.loads(verified_scene.read_text());exported={};assemblies=[]
    for file in sorted(source.rglob('*.SLDASM')):
        index=2 if '电机版' in file.parts else (0 if '右手' in file.parts else 1)
        baseline=previous['assemblies'][index]
        old={p['cad_instance_name']:p for p in baseline['parts']}
        tree=ET.fromstring(streams(file)['swXmlContents/COMPINSTANCETREE'])
        models={m.get('id'):m for m in tree.findall('.//s:swModel',NS)}
        files={m.get('id'):m for m in tree.findall('.//s:swFile',NS)}
        root=next(m for m in models.values() if m.get('swName')==file.stem);components=[]
        for ref in root.findall('s:swReference',NS):
            if ref.get('swSuppressed')=='YES':continue
            model=models[ref.get('swModelRef')]
            if model.findall('s:swReference',NS):raise ValueError('Nested assembly requires explicit registration')
            remote=PureWindowsPath(files[model.get('swFileRef')].get('swPath'))
            at=next(i for i,p in enumerate(remote.parts) if p.lower()=='end_effector')
            rel='/'.join(remote.parts[at+1:]);path=source/rel
            if rel not in by_file:raise ValueError('Missing dependency: '+rel)
            name=ref.get('swName')+'-'+ref.get('swReferenceNumber');prior=old[name]
            T=np.fromstring(ref.get('swTransform'),sep=' ').reshape(4,4).T
            if not np.allclose(T[:3,:3].T@T[:3,:3],np.eye(3),atol=1e-9):raise ValueError('Nonrigid transform')
            oldT=np.array(prior['transform']);oldT[:3,3]/=1000
            if prior.get('motion') and not np.allclose(T,oldT,atol=1e-8):
                raise ValueError('Moving part placement changed; recompute its hinge: '+name)
            if rel not in exported:
                method='latest native saved display tessellation'
                try:mesh=part_mesh(path)
                except ValueError:
                    if prior.get('source_native',{}).get('sha256')!=by_file[rel]['sha256']:raise
                    mesh_path=Path(prior['mesh'])
                    if sha(mesh_path)!=prior.get('graphics_mesh_sha256'):raise
                    mesh=trimesh.load(mesh_path,force='mesh');mesh.apply_scale(.001)
                    method='verified graphics mesh; native and mesh hashes both match'
                if not len(mesh.faces) or not np.isfinite(mesh.vertices).all():raise ValueError('Invalid mesh')
                mesh_file=output/'meshes'/f'part_{len(exported):03d}.stl';mesh.export(mesh_file)
                exported[rel]=dict(file=rel,sha256=by_file[rel]['sha256'],mesh=str(mesh_file.resolve()),
                    mesh_sha256=sha(mesh_file),triangles=len(mesh.faces),bounds_m=mesh.bounds.tolist(),geometry_method=method)
                print('Extracted',path.name,len(mesh.faces),flush=True)
            c=dict(exported[rel],name=name,transform=T.tolist(),color=prior['color'][:3]+[1],
                   group=prior.get('group'),motion=prior.get('motion'),hidden=ref.get('swHidden')=='YES')
            components.append(c)
        if len(components)!=len(baseline['parts']):raise ValueError('Component list changed')
        assemblies.append(dict(kind=['right','left','motor'][index],file=str(file.relative_to(source)),
                               sha256=sha(file),configuration=root.get('swConfigurationName'),components=components,
                               design_datum=baseline['datum_frame']))
    result=dict(units='m',source='Windows Desktop/end_effector',files=records,assemblies=assemblies)
    (output/'native_manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot','output','verified-scene'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();extract(a.snapshot.resolve(),a.output.resolve(),a.verified_scene.resolve())
