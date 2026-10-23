"""Conservative fixed-base arm reach bounds derived from a revolute URDF chain.

Outside the bound can disprove a model endpoint; inside cannot prove IK, joint
limits, collisions or physical safety. Projection is an offline candidate goal,
never a joint command. The original raw target must remain separately recorded.
"""
from dataclasses import dataclass
import math
from numbers import Real
import xml.etree.ElementTree as ET
from teleop.contracts.intent import CartesianPose
from teleop.retargeting.dual_arm import _quaternion_to_matrix,_matmul,_transpose


def _vector(values):
    v=tuple(values)
    if len(v)!=3 or any(isinstance(x,bool) or not isinstance(x,Real) or not math.isfinite(x) for x in v):
        raise ValueError('three finite coordinates required')
    return v


def _rotate(r,v):return tuple(sum(r[i][j]*v[j] for j in range(3)) for i in range(3))


@dataclass(frozen=True)
class ArmReachBound:
    frame_id:str
    shoulder_m:tuple
    wrist_radius_m:float
    tool_offset_in_end_frame_m:tuple

    def __post_init__(self):
        if not isinstance(self.frame_id,str) or not self.frame_id:
            raise ValueError('named base frame required')
        object.__setattr__(self,'shoulder_m',_vector(self.shoulder_m))
        object.__setattr__(self,'tool_offset_in_end_frame_m',_vector(self.tool_offset_in_end_frame_m))
        r=self.wrist_radius_m
        if isinstance(r,bool) or not isinstance(r,Real) or not math.isfinite(r) or r<=0:
            raise ValueError('positive finite reach radius required')

    @property
    def tool_length_m(self):return math.sqrt(sum(x*x for x in self.tool_offset_in_end_frame_m))

    def _wrist(self,pose):
        if not isinstance(pose,CartesianPose) or pose.frame_id!=self.frame_id:
            raise ValueError('pose must use the model base frame')
        offset=_rotate(_quaternion_to_matrix(pose.orientation_xyzw),self.tool_offset_in_end_frame_m)
        return tuple(p-d for p,d in zip(pose.position_m,offset)),offset

    def assess(self,pose,*,position_tolerance_m=math.sqrt(3)*.02,rotation_tolerance_rad=.1):
        for v in (position_tolerance_m,rotation_tolerance_rad):
            if isinstance(v,bool) or not isinstance(v,Real) or not math.isfinite(v) or v<0:
                raise ValueError('finite nonnegative tolerance required')
        if rotation_tolerance_rad>math.pi:raise ValueError('rotation tolerance exceeds pi')
        wrist,_=self._wrist(pose)
        d=math.dist(wrist,self.shoulder_m)
        # Rotation through theta changes the rigid tool offset by at most this
        # chord. Triangle inequality adds it to allowed position error.
        reserve=position_tolerance_m+2*self.tool_length_m*math.sin(rotation_tolerance_rad/2)
        position_distance=math.dist(pose.position_m,self.shoulder_m)
        return dict(wrist_distance_m=d,wrist_radius_m=self.wrist_radius_m,
            tolerance_reserve_m=reserve,pose_excess_after_tolerance_m=d-self.wrist_radius_m-reserve,
            outside_pose_bound=d>self.wrist_radius_m+reserve+1e-10,
            position_distance_m=position_distance,position_radius_m=self.wrist_radius_m+self.tool_length_m,
            outside_position_bound=position_distance>self.wrist_radius_m+self.tool_length_m+position_tolerance_m+1e-10,
            full_ik_feasibility_proven=False,physical_robot_command_sent=False)

    def project(self, pose, *, soft_margin_m=0.):
        """Map a goal inside the necessary outer bound, preserving orientation.

        Positive margin gives an identity inner region and a C1 radial
        compression outside it. It has no mapping history or accumulated
        offset. Far outside, sensitivity tends to zero: this is not 1:1
        following, full IK feasibility, or a physical command.
        """
        if (isinstance(soft_margin_m, bool) or not isinstance(soft_margin_m, Real)
                or not math.isfinite(soft_margin_m)
                or not 0. <= soft_margin_m < self.wrist_radius_m):
            raise ValueError('soft margin must be finite, nonnegative and below radius')
        wrist,offset=self._wrist(pose);distance=math.dist(wrist,self.shoulder_m)
        start = self.wrist_radius_m - soft_margin_m
        if distance <= start:
            return pose
        if not math.isfinite(distance):
            raise ValueError('wrist distance exceeds finite numerical range')
        radius = self.wrist_radius_m
        if soft_margin_m:
            radius -= soft_margin_m * (soft_margin_m / (soft_margin_m + distance - start))
        fraction=radius/distance
        position=tuple(c+fraction*(p-c)+d for c,p,d in zip(self.shoulder_m,wrist,offset))
        return CartesianPose(pose.frame_id,position,pose.orientation_xyzw)


def _origin(joint):
    origin=joint.find('origin')
    xyz=_vector(tuple(float(x) for x in (origin.get('xyz','0 0 0') if origin is not None else '0 0 0').split()))
    r,p,y=_vector(tuple(float(x) for x in (origin.get('rpy','0 0 0') if origin is not None else '0 0 0').split()))
    cr,sr,cp,sp,cy,sy=math.cos(r),math.sin(r),math.cos(p),math.sin(p),math.cos(y),math.sin(y)
    rotation=((cy*cp,cy*sp*sr-sy*cr,cy*sp*cr+sy*sr),
              (sy*cp,sy*sp*sr+cy*cr,sy*sp*cr-cy*sr),(-sp,cp*sr,cp*cr))
    return xyz,rotation


def read_arm_reach_bound(urdf_xml,joint_names,end_link,*,base_frame='base_link'):
    names=tuple(joint_names)
    if len(names)!=7 or len(set(names))!=7:raise ValueError('ordered seven-joint arm chain required')
    joints={};by_child={}
    for j in ET.fromstring(urdf_xml).findall('joint'):
        name=j.get('name');parent=j.find('parent');child=j.find('child')
        if (not name or parent is None or child is None or not parent.get('link')
                or not child.get('link') or name in joints or child.get('link') in by_child):
            raise ValueError('unambiguous URDF tree required')
        joints[name]=j;by_child[child.get('link')]=j
    previous=base_frame;radius=0.;shoulder=None
    chain_links={base_frame}
    for i,name in enumerate(names):
        j=joints.get(name)
        if j is None or j.get('type') not in ('revolute','continuous') or j.find('parent').get('link')!=previous:
            raise ValueError('fixed-base serial revolute chain required')
        xyz,_=_origin(j)
        if i==0:shoulder=xyz
        else:radius+=math.sqrt(sum(x*x for x in xyz))
        previous=j.find('child').get('link')
        if previous in chain_links:
            raise ValueError('cyclic arm chain')
        chain_links.add(previous)
    fixed=[];link=end_link;visited=set()
    while link!=previous:
        if link in visited:raise ValueError('cyclic end-frame chain')
        visited.add(link);j=by_child.get(link)
        if j is None or j.get('type')!='fixed':raise ValueError('rigid end frame required')
        fixed.append(j);link=j.find('parent').get('link')
    translation=(0.,0.,0.);rotation=((1.,0.,0.),(0.,1.,0.),(0.,0.,1.))
    for j in reversed(fixed):
        xyz,r=_origin(j);step=_rotate(rotation,xyz)
        translation=tuple(a+b for a,b in zip(translation,step));rotation=_matmul(rotation,r)
    offset_in_end=_rotate(_transpose(rotation),translation)
    return ArmReachBound(base_frame,shoulder,radius,offset_in_end)
