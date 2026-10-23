"""Offline model-copy constraints and descriptive enqueue-to-feedback fitting.

No robot binding. Apparent delay includes unknown vendor interpolation and clock
receipt effects; these estimates are not identified physical motor constants.
"""
import math
from xml.etree import ElementTree as ET
import numpy as np
from teleop.robots.kuavo5_native_ik import ARM_NAMES


from teleop.robots.kuavo5_model_limits import intersect_arm_limits


def split_control_traces(rows,max_gap_ns=50_000_000):
    segments=[];last=None
    for row in rows:
        if tuple(row['joint_order'])!=ARM_NAMES:raise ValueError('unexpected joint order')
        stamp=row['feedback_timestamp_ns'];wire=row.get('wire')
        if wire is None:raise ValueError('wire enqueue evidence required')
        sent=wire['enqueue_completed_ns']
        if type(stamp) is not int or type(sent) is not int or stamp>sent:raise ValueError('invalid receipt/enqueue clocks')
        if any(len(v)!=14 or not all(math.isfinite(x) for x in v)
               for v in (row['feedback_rad'],wire['positions_deg'])):raise ValueError('invalid joint vector')
        if last and (stamp<last['feedback_timestamp_ns'] or sent<=last['wire']['enqueue_completed_ns']):
            raise ValueError('nonmonotonic robot-side clocks')
        if last is None or row['epoch']!=last['epoch'] or sent-last['wire']['enqueue_completed_ns']>max_gap_ns:
            segments.append([])
        segments[-1].append(row);last=row
    return segments


def resample_segments(segments,step_s=.01):
    """Causal zero-order enqueue reference, interpolated feedback for fitting only."""
    if not math.isfinite(step_s) or step_s<=0 or round(step_s*1e9)<1:
        raise ValueError('positive sampling step required')
    step_ns=round(step_s*1e9)
    output=[]
    for rows in segments:
        if len(rows)<2:continue
        origin=rows[0]['feedback_timestamp_ns']
        # Select the causal command with integer nanoseconds. Float arange can
        # fall just before an exact enqueue boundary and select the prior command.
        sent=np.array([r['wire']['enqueue_completed_ns']-origin for r in rows],dtype=np.int64)
        received=np.array([r['feedback_timestamp_ns']-origin for r in rows],dtype=np.int64)
        # Repeated feedback is not independent measurement; retain one sample.
        unique=np.r_[True,np.diff(received)>0]
        received=received[unique]
        q=np.array([r['feedback_rad'] for r in rows])[unique]
        u=np.radians(np.array([r['wire']['positions_deg'] for r in rows]))
        grid=np.arange(max(sent[0],received[0]),min(sent[-1],received[-1]),step_ns,dtype=np.int64)
        if len(grid)<100:continue
        indices=np.searchsorted(sent,grid,side='right')-1
        output.append((u[indices],np.column_stack([np.interp(grid,received,q[:,i]) for i in range(14)])))
    return output


def fit_response(segments,*,step_s=.01,delay_steps=range(11),tau_ms=range(0,151,5)):
    """Grid fit on first 60% of EACH segment; validate remaining contiguous 40%.

    q[k+1]=a*q[k]+(1-a)*u[k-delay]. Both teacher-forced one-step and
    full validation rollout errors are reported. No refit on validation data.
    """
    segments=[(np.asarray(u,dtype=float),np.asarray(q,dtype=float)) for u,q in segments]
    if not segments or not math.isfinite(step_s) or step_s<=0:raise ValueError('segments and positive step required')
    delays=tuple(delay_steps);taus=tuple(tau_ms)
    if not delays or not taus or any(type(d) is not int or d<0 for d in delays) or any(not math.isfinite(t) or t<0 for t in taus):
        raise ValueError('invalid fit grid')
    warm=max(delays)+1
    for u,q in segments:
        if u.ndim!=1 or u.shape!=q.shape or len(u)<100 or not np.isfinite(u).all() or not np.isfinite(q).all():
            raise ValueError('finite equal vectors of at least 100 samples required')
        if int(len(u)*.6)<=warm:raise ValueError('delay grid exceeds training span')
    train_span=max(float(np.ptp(u[:int(len(u)*.6)])) for u,q in segments)
    if train_span<.03:return dict(status='insufficient_excitation',training_reference_span_rad=train_span)
    best=None
    for delay in delays:
        xs=[];ys=[];us=[]
        for u,q in segments:
            k=np.arange(warm,int(len(u)*.6)-1)
            xs.append(q[k]);ys.append(q[k+1]);us.append(u[k-delay])
        x,y,u=np.concatenate(xs),np.concatenate(ys),np.concatenate(us)
        for tau in taus:
            a=math.exp(-step_s/(tau/1000)) if tau else 0.
            error=a*x+(1-a)*u-y;loss=float(np.mean(error**2))
            if best is None or loss<best[0]:best=(loss,delay,tau,a)
    loss,delay,tau,a=best;one=[];roll=[];instant=[];bias=[]
    for u,q in segments:
        cut=int(len(u)*.6);pred=float(q[cut])
        for k in range(cut,len(u)-1):
            target=u[k-delay]
            one.append(a*q[k]+(1-a)*target-q[k+1])
            pred=a*pred+(1-a)*target
            roll.append(pred-q[k+1]);instant.append(u[k]-q[k+1]);bias.append(u[k]-q[k])
    rmse=lambda e:float(np.sqrt(np.mean(np.square(e))))
    return dict(status='descriptive_fit_only',delay_ms=delay*step_s*1000,tau_ms=tau,
        training_reference_span_rad=train_span,training_one_step_rmse_rad=math.sqrt(loss),
        validation_one_step_rmse_rad=rmse(one),validation_rollout_rmse_rad=rmse(roll),
        validation_instant_rmse_rad=rmse(instant),validation_mean_reference_minus_feedback_rad=float(np.mean(bias)),
        validation_samples=len(one),grid_boundary=delay in (min(delays),max(delays)) or tau in (min(taus),max(taus)))
