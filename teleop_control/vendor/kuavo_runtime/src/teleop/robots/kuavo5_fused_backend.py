"""Explicit candidate factory. No device discovery or execution on import.

The existing public IKSolver boundary is retained. This module does not choose,
launch or silently replace a solver, controller, or transport.
"""
import threading
from teleop.control.kuavo5_live_controller import NativeArmWorkSolver
from teleop.retargeting.kuavo_vendor_incremental import VendorIncrementalRetargeter, VendorIncrementalConfig
from teleop.retargeting.kuavo_target_progress import KuavoTargetProgress
from teleop.robots.kuavo5_native_ik import ARM_NAMES
from teleop.control.kuavo5_live_controller import KuavoLiveController
from teleop.control.kuavo5_tracking_governor import KuavoTrackingGovernor

_CLAIM_LOCK=threading.Lock()


def make_fused_work_solver(schema,*,solver,frame_id,config=None,recovery='fixed_anchor'):
    if tuple(j.name for j in schema.joints)!=ARM_NAMES:
        raise ValueError('candidate requires exactly the v63 ordered 14 arm joints')
    # Keep the actual solver injected, including its model, FK/limit checks,
    # and caller-owned seed. Never pass 14 values into vendor's 18-DoF launcher.
    for name in ('solve','reset','forward_kinematics'):
        if not callable(getattr(solver,name,None)):
            raise ValueError('solver does not implement the shared IK contract')
    if recovery not in ('fixed_anchor','baseline_progress'):
        raise ValueError('explicit fixed_anchor or baseline_progress recovery required')
    if config is None:config=VendorIncrementalConfig(position_smoothing='shared_joint')
    return NativeArmWorkSolver(solver,VendorIncrementalRetargeter(frame_id=frame_id,config=config,
        input_owner_validates_freshness=True),
        target_progress=KuavoTargetProgress() if recovery=='baseline_progress' else None)


def make_fused_controller(schema,transport,*,solver,clock,diagnostic=None,recovery='fixed_anchor',config=None):
    """Reuse the existing single control owner, gates and transport.

    Caller supplies every dependency. The shipped candidate CLI uses only fake
    callbacks. This is not an automatic robot binding or runtime hot switch.
    """
    worker=make_fused_work_solver(schema,solver=solver,frame_id='base_link',recovery=recovery,config=config)
    with _CLAIM_LOCK:
        if getattr(transport,'_fused_owner_claimed',False) or getattr(transport,'_closed',False):
            raise ValueError('transport already owned or closed; no runtime backend switch')
        transport._fused_owner_claimed=True
        try:
            return KuavoLiveController(schema,transport,solve=worker,clock=clock,
                control_period_s=.01,diagnostic=diagnostic,max_velocity_rad_s=.5,
                max_acceleration_rad_s2=5.,reuse_valid_solution_seed=True,
                tracking_governor=KuavoTrackingGovernor(lead_rad=.15))
        except Exception:
            transport._fused_owner_claimed=False
            raise
