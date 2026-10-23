"""Robot-independent, instantaneous checks of explicitly supplied capsules.

Endpoints must already be transformed into one common fixed frame (e.g. by FK).
No robot dimensions, adjacency policy, SDK calls or live-motion approval live
here. A clear result covers only the supplied primitives at this one posture;
it says nothing about model coverage, swept motion, obstacles or stability.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
import math


Vector3 = tuple[float, float, float]


def _number(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("geometry values must be finite numbers")
    return float(value)


def _point(values: Sequence[float]) -> Vector3:
    if len(values) != 3:
        raise ValueError("a point must contain three coordinates")
    return tuple(_number(value) for value in values)


def _sub(a: Vector3, b: Vector3) -> Vector3:
    return tuple(x - y for x, y in zip(a, b))


def _dot(a: Vector3, b: Vector3) -> float:
    return sum(x * y for x, y in zip(a, b))


def _cross(a: Vector3, b: Vector3) -> Vector3:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _along(point: Vector3, vector: Vector3, fraction: float) -> Vector3:
    return tuple(p + fraction * v for p, v in zip(point, vector))


def _point_segment_distance(point: Vector3, start: Vector3, end: Vector3) -> float:
    vector = _sub(end, start)
    length_squared = _dot(vector, vector)
    if length_squared == 0:
        return math.dist(point, start)
    fraction = _dot(_sub(point, start), vector) / length_squared
    if not math.isfinite(fraction):
        raise ValueError("geometry exceeds numerical range")
    closest = _along(start, vector, min(1.0, max(0.0, fraction)))
    return math.dist(point, closest)


def _segment_distance(a: "Capsule", b: "Capsule") -> float:
    # The constrained minimum is either an interior stationary point or on
    # one of the four boundaries of [0,1] x [0,1]. Boundary projections also
    # handle parallel segments and zero-length segments (spheres).
    distances = [
        _point_segment_distance(a.start_m, b.start_m, b.end_m),
        _point_segment_distance(a.end_m, b.start_m, b.end_m),
        _point_segment_distance(b.start_m, a.start_m, a.end_m),
        _point_segment_distance(b.end_m, a.start_m, a.end_m),
    ]
    u, v = _sub(a.end_m, a.start_m), _sub(b.end_m, b.start_m)
    w = _sub(a.start_m, b.start_m)
    cross = _cross(u, v)
    denominator = _dot(cross, cross)
    # Cross products avoid subtracting nearly equal squared dot products.
    if not math.isfinite(denominator):
        raise ValueError("geometry exceeds numerical range")
    if denominator > 0:
        s = _dot(_cross(v, w), cross) / denominator
        t = _dot(_cross(u, w), cross) / denominator
        if not math.isfinite(s) or not math.isfinite(t):
            raise ValueError("geometry exceeds numerical range")
        if 0 <= s <= 1 and 0 <= t <= 1:
            distances.append(math.dist(_along(a.start_m, u, s), _along(b.start_m, v, t)))
    if not all(math.isfinite(value) for value in distances):
        raise ValueError("geometry exceeds numerical range")
    return min(distances)


@dataclass(frozen=True)
class Capsule:
    """One named thick segment in a specified common coordinate frame, in metres."""

    name: str
    frame_id: str
    start_m: Vector3
    end_m: Vector3
    radius_m: float

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip()
               for value in (self.name, self.frame_id)):
            raise ValueError("geometry name and frame must be nonempty strings")
        object.__setattr__(self, "start_m", _point(self.start_m))
        object.__setattr__(self, "end_m", _point(self.end_m))
        radius = _number(self.radius_m)
        if radius <= 0:
            raise ValueError("radius must be positive")
        object.__setattr__(self, "radius_m", radius)


@dataclass(frozen=True)
class CapsuleCheckResult:
    checked_pairs: int
    minimum_surface_gap_m: float
    violating_pairs: tuple[tuple[str, str], ...]

    @property
    def clear_for_supplied_geometry(self) -> bool:
        """Not a claim of robot safety or permission to send a command."""
        return self.checked_pairs > 0 and not self.violating_pairs


class CapsuleCollisionChecker:
    """Check all required primitives; exclusions are explicit reviewed policy."""

    def __init__(
        self,
        required_names: Sequence[str],
        *,
        margin_m: float = 0.0,
        exclusions: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        names = tuple(required_names)
        if (len(names) < 2 or any(not isinstance(n, str) or not n.strip() for n in names)
                or len(set(names)) != len(names)):
            raise ValueError("at least two unique nonempty geometry names are required")
        margin = _number(margin_m)
        if margin < 0:
            raise ValueError("margin must be nonnegative")
        excluded = set()
        for pair, reason in (exclusions or {}).items():
            if (not isinstance(pair, tuple) or len(pair) != 2 or pair[0] == pair[1]
                    or any(name not in names for name in pair)
                    or not isinstance(reason, str) or not reason.strip()):
                raise ValueError("exclusion requires two known different names and a reason")
            canonical = tuple(sorted(pair))
            if canonical in excluded:
                raise ValueError("duplicate exclusion pair")
            excluded.add(canonical)
        self._pairs = tuple(pair for pair in combinations(sorted(names), 2) if pair not in excluded)
        if not self._pairs:
            raise ValueError("cannot exclude every pair")
        self._required_names = frozenset(names)
        self._margin_m = margin

    def check(self, capsules: Sequence[Capsule]) -> CapsuleCheckResult:
        bodies = {body.name: body for body in capsules}
        if len(bodies) != len(capsules) or set(bodies) != self._required_names:
            raise ValueError("supplied geometry must match required names exactly, without duplicates")
        if len({body.frame_id for body in capsules}) != 1:
            raise ValueError("all geometry must use the same coordinate frame")
        minimum = math.inf
        violations = []
        for first, second in self._pairs:
            a, b = bodies[first], bodies[second]
            gap = _segment_distance(a, b) - a.radius_m - b.radius_m
            if not math.isfinite(gap):
                raise ValueError("geometry exceeds numerical range")
            minimum = min(minimum, gap)
            # Touching the configured margin is rejected, with a tiny allowance
            # for floating-point rounding toward the unsafe side.
            if gap <= self._margin_m + 1e-12:
                violations.append((first, second))
        return CapsuleCheckResult(len(self._pairs), minimum, tuple(violations))
