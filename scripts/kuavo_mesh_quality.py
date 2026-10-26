"""Offline crease-aware normals. Geometry and intentional sharp edges stay intact."""
import math
import numpy as np


def smooth_normals(mesh, crease_degrees=40):
    vertices=np.asarray(mesh['vertices'],dtype=float)
    faces=np.asarray(mesh['faces'],dtype=int)
    triangles=vertices[faces]
    area_normals=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])
    lengths=np.linalg.norm(area_normals,axis=1)
    unit=area_normals/np.maximum(lengths[:,None],1e-20)
    # Weld only for lighting; never change the source vertices or triangles.
    _,weld=np.unique(np.round(vertices,6),axis=0,return_inverse=True)
    corners=weld[faces]
    parent=np.arange(faces.size)
    def root(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    edges={}
    for f,ids in enumerate(corners):
        for a,b in ((0,1),(1,2),(2,0)):
            u,v=int(ids[a]),int(ids[b])
            if u==v:continue
            edge=(min(u,v),max(u,v))
            ends=(3*f+a,3*f+b) if u<v else (3*f+b,3*f+a)
            edges.setdefault(edge,[]).append((f,ends))
    threshold=math.cos(math.radians(crease_degrees))
    for entries in edges.values():
        # Non-manifold edges remain sharp instead of blending unrelated shells.
        if len(entries)!=2:continue
        (a,ac),(b,bc)=entries
        if lengths[a]<1e-20 or lengths[b]<1e-20 or unit[a]@unit[b]<threshold:continue
        for x,y in zip(ac,bc):parent[root(y)]=root(x)
    roots=np.array([root(i) for i in range(faces.size)])
    _,indices=np.unique(roots,return_inverse=True)
    # Angle weights avoid large triangles dominating a curved CAD surface.
    weights=[]
    for c in range(3):
        a=triangles[:,(c+1)%3]-triangles[:,c];b=triangles[:,(c+2)%3]-triangles[:,c]
        weights.append(np.arctan2(np.linalg.norm(np.cross(a,b),axis=1),np.sum(a*b,axis=1)))
    weighted=unit[:,None,:]*np.stack(weights,axis=1)[:,:,None]
    normals=np.zeros((indices.max()+1,3));np.add.at(normals,indices,weighted.reshape(-1,3))
    magnitude=np.linalg.norm(normals,axis=1)
    normals[magnitude<1e-20]=[0,0,1]
    normals/=np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-20)
    mesh['normals']=np.round(normals,6).tolist()
    mesh['normal_indices']=indices.reshape(-1,3).tolist()


def prepare_normals(model):
    for mesh in model['meshes'].values():smooth_normals(mesh)
    model['shading']={'method':'angle_weighted_crease_normals','crease_degrees':40,
                      'note':'Display lighting only; vertices, holes and mechanical gaps are unchanged.'}
