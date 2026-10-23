"""Kuavo-only reference-goal governor; no transport or hardware authority.

Shorten the requested path before the ordinary acceleration planner. Never
replace the last sent reference with feedback or clip a planned position step.
"""
from dataclasses import dataclass
import math
from numbers import Real
from types import MappingProxyType

from teleop.robots.kuavo5_native_ik import ARM_NAMES

SIDES = {'left': ARM_NAMES[:7], 'right': ARM_NAMES[7:]}


@dataclass(frozen=True)
class TrackingGoal:
    targets: object
    arm_scales: object
    limited_joints: tuple
    max_lead_rad: float
    tracking_limited: bool
    stalled_joint: str | None
    stall_age_s: float


class KuavoTrackingGovernor:
    def __init__(self, *, lead_rad=.06, stalled_after_s=.5, min_progress_rad=.002):
        for name, value in (('lead_rad', lead_rad), ('stalled_after_s', stalled_after_s),
                            ('min_progress_rad', min_progress_rad)):
            if isinstance(value, bool) or not isinstance(value, Real) \
                    or not math.isfinite(value) or value <= 0:
                raise ValueError(name+' must be finite and positive')
        if min_progress_rad >= lead_rad:
            raise ValueError('progress threshold must be smaller than soft lead')
        self.lead_rad = float(lead_rad)
        self.stalled_after_s = float(stalled_after_s)
        self.min_progress_rad = float(min_progress_rad)
        self._watches = {}
        self._last_now = None

    def validate_profile(self, *, velocity, acceleration, period, hard_tracking):
        for name, value in (('velocity', velocity), ('acceleration', acceleration),
                            ('period', period), ('hard_tracking', hard_tracking)):
            if isinstance(value, bool) or not isinstance(value, Real) \
                    or not math.isfinite(value) or value <= 0:
                raise ValueError('tracking governor requires finite positive '+name)
        # This is a reference-planning reserve for unchanged feedback, never a
        # statement about physical stopping distance or motor acceleration.
        reserve = velocity*velocity/(2*acceleration)+velocity*period
        if self.lead_rad+reserve >= hard_tracking:
            raise ValueError('tracking governor lacks hard-bound braking reserve')

    def reset(self, sides=('left', 'right')):
        selected = tuple(sides)
        if not selected or len(set(selected)) != len(selected) or set(selected)-set(SIDES):
            raise ValueError('distinct left/right reset sides required')
        for side in selected:
            for name in SIDES[side]:
                self._watches.pop(name, None)

    @staticmethod
    def _values(values, label):
        if not hasattr(values, 'keys') or set(values) != set(ARM_NAMES):
            raise ValueError(label+' requires exactly 14 arm joints')
        copied = {}
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
                raise ValueError('invalid '+label+': '+name)
            copied[name] = float(value)
        return copied

    def _clock(self, now_ns):
        if type(now_ns) is not int or now_ns < 0 \
                or (self._last_now is not None and now_ns < self._last_now):
            raise ValueError('invalid or backwards governor clock')
        self._last_now = now_ns

    def _sample_watch(self, name, measured, direction, now_ns):
        watch = self._watches.get(name)
        if watch is None or watch[3] != direction:
            watch = (now_ns, measured, measured, direction)
        start, low, high, direction = watch
        low = min(low, measured)
        high = max(high, measured)
        if high-low >= self.min_progress_rad:
            start = now_ns
            low = high = measured
        self._watches[name] = (start, low, high, direction)
        return (now_ns-start)/1e9

    def observe_recovery(self, feedback, previous, *, active_sides, now_ns,
                         previous_velocities=None):
        """Continue existing physical-progress watches, never create a goal.

        A failed IK candidate cannot grant a new outward demand. It also cannot
        erase evidence that an already slowed reference remains ahead of a
        stationary measured joint. Only existing watches are sampled here.
        """
        feedback = self._values(feedback, 'feedback')
        previous = self._values(previous, 'previous reference')
        velocities = (dict.fromkeys(ARM_NAMES, 0.) if previous_velocities is None else
                      self._values(previous_velocities, 'previous velocities'))
        sides = tuple(active_sides)
        if len(set(sides)) != len(sides) or set(sides)-set(SIDES):
            raise ValueError('distinct active arm sides required')
        self._clock(now_ns)
        active = {name for side in sides for name in SIDES[side]}
        max_lead = max((abs(previous[n]-feedback[n]) for n in active), default=0.)
        stall_age, stalled = 0., None
        for name, watch in tuple(self._watches.items()):
            error = previous[name]-feedback[name]
            if name not in active or abs(error) < self.lead_rad*2/3 \
                    or error*watch[3] <= 0 or abs(velocities[name]) > .03:
                self._watches.pop(name, None)
                continue
            age = self._sample_watch(name, feedback[name], watch[3], now_ns)
            stall_age = max(stall_age, age)
            if age >= self.stalled_after_s and stalled is None:
                stalled = name
        return TrackingGoal(MappingProxyType(previous), MappingProxyType({}),
            tuple(self._watches), max_lead, bool(self._watches), stalled, stall_age)

    def apply(self, targets, feedback, previous, *, active_sides, now_ns,
              previous_velocities=None):
        targets = self._values(targets, 'targets')
        feedback = self._values(feedback, 'feedback')
        previous = self._values(previous, 'previous reference')
        velocities = (dict.fromkeys(ARM_NAMES, 0.) if previous_velocities is None else
                      self._values(previous_velocities, 'previous velocities'))
        sides = tuple(active_sides)
        if len(set(sides)) != len(sides) or set(sides)-set(SIDES):
            raise ValueError('distinct active arm sides required')
        self._clock(now_ns)
        governed = dict(targets)
        scales, limited = {}, []
        max_lead, stall_age = 0., 0.
        active = {name for side in sides for name in SIDES[side]}
        tracking_limited, stalled = False, None
        for name in tuple(self._watches):
            if name not in active:
                self._watches.pop(name)
        for side in sides:
            factor = 1.
            for name in SIDES[side]:
                error = previous[name]-feedback[name]
                max_lead = max(max_lead, abs(error))
                delta = targets[name]-previous[name]
                allowed = 1.
                if delta:
                    edge = feedback[name]+math.copysign(self.lead_rad, delta)
                    allowed = max(0., min(1., (edge-previous[name])/delta))
                    factor = min(factor, allowed)
                    if allowed < 1.-1e-12:
                        limited.append(name)
                # A distant goal may be shortened even with negligible lag;
                # that still permits full reference speed while feedback tracks.
                # The diagnostic marks *tracking* limitation only once measured
                # lead is material and an outward goal is actually shortened.
                outward = (allowed < 1.-1e-12 and error*delta > 0.
                           and abs(error) >= self.lead_rad*2/3)
                # Observe a persistent outward request only after the reference
                # has slowed. Range, rather than endpoint displacement, keeps
                # real back-and-forth feedback motion from looking stationary.
                tracking_limited = tracking_limited or outward
                watching = outward and abs(velocities[name]) <= .03
                if not watching:
                    self._watches.pop(name, None)
                    continue
                direction = 1 if error > 0 else -1
                age = self._sample_watch(name, feedback[name], direction, now_ns)
                stall_age = max(stall_age, age)
                if age >= self.stalled_after_s and stalled is None:
                    stalled = name
            scales[side] = factor
            for name in SIDES[side]:
                governed[name] = previous[name]+factor*(targets[name]-previous[name])
        return TrackingGoal(MappingProxyType(governed), MappingProxyType(scales),
            tuple(limited), max_lead, tracking_limited, stalled, stall_age)
