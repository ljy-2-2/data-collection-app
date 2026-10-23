"""Opt-in numerical pacing. No clock reads, input leases or hardware authority."""
import math
from teleop.contracts.intent import EndEffectorPoseIntent
from teleop.retargeting.kuavo_target_progress import FixedAnchorTargetProgress


class TimedTargetProgress(FixedAnchorTargetProgress):
    policy = 'fixed_anchor_time_budget_latest_target'

    def __init__(self, *, linear_rate_m_s=1., angular_rate_rad_s=10./3.,
                 budget_cap_s=.03, **kwargs):
        for name,value,upper in (('linear_rate_m_s',linear_rate_m_s,1.),
                                 ('angular_rate_rad_s',angular_rate_rad_s,10./3.),
                                 ('budget_cap_s',budget_cap_s,.03)):
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<value<=upper:
                raise ValueError(name+' outside audited numerical pacing range')
        super().__init__(max_position_step_m=linear_rate_m_s*budget_cap_s,
                         max_orientation_step_rad=angular_rate_rad_s*budget_cap_s, **kwargs)
        self.linear_rate_m_s=float(linear_rate_m_s)
        self.angular_rate_rad_s=float(angular_rate_rad_s)
        self.budget_cap_s=float(budget_cap_s)
        self._times={};self._budgets={};self._trial_budgets={}

    def prepare(self, raw_intent):
        raise ValueError('timed progress requires an explicit worker start timestamp')

    def prepare_at(self, raw_intent, *, now_ns):
        if not isinstance(raw_intent,EndEffectorPoseIntent):
            raise ValueError('EndEffectorPoseIntent required')
        if type(now_ns) is not int or now_ns<=0:
            raise ValueError('positive integer monotonic timestamp required')
        budgets={}
        for side in raw_intent.targets:
            old=self._times.get(side)
            if old is not None and now_ns<=old:
                raise ValueError('numerical progress clock must increase')
            budgets[side]=min(self.budget_cap_s,.01 if old is None else (now_ns-old)/1e9)
        # Validate the whole multi-arm ticket before consuming timestamps.
        self._budgets=budgets
        try:
            ticket=super().prepare(raw_intent)
        finally:
            self._budgets={}
        for side in raw_intent.targets:
            self._times[side]=now_ns
            self._trial_budgets[side]=(ticket,budgets[side])
        return ticket

    def _step_limits(self, side):
        budget=self._budgets[side]
        return self.linear_rate_m_s*budget,self.angular_rate_rad_s*budget

    def reset(self, sides=('left','right')):
        selected=tuple(sides) if sides is not None and not isinstance(sides,str) else sides
        super().reset(selected)
        for side in selected:
            self._times.pop(side,None);self._trial_budgets.pop(side,None)

    def diagnostic_fields(self,ticket):
        result=super().diagnostic_fields(ticket)
        budgets={}
        for side in ticket.raw_intent.targets:
            trial,budget=self._trial_budgets.get(side,(None,None))
            if trial is not ticket:raise ValueError('current timed trial required')
            budgets[side]=budget
        result.update(target_budget_s=budgets, target_budget_cap_s=self.budget_cap_s,
                      target_linear_rate_m_s=self.linear_rate_m_s,
                      target_angular_rate_rad_s=self.angular_rate_rad_s)
        return result
