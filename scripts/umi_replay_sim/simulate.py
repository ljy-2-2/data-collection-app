#!/usr/bin/env python3
"""Offline full-pose Tianji replay. No robot connection or execution mode exists."""
import argparse
import contextlib
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import numpy as np

ROOT=Path(__file__).resolve().parents[2]
VENDOR=ROOT/'teleop_control/vendor/tianji'
sys.path.insert(0,str(VENDOR/'teleop'))
from umi_data import Recording,relative_target
from numerical_ik import solver_models
from replay_plan import make_plan,basis
from marvin_teleop.configuration import configuration
from marvin_teleop.model import Models


def offline_guard(event,args):
    if event in ('socket.connect','socket.bind','subprocess.Popen','os.system'):
        raise RuntimeError('Simulation forbids network connections and external commands')
    if event=='ctypes.dlopen' and args[0] and ('Rt' in str(args[0]) or 'robot' in Path(str(args[0])).name.lower()):
        raise RuntimeError('Simulation forbids robot control libraries')


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    temporary.replace(path)


def load_initial(path):
    record=json.loads(path.read_text())
    if record.get('units')!='deg':raise ValueError('Initial pose requires explicit units="deg"')
    if record.get('robot')!='tianji':raise ValueError('Initial pose must be for tianji')
    joints={s:np.asarray(record['joints'][s],float) for s in ('left','right')}
    if any(q.shape!=(7,) or not np.isfinite(q).all() for q in joints.values()):
        raise ValueError('Initial pose requires seven finite joint angles for each arm')
    return record,joints


