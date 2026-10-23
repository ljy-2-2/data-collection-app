"""Fixed-waist port of Leju IncrementalControlModule's mathematical core.

Pinned upstream: de6a28eacbde15a9680af72d169ee106b06c5273.
See docs/kuavo5-fused-candidate.md for provenance and deliberate adaptations.
No ROS, device access, launch, mode service, publisher, or IK implementation.
"""
from dataclasses import dataclass
import math

from teleop.contracts.intent import CartesianPose, EndEffectorPoseIntent
from teleop.contracts.xr import XRFrame
from teleop.retargeting.dual_arm import (
    DualArmRetargetingResult, _ROBOT_FROM_XR, _matmul, _matvec,
    _transpose, _yaw_frame, _quaternion_to_matrix, _matrix_to_quaternion)

SIDES = ('left', 'right')


def _unit(q):
    norm=math.sqrt(sum(v*v for v in q))
    if not math.isfinite(norm) or norm < 1e-12:
        raise ValueError('nonzero finite quaternion required')
    return tuple(v/norm for v in q)


def _mul(a,b):
    x,y,z,w=a; X,Y,Z,W=b
    return _unit((w*X+x*W+y*Z-z*Y,w*Y-x*Z+y*W+z*X,
                  w*Z+x*Y-y*X+z*W,w*W-x*X-y*Y-z*Z))


def _inv(q): return (-q[0],-q[1],-q[2],q[3])


def incremental_orientation(current, anchor, target, threshold=.01):
    """Upstream q_delta=q_current*q_anchor^-1; accumulate sub-threshold input."""
    current,anchor,target=map(_unit,(current,anchor,target))
    delta=_mul(current,_inv(anchor))
    angle=2*math.acos(min(1.,abs(delta[3])))
    if angle < threshold: return anchor,target
    return current,_mul(delta,target)


def limit_orientation(target, measured):
    """Upstream getLatestRobot*HandQuatInc / limitQuaternionAngleEulerZYX."""
    x,y,z,w=_mul(_inv(measured),target)
    roll=math.atan2(2*(w*x+y*z),1-2*(x*x+y*y))
    pitch=math.asin(max(-1.,min(1.,2*(w*y-z*x))))
    yaw=math.atan2(2*(w*z+x*y),1-2*(y*y+z*z))
    yaw=max(-.95*math.pi/2,min(.95*math.pi/2,yaw))
    pitch=max(-math.pi/2,min(math.pi/2,pitch))
    roll=max(-math.pi/2,min(math.pi/2,roll))
    qx=(math.sin(roll/2),0,0,math.cos(roll/2))
    qy=(0,math.sin(pitch/2),0,math.cos(pitch/2))
    qz=(0,0,math.sin(yaw/2),math.cos(yaw/2))
    return _mul(measured,_mul(qz,_mul(qy,qx)))


def fhan_step(x,v,target,*,h,r=900.,h0=.06):
    """Exact upstream leju_utils::fhanStepForward arithmetic and sign epsilon."""
    if not all(math.isfinite(t) for t in (x,v,target,h,r,h0)) or min(h,r,h0)<=0:
        raise ValueError('finite state and positive FHAN parameters required')
    def sign(t): return 0. if -1e-6<t<1e-6 else (1. if t>=1e-6 else -1.)
    def fsg(t,d): return (sign(t+d)-sign(t-d))/2
    d=r*h0*h0; a0=h0*v; y=x-target+a0
    a1=math.sqrt(d*(d+8*abs(y))); a2=a0+sign(y)*(a1-d)/2
    a=(a0+y)*fsg(y,d)+a2*(1-fsg(y,d))
    acceleration=-r*(a/d)*fsg(a,d)-r*sign(a)*(1-fsg(a,d))
    return x+h*v,v+h*acceleration


