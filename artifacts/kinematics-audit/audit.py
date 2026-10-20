from pathlib import Path
import sys,json,time,math
import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'teleop_control/vendor/tianji/teleop'))
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models
from marvin_teleop.geometry import angle,rotation
from marvin_teleop.differential import step,rotation_vector
cfg=configuration(ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json')
models=Models(cfg);report={}
report['kinematic_validation']=models.validate()
report['collision_tools']={s:a.tool_bound is not None for s,a in models.arms.items()}
report['mount_source']=models.mapping_report['active_mount_source']
options=dict(elbow_branch_margin_deg=cfg['elbow_branch_margin_deg'],rotation_weight_m=cfg['ik_rotation_weight_m'],
 singular_rotation_weight_m=cfg['ik_singular_rotation_weight_m'],singular_sigma_low=cfg['ik_singular_sigma_low'],singular_sigma_high=cfg['ik_singular_sigma_high'],
 elbow_posture_offset_deg=cfg['elbow_posture_offset_deg'],elbow_posture_gain_s=cfg['elbow_posture_gain_s'],
 joint_limit_soft_margin_deg=cfg['joint_limit_soft_margin_deg'],joint_limit_reserve_deg=cfg['joint_limit_reserve_deg'],joint_limit_escape_speed_deg_s=cfg['joint_limit_escape_speed_deg_s'])
rng=np.random.default_rng(74)
report['static']=[];report['trajectory']=[]
for side,arm in models.arms.items():
 poses={'zero':np.zeros(7),'bent':np.array([0.,-35,0,-65,0,0,0])}
 for name,q0 in poses.items():
  if not arm.valid_joints(q0):continue
  tcp=arm.fk(q0);j=arm.world_jacobian(q0,tcp)
  step(arm,tcp,q0,.01,180,velocity=np.zeros(7),max_accel=450,elbow_reference=q0,**options)
  report['static'].append(dict(side=side,pose=name,sigma=float(np.linalg.svd(j,compute_uv=False)[-1]),
      rotation_weight=arm.last_rotation_weight_m,min_margin=arm.last_joint_limit_margin_deg))
  for kind in ('translate','rotate'):
   q=q0.copy();vel=np.zeros(7);maxp=maxr=0.;minweight=1.;times=[];error_up=0;prev=None
   for i in range(300):
    target=tcp.copy();u=min(1.,i/100)
    if kind=='translate':target[1,3]+=.05*u
    else:target[:3,:3]=rotation([0,0,1],math.radians(20)*u)@tcp[:3,:3]
    before=arm.fk(q)
    t=time.perf_counter()
    qnext=step(arm,target,q,.01,180,velocity=vel,max_accel=450,elbow_reference=q0,**options)
    times.append((time.perf_counter()-t)*1000)
    vel=(qnext-q)/.01;q=qnext
    actual=arm.fk(q)
    p=float(np.linalg.norm(actual[:3,3]-target[:3,3])*1000);r=math.degrees(angle(actual[:3,:3]@target[:3,:3].T))
    maxp=max(p,maxp);maxr=max(r,maxr);minweight=min(minweight,arm.last_rotation_weight_m)
    if i>100 and prev is not None and p>prev+.01:error_up+=1
    prev=p
   report['trajectory'].append(dict(side=side,pose=name,kind=kind,final_position_error_mm=p,final_rotation_error_deg=r,
     max_position_error_mm=maxp,max_rotation_error_deg=maxr,min_rotation_weight=minweight,
     solver_ms_p50=float(np.percentile(times,50)),solver_ms_p95=float(np.percentile(times,95)),post_hold_position_error_increases=error_up))
# Full collision validation is called three times on every live control tick.
joints={s:np.zeros(7) for s in models.arms}
times=[]
for _ in range(35):
 t=time.perf_counter();reason=models.collision_reason(joints,cfg['collision_margin_m']);times.append((time.perf_counter()-t)*1000)
report['collision_timing']=dict(reason_at_zero=reason,ms_p50=float(np.percentile(times,50)),ms_p95=float(np.percentile(times,95)),three_checks_ms_p50=float(np.percentile(times,50)*3))
report['installed_defaults']=dict(speed=cfg['max_joint_speed_deg_s'],accel=cfg['max_joint_accel_deg_s2'],rate=cfg['rate_hz'])
(ROOT/'artifacts/kinematics-audit/results.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
print(json.dumps(report,indent=2,ensure_ascii=False),flush=True)
