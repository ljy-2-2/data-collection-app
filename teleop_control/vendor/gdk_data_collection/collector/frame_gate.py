"""Live integrity gate + drop counters.

Deltas vs stock pipeline, on purpose (FINDINGS.md weaknesses):
  - staleness is ENFORCED live (stock: dead tolerance param, warn-only);
  - per-source delta_ns is RECORDED per frame (stock never measures it);
  - a missing NON-essential camera marks that camera, not the whole frame
    (stock's all-or-nothing filter compounds losses across cameras).
"""

from __future__ import annotations

import dataclasses
import threading


@dataclasses.dataclass
class FrameVerdict:
    accept: bool
    reasons: list[str] = dataclasses.field(default_factory=list)
    cam_delta_ms: dict[str, float] = dataclasses.field(default_factory=dict)


class Counters:
    def __init__(self):
        self._lock = threading.Lock()
        self._c: dict[str, int] = {}

    def bump(self, key: str, n: int = 1):
        with self._lock:
            self._c[key] = self._c.get(key, 0) + n

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._c.items()))


class FrameGate:
    """Pre-fetch gate: bundle-level checks that don't need pixels."""

    def __init__(self, cameras, essential_keys: list[str], counters: Counters):
        self._budget_ms = {c.name: c.staleness_budget_ms for c in cameras}
        self._essential_cams = {c.name for c in cameras if c.essential}
        self._essential_keys = list(essential_keys)
        self._counters = counters

    def check(self, bundle_ts_ns: int, tensors: dict, cam_ts: dict[str, int],
              wanted_cams: list[str]) -> FrameVerdict:
        v = FrameVerdict(accept=True)

        for key in self._essential_keys:
            if key not in tensors:
                v.accept = False
                v.reasons.append(f"missing_tensor:{key}")
                self._counters.bump(f"reject.missing_tensor.{key}")

        for cam in wanted_cams:
            declared = cam_ts.get(cam)
            if declared is None:
                if cam in self._essential_cams:
                    v.accept = False
                    v.reasons.append(f"no_cam_ts:{cam}")
                    self._counters.bump(f"reject.no_cam_ts.{cam}")
                else:
                    self._counters.bump(f"mark.no_cam_ts.{cam}")
                continue
            delta_ms = (bundle_ts_ns - declared) / 1e6
            v.cam_delta_ms[cam] = delta_ms
            if delta_ms > self._budget_ms.get(cam, 50.0):
                if cam in self._essential_cams:
                    v.accept = False
                    v.reasons.append(f"stale:{cam}:{delta_ms:.1f}ms")
                    self._counters.bump(f"reject.stale.{cam}")
                else:
                    self._counters.bump(f"mark.stale.{cam}")

        if v.accept:
            self._counters.bump("frames.accepted")
        else:
            self._counters.bump("frames.rejected")
        return v