def export_preview(path,data,initial,models,trace,plan):
    # Failed plans contain only a diagnostic IK prefix, never an executable path.
    if plan:
        times=np.linspace(0,data.duration,math.ceil(data.duration*100)+1)
        q=np.asarray([[plan['curves'][s].at(t) for s in data.sides] for t in times])
        playback=times*plan['stretch']
    else:
        times=np.array([row[0] for row in trace] or [0.])
        q=np.asarray([[row[1][s] for s in data.sides] for row in trace] or [[initial[s] for s in data.sides]])
        playback=times
    # The full desired path remains visible in the report even when IK fails early.
    target_times=np.linspace(0,data.duration,math.ceil(data.duration*50)+1)
    first={s:data.at(s,0)[0] for s in data.sides}
    anchor={s:models.arms[s].fk(initial[s]) for s in data.sides}
    rotations={s:basis(first[s],anchor[s],'initial-tool',None) for s in data.sides}
    targets=np.asarray([[relative_target(first[s],data.at(s,t)[0],anchor[s],rotations[s]) for s in data.sides] for t in target_times])
    actual=np.asarray([[models.arms[s].fk(values[i]) for i,s in enumerate(data.sides)] for values in q])
    encoder=np.asarray([[data.at(s,t)[1] for s in data.sides] for t in target_times])
    np.savez_compressed(path,source_time_s=times,playback_time_s=playback,joints_deg=q,
                        actual_tcp=actual,target_time_s=target_times,target_tcp=targets,
                        encoder_rad=encoder,complete=np.array(bool(plan)))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',required=True,type=Path)
    p.add_argument('--initial',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--speed',type=float,default=1.,help='Requested playback ratio; slowed further for joint limits')
    p.add_argument('--joint-speed',type=float,default=30.,help='deg/s, simulation time scaling only')
    p.add_argument('--joint-accel',type=float,default=60.,help='deg/s^2, simulation time scaling only')
    p.add_argument('--rows',type=int,nargs='+',help='Optional platform row subset for diagnostics')
    p.add_argument('--umi-point',choices=['controller','hand-root'],default='controller')
    args=p.parse_args()
    if not 0<args.speed<=1 or not 0<args.joint_speed<=180 or not 0<args.joint_accel<=450:
        p.error('Speed/acceleration outside supported positive limits')
    sys.addaudithook(offline_guard)
    source_manifest=json.loads(args.manifest.read_text())
    entries=source_manifest['episodes']
    if args.rows:
        entries=[r for r in entries if r['row'] in args.rows]
        if len(entries)!=len(set(args.rows)):p.error('Requested row missing from frozen manifest')
    if not entries:p.error('Empty manifest')
    initial_record,initial=load_initial(args.initial)
    args.output.mkdir(parents=True,exist_ok=True)
    config_path=VENDOR/'teleop/marvin_teleop/config.json'
    cfg=configuration(config_path)
    models=Models(cfg)
    models.validate()
    solved_models=solver_models(models,'hybrid',False)
    assets=[config_path,VENDOR/'SDK_PYTHON/libKine.so',VENDOR/'CommonConfig/ccs_m6_40.MvKDCfg']
    assets.extend(arm.path for arm in models.arms.values())
    # Register target axes in the same CAD stand frame as the App viewer.
    from marvin_teleop.geometry import transform
    display=json.loads((ROOT/'public/tianji-model.json').read_text())
    base=next(j for j in display['joints'] if j['child']=='Base_L')
    display_from_world=transform(base['xyz'],base['rpy'])@np.linalg.inv(models.arms['left'].base)
    provenance=dict(robot='天机 Marvin M6',simulation='offline kinematic model; no dynamics or hardware',
        initial_pose=initial_record,initial_file_sha256=sha(args.initial),manifest_sha256=sha(args.manifest),
        model_files={str(f.relative_to(ROOT)):sha(f) for f in assets},
        simulation_code_sha256={f.name:sha(f) for f in Path(__file__).parent.glob('*.py')},
        tcp={s:dict(name=a.control_point_name,flange_to_tcp=a.tool.tolist()) for s,a in models.arms.items()},
        mapping='T_target(t) = T_robot_TCP_start @ inverse(T_UMI_first) @ T_UMI(t), separately per arm',
        scale=1.,orientation_enforced=True,source_point=args.umi_point,
        display_from_world=display_from_world.tolist(),
        requested_speed=args.speed,joint_speed_deg_s=args.joint_speed,joint_accel_deg_s2=args.joint_accel,
        no_hardware_execution=True,hardware_ready=False,
        tolerances=dict(ik_position_mm=.5,ik_orientation_deg=.2,interpolation_position_mm=2.,interpolation_orientation_deg=.5),
        limitations=['Each episode starts from the same supplied pose; transitions between episodes are not planned.',
          'Sampled model self-collision only; no measured workspace, dynamics, payload or controller tracking validation.',
          'Recorded encoders exported for inspection; gripper actuation and synchronization are not validated.',
          'Per-arm first-frame alignment preserves each tool relative motion, not original absolute world positions or inter-arm separation.'])
    write_json(args.output/'run.json',provenance)
    options=SimpleNamespace(start=0.,end=None,alignment='initial-tool',rotation=None,scale=1.,
                            speed=args.speed,joint_speed=args.joint_speed,joint_accel=args.joint_accel,position_only=False)
    results=[]
    for index,row in enumerate(entries,1):
        before=time.monotonic();trace=[];plan=None;data=None
        output=args.output/f"row-{row['row']:03d}"
        output.mkdir(exist_ok=True)
        result=dict(row=row['row'],id=row['id'],folder=row['folder'],status='failed',hardware_ready=False)
        try:
            source=args.manifest.parent/row['id']/'signals.mcap'
            digest=sha(source)
            if digest!=row['files']['signals.mcap']:raise ValueError('Source MCAP hash mismatch')
            result['source_sha256']=digest
            data=Recording(source,point=args.umi_point)
            result['recording']=data.summary()
            with (output/'planning.log').open('w') as log,contextlib.redirect_stdout(log):
                plan=make_plan(data,solved_models,initial,cfg,options,{}, {},trace=trace)
            result.update(status='pass',plan=plan['report'])
        except Exception as exc:
            result.update(error=str(exc),diagnostic=getattr(exc,'diagnostic',{}))
        if data is not None:
            export_preview(output/'preview.npz',data,initial,models,trace,plan)
            result['preview']='preview.npz'
            result['preview_complete']=plan is not None
            result['last_solved_source_time_s']=float(trace[-1][0]) if trace else 0.
        result['elapsed_s']=time.monotonic()-before
        write_json(output/'report.json',result)
        results.append(result)
        summary=dict(**provenance,total=len(entries),processed=len(results),
                     passed=sum(r['status']=='pass' for r in results),failed=sum(r['status']=='failed' for r in results),episodes=results)
        write_json(args.output/'summary.json',summary)
        print(f"[{index}/{len(entries)}] row {row['row']}: {result['status']} ({result['elapsed_s']:.1f}s)",flush=True)
    with (args.output/'summary.csv').open('w',newline='') as f:
        fields=['row','id','folder','status','last_solved_source_time_s','elapsed_s','error']
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(results)
    print(f"Finished: {summary['passed']} pass / {summary['failed']} failed. Hardware execution is not included.")
    return 0 if summary['failed']==0 else 2


if __name__=='__main__':raise SystemExit(main())
