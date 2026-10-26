"""Bounded numerical IK using FK/Jacobian, independent of vendor analytic IK branches."""
import math
import numpy as np


class ElbowBranchDomain:
    """Offline replay retains the initial elbow branch throughout the curve."""
    def __init__(self,arm,reference,margin_deg):
        self.arm=arm
        distance=float(reference[3]-arm.elbow_singularity_deg)
        self.sign=1. if distance>=0 else -1.
        self.clearance=min(float(margin_deg),abs(distance))
    def __getattr__(self,name):return getattr(self.arm,name)
    def valid_joints(self,q,margin=.2):
        return self.arm.valid_joints(q,margin) and self.sign*(q[3]-self.arm.elbow_singularity_deg)>=self.clearance-1e-10
    def joint_halfspaces(self,margin=.2):
        faces,bounds=self.arm.joint_halfspaces(margin)
        face=np.zeros(7);face[3]=-self.sign
        return np.vstack((faces,face)),np.r_[bounds,-self.sign*self.arm.elbow_singularity_deg-self.clearance]


def residual(arm,target,q):
    from marvin_teleop.differential import rotation_vector
    actual=arm.fk(q)
    error=np.r_[target[:3,3]-actual[:3,3],rotation_vector(target[:3,:3]@actual[:3,:3].T)]
    return error,actual


def project(domain,q):
    A,b=domain.joint_halfspaces();q=np.array(q,float).copy()
    for _ in range(100):
        changed=False
        for a,limit in zip(A,b):
            extra=float(a@q-limit+1e-5)
            if extra>0:q-=extra*a/(a@a);changed=True
        if not changed:break
    return q


def solve_numeric(arm,target,reference,*,preserve_branch=True,starts=1,iterations=280,position_only=False):
    from marvin_teleop.constrained_ik import bounded_correction,DAMPING_SQ
    domain=ElbowBranchDomain(arm,reference,.1) if preserve_branch else arm
    reference=np.asarray(reference,float)
    seeds=[project(domain,reference)]
    # Deterministic multi-start diagnostics; no claim of global infeasibility if all fail.
    for elbow in [30.,-50.,-90.,55.]:
        q=np.zeros(7);q[3]=elbow;seeds.append(project(domain,q))
    rng=np.random.default_rng(696)
    while len(seeds)<starts:
        q=reference+rng.normal(0,45,7) if len(seeds)%2 else rng.uniform(arm.lower+.3,arm.upper-.3)
        seeds.append(project(domain,q))
    best=None;total=0
    weight=np.array([1.,1.,1.,0.,0.,0.]) if position_only else np.array([1.,1.,1.,.25,.25,.25])
    for seed in seeds[:starts]:
        q=seed.copy();stale=0;last=math.inf
        for _ in range(iterations):
            total+=1;error,T=residual(arm,target,q)
            ep=float(np.linalg.norm(error[:3]));er=math.degrees(float(np.linalg.norm(error[3:])))
            cost=float(np.linalg.norm(weight*error))
            if best is None or cost<best['cost']:
                best=dict(cost=cost,position_error_mm=ep*1000,rotation_error_deg=er,joints_deg=q.tolist())
            if ep<=.0005 and (position_only or er<=.2) and domain.valid_joints(q):
                # An independent URDF forward chain also checks the candidate.
                urdf=arm.chain(q)[-1]@arm.flange@arm.tool
                from replay_plan import rotation_angle
                ue=float(np.linalg.norm(urdf[:3,3]-target[:3,3]))
                ur=math.degrees(rotation_angle(urdf[:3,:3]@target[:3,:3].T))
                if ue>.0008 or (not position_only and ur>.25):break
                return q,dict(success=True,position_only=position_only,min_jacobian_sigma=float(np.linalg.svd(arm.world_jacobian(q,T)[:3 if position_only else 6],compute_uv=False)[-1]),iterations=total,position_error_mm=ep*1000,rotation_error_deg=er,
                              urdf_position_error_mm=ue*1000,urdf_rotation_error_deg=ur,
                              preserves_elbow_branch=bool((q[3]-arm.elbow_singularity_deg)*(reference[3]-arm.elbow_singularity_deg)>0))
            if last-cost<1e-9:stale+=1
            else:stale=0
            if stale>28:break
            last=cost
            J=weight[:,None]*arm.world_jacobian(q,T);e=weight*error
            delta=np.linalg.solve(J.T@J+DAMPING_SQ*np.eye(7),J.T@e)
            delta,_=bounded_correction(J,delta,domain,q)
            delta*=min(1.,math.radians(8)/max(float(np.max(abs(delta))),1e-12))
            if np.max(abs(delta))<1e-10:break
            moved=False
            for step in [1.,.5,.25,.1,.03]:
                candidate=q+np.degrees(delta)*step
                if not domain.valid_joints(candidate):continue
                next_error,_=residual(arm,target,candidate)
                if np.linalg.norm(weight*next_error)<cost-1e-12:
                    q=candidate;moved=True;break
            if not moved:break
    return None,dict(success=False,iterations=total,best=best,
                     note='有限次多起点优化未达容差，不构成全局不可达证明')


class SolverArm:
    def __init__(self,arm,mode='hybrid',position_only=False):
        self.arm=arm;self.mode=mode;self.position_only=position_only;self.last_method='';self.fallbacks=0;self.last_numerical=None
    def __getattr__(self,name):return getattr(self.arm,name)
    def ik(self,target,reference,**kwargs):
        if not self.position_only and self.mode!='numerical':
            try:
                q=self.arm.ik(target,reference,**kwargs)
                # An analytic solution on the opposite elbow branch is not a
                # reason to skip the bounded numerical solve on this branch.
                if not ElbowBranchDomain(self.arm,reference,.1).valid_joints(q):
                    raise ValueError('Analytic candidate changes elbow branch')
                error,T=residual(self.arm,target,q)
                if np.linalg.norm(error[:3])>.0005 or np.linalg.norm(error[3:])>math.radians(.2):
                    raise ValueError('Analytic candidate exceeds full-pose tolerance')
                self.last_method='vendor';return q
            except ValueError:
                if self.mode=='vendor':raise
        q,report=solve_numeric(self.arm,target,reference,preserve_branch=True,starts=1,position_only=self.position_only)
        self.last_numerical=report
        if q is None:raise ValueError(f'{self.side}: 数值 IK 未收敛到 '+('0.5mm 位置容差；' if self.position_only else '0.5mm/0.2° 容差；')+str(report.get('best')))
        if report['min_jacobian_sigma']<1e-5:raise ValueError(f'{self.side}: 数值候选接近雅可比奇异姿态，不用于连续回放')
        self.last_method='numerical-position' if self.position_only else 'numerical';self.fallbacks+=1
        return q


def solver_models(models,mode,position_only=False):
    from types import SimpleNamespace
    def wrap(a):
        if isinstance(a,SolverArm):
            if a.mode==mode and a.position_only==position_only:return a
            a=a.arm
        return SolverArm(a,mode,position_only)
    return SimpleNamespace(arms={s:wrap(a) for s,a in models.arms.items()},collision_reason=models.collision_reason)
