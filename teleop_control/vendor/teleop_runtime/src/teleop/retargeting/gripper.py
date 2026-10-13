"""Map normalized operator triggers to independently gated gripper intents."""

from collections.abc import Mapping
import math

from teleop.contracts.intent import GripperIntent

TRIGGER_NEUTRAL_THRESHOLD = 0.05


class GripperMapper:
    """Create commands only after a safe baseline and a meaningful change."""

    _SIDES = ("left", "right")

    def __init__(
        self,
        *,
        min_delta: float = 0.005,
        min_period_s: float = 0.05,
        max_step_fraction: float = 1.0,
    ) -> None:
        values = {
            "min_delta": min_delta,
            "min_period_s": min_period_s,
            "max_step_fraction": max_step_fraction,
        }
        for name, value in values.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0.0
            ):
                raise ValueError(f"{name} must be finite and positive")
        if min_delta > 1.0 or max_step_fraction > 1.0:
            raise ValueError("gripper fractions must not exceed 1")
        self._min_delta = float(min_delta)
        self._min_period_ns = int(float(min_period_s) * 1_000_000_000)
        self._max_step = float(max_step_fraction)
        self._accepted: dict[str, float] = {}
        self._last_sent_ns: dict[str, int] = {}
        self._armed: dict[str, bool] = {}
        self._input_baseline: dict[str, float] = {}
        self._operator_changed: set[str] = set()

    @property
    def initialized(self) -> bool:
        return set(self._accepted) == set(self._SIDES)

    def prime(self, physical_fractions: Mapping[str, float]) -> None:
        """Set the step reference from robot feedback without sending."""
        normalized = dict(GripperIntent(physical_fractions).targets)
        missing = set(self._SIDES) - set(normalized)
        if missing:
            raise ValueError(f"gripper feedback is missing sides: {sorted(missing)}")
        self._accepted = normalized
        self._last_sent_ns.clear()
        self._armed = {side: False for side in self._SIDES}
        self._input_baseline.clear()
        self._operator_changed.clear()

    def prime_side(self, side: str, physical_fraction: float) -> None:
        """Refresh one failed side from feedback without disturbing the other."""
        normalized = dict(GripperIntent({side: physical_fraction}).targets)
        self._accepted[side] = normalized[side]
        self._last_sent_ns.pop(side, None)
        self._armed[side] = False
        self._input_baseline.pop(side, None)
        self._operator_changed.discard(side)

    def block(self, side: str) -> None:
        """Require one trigger to return neutral after an unknown send outcome."""
        if side not in self._SIDES:
            raise ValueError(f"unsupported gripper side: {side!r}")
        self._armed[side] = False
        self._input_baseline.pop(side, None)
        self._operator_changed.discard(side)

    def reset(self) -> None:
        self._accepted.clear()
        self._last_sent_ns.clear()
        self._armed.clear()
        self._input_baseline.clear()
        self._operator_changed.clear()

    def propose(
        self,
        fractions: Mapping[str, float],
        *,
        now_ns: int,
    ) -> GripperIntent | None:
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a non-negative integer")
        current = dict(GripperIntent(fractions).targets)
        missing = set(self._SIDES) - set(current)
        if missing:
            raise ValueError(f"gripper sample is missing sides: {sorted(missing)}")
        if not self.initialized:
            raise RuntimeError("prime gripper mapper from robot feedback before use")
        targets = {}
        for side in self._SIDES:
            if not self._armed[side]:
                if current[side] <= TRIGGER_NEUTRAL_THRESHOLD:
                    self._armed[side] = True
                    self._input_baseline[side] = current[side]
                continue
            if side not in self._operator_changed:
                if abs(current[side] - self._input_baseline[side]) < self._min_delta:
                    continue
                self._operator_changed.add(side)
            previous = self._accepted[side]
            if abs(current[side] - previous) < self._min_delta:
                continue
            last_sent_ns = self._last_sent_ns.get(side)
            if last_sent_ns is not None and now_ns - last_sent_ns < self._min_period_ns:
                continue
            requested_delta = current[side] - previous
            if abs(requested_delta) <= self._max_step:
                targets[side] = current[side]
            else:
                targets[side] = previous + math.copysign(
                    self._max_step,
                    requested_delta,
                )
        return GripperIntent(targets) if targets else None

    def commit(self, intent: GripperIntent, *, now_ns: int) -> None:
        if not isinstance(intent, GripperIntent):
            raise TypeError("intent must be GripperIntent")
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("now_ns must be a non-negative integer")
        for side, value in intent.targets.items():
            self._accepted[side] = value
            self._last_sent_ns[side] = now_ns
