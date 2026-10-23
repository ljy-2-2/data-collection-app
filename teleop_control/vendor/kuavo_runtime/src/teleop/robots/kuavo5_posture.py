"""Shared official-IK elbow preference; no control or independent seed history.

The seed must be selected by the existing validated-seed owner. This adapter
keeps no independent history, cannot refresh a lease, and never changes the hand
target. Robot elbow FK is a posture preference, NOT measured human body data.
"""
from copy import deepcopy
import math

from teleop.robots.kuavo5_native_ik import KuavoNativeIKSolver


def validate_geometry(value):
    """Validate the explicitly fixed-base, 14-joint model geometry reply."""
    from teleop.robots.kuavo5_numeric_process import finite_vector
    if not isinstance(value, dict) or value.get('frame_id') != 'base_link':
        raise ValueError('base_link geometry required')
    arms = value.get('arms')
    if not isinstance(arms, dict) or set(arms) != {'left', 'right'}:
        raise ValueError('both arm geometries required')
    for arm in arms.values():
        if not isinstance(arm, dict) or set(arm) != {'shoulder', 'elbow', 'wrist', 'hand'}:
            raise ValueError('shoulder/elbow/wrist/hand geometry required')
        for point in arm.values():
            finite_vector(point, 3)
        for a, b in (('shoulder', 'elbow'), ('elbow', 'wrist')):
            distance = math.dist(arm[a], arm[b])
            if not math.isfinite(distance) or not 1e-5 < distance < 2.:
                raise ValueError('degenerate or implausible arm geometry')
    return value


class ElbowPreferenceBackend:
    """Explicit mathematical preference on top of an injected official-math backend.

    enabled=False is exactly the original call path (no geometry query).
    Positive weight is relative to official position cost. Zero cannot mean
    disabled here: the vendor assigns zero its built-in default weight.
    No gravity projection, elbow-circle branch, fake human pose or shoulder lock.
    """
    def __init__(self, backend, *, enabled=False, weight=.1,
                 active_sides=('left', 'right')):
        if type(enabled) is not bool:
            raise ValueError('explicit boolean enabled required')
        if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 < weight <= .3:
            raise ValueError('candidate weight must be in (0, 0.3]')
        sides = tuple(active_sides)
        if not sides or len(set(sides)) != len(sides) or set(sides)-{'left', 'right'}:
            raise ValueError('distinct active arms required')
        for name in ('forward', 'inverse') + (('geometry',) if enabled else ()):
            if not callable(getattr(backend, name, None)):
                raise ValueError('explicit mathematical backend required: '+name)
        self.backend, self.enabled, self.weight, self.active_sides = backend, enabled, float(weight), sides

    def forward(self, q):
        return self.backend.forward(q)

    def inverse(self, request):
        return self.backend.inverse(self.prepare_request(request))

    def prepare_request(self, request, *, geometry_cache=None):
        if not self.enabled:
            return request
        from teleop.robots.kuavo5_numeric_process import validate_request
        validate_request(request)
        if any(any(x != 0. for x in h['elbow_pos_xyz']) for h in request['hand_poses'].values()):
            raise ValueError('existing elbow target must not be overwritten')
        seed = [x for side in ('left', 'right') for x in request['hand_poses'][side+'_pose']['joint_angles']]
        key = tuple(seed)
        if geometry_cache is not None and key in geometry_cache:
            geometry = validate_geometry(geometry_cache[key])
        else:
            geometry = validate_geometry(self.backend.geometry(seed))
            if geometry_cache is not None:
                geometry_cache[key] = deepcopy(geometry)
        candidate = deepcopy(request)
        for side in self.active_sides:
            elbow = geometry['arms'][side]['elbow']
            if math.dist(elbow, (0., 0., 0.)) < 1e-8:
                raise ValueError('elbow coincides with vendor disable sentinel')
            candidate['hand_poses'][side+'_pose']['elbow_pos_xyz'] = list(elbow)
        candidate['ik_param']['elbow_cost_scale'] = self.weight
        return candidate


def posture_profile(arm_posture, target_recovery):
    if arm_posture not in ('baseline', 'elbow_preference', 'elbow_preference_v2'):
        raise ValueError('unknown arm posture method')
    if arm_posture in ('elbow_preference', 'elbow_preference_v2') and target_recovery != 'bounded_candidate':
        raise ValueError('elbow preference requires bounded_candidate official mathematics')
    return dict(arm_posture=arm_posture,
                elbow_preference_weight=.1 if arm_posture in ('elbow_preference', 'elbow_preference_v2') else 0.,
                posture_status='manual_comparison_pending' if arm_posture in ('elbow_preference', 'elbow_preference_v2') else 'existing_baseline')


def validate_posture_ready(packet, arm_posture, target_recovery):
    expected = posture_profile(arm_posture, target_recovery)
    # Old baseline packets predate these fields. A new candidate must identify
    # itself explicitly, including its fixed weight; never silently fall back.
    for key, value in expected.items():
        actual = packet.get(key, value if arm_posture == 'baseline' else None)
        if actual != value:
            raise ValueError('arm posture handshake mismatch: '+key)


class KuavoElbowPreferenceSolver(KuavoNativeIKSolver):
    """Existing solver checks/fallbacks; add only a seed-FK elbow preference.

    Owned by the existing serial solve worker. Grip epochs, seed expiry and
    cancellation remain the responsibility of NativeArmWorkSolver. This class
    stores no candidate/history and never refreshes any input or seed deadline.
    """
    def __init__(self, *args, cache_solve_geometry=False, **kwargs):
        if type(cache_solve_geometry) is not bool:
            raise ValueError('explicit solve-local geometry cache boolean required')
        self._cache_solve_geometry = cache_solve_geometry
        self._posture_geometry_cache = {}
        super().__init__(*args, **kwargs)
        if not callable(getattr(self.backend, 'geometry', None)):
            raise ValueError('elbow preference needs explicit mathematical geometry')
        self._posture_active_sides = ()

    def solve(self, intent, state, **kwargs):
        self._validated_command = self._validated_poses = None
        sides = tuple(getattr(intent, 'targets', ()))
        if not sides or set(sides)-{'left', 'right'}:
            raise ValueError('active arm targets required')
        self._posture_active_sides = sides
        self._posture_geometry_cache.clear()
        try:
            return super().solve(intent, state, **kwargs)
        finally:
            self._posture_active_sides = ()
            self._posture_geometry_cache.clear()

    def _request(self, targets, seed, constraint_mode=None):
        ordinary = super()._request(targets, seed, constraint_mode)
        return ElbowPreferenceBackend(self.backend, enabled=True, weight=.1,
            active_sides=self._posture_active_sides).prepare_request(ordinary,
                geometry_cache=self._posture_geometry_cache if self._cache_solve_geometry else None)