@dataclass(frozen=True)
class VendorIncrementalConfig:
    position_smoothing: str='vendor_task'
    scale: float=1.
    fhan_r: float=900.
    smoothing_horizon_s: float=.06
    max_integration_step_s: float=.01
    orientation_threshold_rad: float=.01
    input_timeout_ns: int=100_000_000

    def __post_init__(self):
        if self.position_smoothing not in ('vendor_task','shared_joint'):
            raise ValueError('explicit vendor_task or shared_joint smoothing required')
        for value in (self.scale,self.fhan_r,self.smoothing_horizon_s,self.max_integration_step_s):
            if isinstance(value,bool) or not math.isfinite(value) or value<=0:
                raise ValueError('finite positive incremental configuration required')
        if not 0<=self.orientation_threshold_rad<=math.pi:
            raise ValueError('orientation threshold out of range')
        if self.max_integration_step_s>self.smoothing_horizon_s/2:
            raise ValueError('integration step too large for smoothing horizon')
        if type(self.input_timeout_ns) is not int or not 0<self.input_timeout_ns<=100_000_000:
            raise ValueError('input timeout must be at most 100 ms')


class VendorIncrementalRetargeter:
    """One coordinate transform, vendor math, independent clutches; no execution.

    Public start requires release. declutch() is the serial worker's explicit
    authorization to anchor a held side after the input owner observed its edge.
    Source loss resets that authorization; normal IK failures do not rebase.
    """
    def __init__(self,*,frame_id='base_link',config=None,input_owner_validates_freshness=False):
        if frame_id!='base_link':
            raise ValueError('fixed-waist candidate requires verified base_link frame')
        self.frame_id=frame_id; self.config=config or VendorIncrementalConfig()
        if type(input_owner_validates_freshness) is not bool:
            raise ValueError('explicit input owner validation flag required')
        self._owner_validates_freshness=input_owner_validates_freshness
        self._states={}; self._allowed=dict.fromkeys(SIDES,False)
        self._last=None; self.diagnostics={}

    def declutch(self,sides=SIDES):
        if not sides or len(set(sides))!=len(sides) or set(sides)-set(SIDES):
            raise ValueError('unique left/right sides required')
        for side in sides:
            self._states.pop(side,None); self._allowed[side]=True
        if set(sides)==set(SIDES):
            # A new owner-authorized epoch must not inherit the old epoch's
            # timeout. Owner has already validated the fresh release/press.
            self._last=None

    def _invalidate(self,reason):
        self._states.clear(); self._allowed=dict.fromkeys(SIDES,False)
        self.diagnostics={'status':reason,'requires_release':True}
        return DualArmRetargetingResult(None,{})

    def update(self,frame,anchor_poses):
        try:
            if not isinstance(frame,XRFrame):raise ValueError('XRFrame required')
            return self._update(frame,anchor_poses)
        except (ValueError,TypeError,KeyError):
            self._invalidate('invalid_input_or_feedback')
            raise

    def _update(self,frame,anchor_poses):
        if frame.coordinate_frame!='openxr_local':
            self._invalidate('invalid_coordinate_frame')
            raise ValueError('OpenXR local coordinate frame required')
        for pose in (frame.head_pose,frame.left_controller.pose,frame.right_controller.pose):
            _unit(pose.orientation_xyzw)
        stamp=(frame.sequence_id,frame.device_timestamp_ns,frame.received_monotonic_ns)
        if frame.stale:
            self._last=stamp
            return self._invalidate('stale_input')
        if self._last is not None:
            if any(now<=old for now,old in zip(stamp,self._last)):
                # Repeated source packets neither advance the filter nor renew its clock.
                self.diagnostics={'status':'repeated_or_reordered_input'}
                return DualArmRetargetingResult(None,{})
            # The serial IK worker intentionally samples fewer frames than the
            # input owner. Its solve cadence is not the PICO connection cadence.
            gap_limit=200_000_000 if self._owner_validates_freshness else self.config.input_timeout_ns
            if stamp[2]-self._last[2]>gap_limit:
                self._last=stamp
                return self._invalidate('input_gap')
        self._last=stamp
        get_anchor=anchor_poses if callable(anchor_poses) else anchor_poses.__getitem__
        targets={}; raw_targets={}
        for side,hand in (('left',frame.left_controller),('right',frame.right_controller)):
            state=self._states.get(side)
            if hand.grip<.90:
                self._states.pop(side,None); self._allowed[side]=True
                continue
            if state is None:
                if not self._allowed[side] or hand.grip<=.95: continue
                anchor=get_anchor(side)
                if not isinstance(anchor,CartesianPose) or anchor.frame_id!=self.frame_id:
                    raise ValueError('verified base_link FK anchor required')
                basis=_matmul(_ROBOT_FROM_XR,_transpose(_yaw_frame(frame.head_pose)))
                q=_matrix_to_quaternion(_matmul(_matmul(basis,
                    _quaternion_to_matrix(hand.pose.orientation_xyzw)),_transpose(basis)))
                state=dict(anchor=anchor,basis=basis,human=hand.pose.position_m,
                    q_anchor=q,q_target=anchor.orientation_xyzw,delta=(0.,0.,0.),
                    velocity=(0.,0.,0.),raw=(0.,0.,0.),time_ns=stamp[2])
                self._states[side]=state
                # Anchor is actual FK, including valid (0,0,0); no default pose fallback.
                targets[side]=anchor; raw_targets[side]=anchor
                continue
            anchor=state['anchor']; basis=state['basis']
            raw=tuple(self.config.scale*v for v in _matvec(basis,
                tuple(a-b for a,b in zip(hand.pose.position_m,state['human']))))
            elapsed=(stamp[2]-state['time_ns'])/1e9
            steps=max(1,math.ceil(elapsed/self.config.max_integration_step_s-1e-9))
            h=elapsed/steps
            # Interpolate the sampled hand path; bounded substeps cover REAL elapsed time.
            for k in (range(1,steps+1) if self.config.position_smoothing=='vendor_task' else ()):
                goal=tuple(a+(b-a)*k/steps for a,b in zip(state['raw'],raw))
                xv=[fhan_step(x,v,g,h=h,r=self.config.fhan_r,
                    h0=self.config.smoothing_horizon_s)
                    for x,v,g in zip(state['delta'],state['velocity'],goal)]
                state['delta']=tuple(p[0] for p in xv); state['velocity']=tuple(p[1] for p in xv)
            if self.config.position_smoothing=='shared_joint':
                # The fused profile already has the shared joint planner and
                # vendor execution interpolation. Do not add Cartesian lag.
                state['delta']=raw;state['velocity']=(0.,0.,0.)
            q=_matrix_to_quaternion(_matmul(_matmul(basis,
                _quaternion_to_matrix(hand.pose.orientation_xyzw)),_transpose(basis)))
            state['q_anchor'],state['q_target']=incremental_orientation(q,state['q_anchor'],
                state['q_target'],self.config.orientation_threshold_rad)
            measured=get_anchor(side)
            if not isinstance(measured,CartesianPose) or measured.frame_id!=self.frame_id:
                raise ValueError('verified base_link FK required for orientation limit')
            target_q=limit_orientation(state['q_target'],measured.orientation_xyzw)
            targets[side]=CartesianPose(self.frame_id,tuple(a+b for a,b in zip(anchor.position_m,state['delta'])),target_q)
            raw_targets[side]=CartesianPose(self.frame_id,tuple(a+b for a,b in zip(anchor.position_m,raw)),state['q_target'])
            state.update(raw=raw,time_ns=stamp[2])
        self.diagnostics=dict(status='target' if targets else 'idle',
            policy='fixed_anchor_vendor_incremental',raw_targets=raw_targets,filtered_targets=targets)
        return DualArmRetargetingResult(EndEffectorPoseIntent(targets,source='kuavo_vendor_incremental') if targets else None,{})
