"""Pure postprocessing for offline replay. Inject FK; never discover hardware.

A/B/C belong to the newest *delivered* solve in the active clutch. D/E are
current simulated reference/feedback, not that solve's eventual execution.
CD is reported only when the reference explicitly identifies the same solve.
Distances describe simultaneous separation, not additive causal attribution.
"""
import json
import math
from collections import Counter
from pathlib import Path

SIDES=('left','right')
EDGES=('AB','BC','CD','DE','AE')


def _pose(value):
    if hasattr(value,'position_m'):
        value=dict(frame_id=value.frame_id,position_m=value.position_m,orientation_xyzw=value.orientation_xyzw)
    if not isinstance(value,dict) or not isinstance(value.get('frame_id'),str) or not value['frame_id']:
        raise ValueError('named pose frame required')
    for key,n in (('position_m',3),('orientation_xyzw',4)):
        data=value.get(key)
        if not isinstance(data,(list,tuple)) or len(data)!=n or any(
                isinstance(x,bool) or not isinstance(x,(float,int)) or not math.isfinite(x) for x in data):
            raise ValueError('finite pose vector required: '+key)
    norm=math.sqrt(sum(x*x for x in value['orientation_xyzw']))
    if abs(norm-1)>1e-5:raise ValueError('unit quaternion required')
    return dict(frame_id=value['frame_id'],position_m=list(value['position_m']),
                orientation_xyzw=[x/norm for x in value['orientation_xyzw']])


def pose_error(a,b):
    a,b=_pose(a),_pose(b)
    if a['frame_id']!=b['frame_id']:raise ValueError('cannot compare different pose frames')
    dot=abs(sum(x*y for x,y in zip(a['orientation_xyzw'],b['orientation_xyzw'])))
    return dict(position_m=math.dist(a['position_m'],b['position_m']),
                rotation_deg=math.degrees(2*math.acos(min(1.,dot))))


def stats(values):
    values=sorted(values)
    return dict(count=len(values),mean=sum(values)/len(values) if values else None,
                p95=values[math.ceil(.95*len(values))-1] if values else None,max=max(values,default=None))


def _unique(rows,key):
    result={}
    for row in rows:
        k=key(row)
        if k in result:raise ValueError('duplicate solve/diagnostic key')
        result[k]=row
    return result


def build_layers(ticks,solves,diagnostics,forward,*,sample_period_ns=100_000_000):
    if type(sample_period_ns) is not int or sample_period_ns<=0:raise ValueError('positive sample period required')
    if any(b['time_ns']<=a['time_ns'] for a,b in zip(ticks,ticks[1:])):
        raise ValueError('strictly increasing tick times required')
    solve_map=_unique(solves,lambda s:(s['epoch'],s['sequence'],s['started_ns']))
    diag_map=_unique(diagnostics,lambda d:(d['context']['epoch'],d['source_sequence_id'],d['started_monotonic_ns']))
    paired=[]
    for key,s in solve_map.items():
        d=diag_map.get(key)
        if d is None:raise ValueError('missing matching solve diagnostic')
        if s['ready_ns']<s['started_ns']:raise ValueError('result ready before start')
        if (s['error'] is None)!=(d['event']=='ik_trace'):raise ValueError('inconsistent solve outcome')
        paired.append((s,d))
    paired.sort(key=lambda p:p[0]['ready_ns'])
    samples=[];index=0;latest={};next_sample=None;origin=ticks[0]['time_ns'] if ticks else 0
    for tick in ticks:
        now=tick['time_ns']
        while index<len(paired) and paired[index][0]['ready_ns']<=now:
            s,d=paired[index];latest[s['epoch']]=(s,d);index+=1
        if next_sample is not None and now<next_sample:continue
        next_sample=now+sample_period_ns
        poses={}
        for k,field in (('D','reference_rad'),('E','feedback_rad')):
            values=forward(tick[field]) if tick.get(field) is not None else {}
            poses[k]={s:_pose(values[s]) if values.get(s) is not None else None for s in SIDES}
        sides={};pair=latest.get(tick['epoch'])
        for side_index,side in enumerate(SIDES):
            p=dict(A=None,B=None,C=None,D=poses['D'][side],E=poses['E'][side])
            row=dict(poses=p,solve_status='unavailable',solve_sequence=None,solve_age_ms=None,
                     candidate_reference_relation='unmatched_or_braking',errors={})
            eligible=pair is not None and side in tick['active_sides']
            if eligible:
                s,d=pair
                eligible=0<=now-s['started_ns']<=200_000_000
                if 'clutch_generations' in tick and 'clutch_generations' in s:
                    eligible=eligible and tick['clutch_generations'][side_index]==s['clutch_generations'][side_index]
            if eligible:
                row.update(solve_status='accepted' if s['error'] is None else 'rejected',
                           solve_sequence=s['sequence'],solve_age_ms=(now-s['started_ns'])/1e6,
                           reason=s.get('reason'))
                progress=d.get('target_progress') or {}
                raw=progress.get('raw_targets') or {};effective=progress.get('effective_targets') or {}
                if side in raw:p['A']=_pose(raw[side])
                if side in effective:p['B']=_pose(effective[side])
                actual=d.get('actual_poses') or {}
                if s['error'] is None and side in actual:p['C']=_pose(actual[side])
                source=tick.get('reference_source')
                if source and source['epoch']==s['epoch'] and source['sequence']==s['sequence'] \
                        and source['kind']=='bounded_ik_candidate' and s['ready_ns']<=source['time_ns']<=now \
                        and now-source['time_ns']<=200_000_000 and p['C'] is not None:
                    row['candidate_reference_relation']='same_solve'
            for edge in EDGES:
                a,b=(p[k] for k in edge)
                row['errors'][edge]=(pose_error(a,b) if a is not None and b is not None and
                    (edge!='CD' or row['candidate_reference_relation']=='same_solve') else None)
            sides[side]=row
        samples.append(dict(time_s=(now-origin)/1e9,time_ns=now,status=tick['status'],epoch=tick['epoch'],
                            active_sides=tick['active_sides'],grip_values=tick.get('grip_values'),sides=sides,
                            reference_source=tick.get('reference_source'),
                            reference_age_ms=((now-tick['reference_source']['time_ns'])/1e6
                                if tick.get('reference_source') else None)))
    metrics={side:{edge:{unit:stats([r['sides'][side]['errors'][edge][unit] for r in samples
                if r['sides'][side]['errors'][edge] is not None and side in r['active_sides']])
                for unit in ('position_m','rotation_deg')} for edge in EDGES} for side in SIDES}
    return dict(scope='offline_hypothetical_response_not_physical_acceptance',physical_robot_command_sent=False,
        sample_period_ns=sample_period_ns,samples=samples,metrics=metrics,
        solve_counts=dict(Counter('accepted' if s['error'] is None else 'rejected' for s in solves)),
        notes=['A/B/C are latest delivered solve, not the latest XR pose',
               'CD requires an exact published source match; gaps are not zero error',
               'distances are not additive and do not prove causal responsibility',
               'D/E are simulated; no WBC, collisions, dynamics, or motor faults modeled',
               'each route has its own clutch anchors after regrip; raw goals are not a shared world trajectory'])


def render_html(cases):
    data=json.dumps(cases,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return Path(__file__).with_name('kuavo_layers.html').read_text().replace('__REPORT_DATA__',data)
