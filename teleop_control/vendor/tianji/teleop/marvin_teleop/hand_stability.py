"""Per-clutch XR intent filtering; never filters robot feedback or safety flags."""
from collections import deque
import math
import numpy as np
from .differential import rotation_vector
from .geometry import rotation


class HandStability:
    # Hysteresis is relative to a fixed accepted pose, not the last raw frame:
    # deliberate slow motion accumulates and can always leave the hold band.
    POSITION_BAND_M = .0008
    ROTATION_BAND_RAD = math.radians(.2)
    TIME_CONSTANT_S = .025
    SETTLE_WINDOW_S = .15
    RELEASE_CONFIRM_S = .04

    def __init__(self, pose, stamp):
        self.filtered = pose.copy()
        self.target = pose.copy()
        self.stamp = float(stamp)
        self.sample_interval = 0.
        self.history = deque([(self.stamp, self.filtered.copy())])
        self.position_held = self.rotation_held = True
        self.position_center = pose[:3,3].copy()
        self.rotation_center = pose[:3,:3].copy()
        self.position_outside = self.rotation_outside = None

    def _released(self, distance, band, since, stamp):
        if distance <= band:
            return False, None
        since = stamp if since is None else since
        # Clear deliberate movement responds immediately. Borderline tracking
        # noise must persist across real XR frames before ending a joint hold.
        return distance > 2*band or stamp-since >= self.RELEASE_CONFIRM_S-1e-6, since

    def update(self, pose, stamp):
        stamp = float(stamp)
        if not np.isfinite(stamp):
            raise ValueError('invalid hand stability timestamp')
        dt = stamp - self.stamp
        if dt < 0:
            raise ValueError('XR timestamp reversed; release and re-engage')
        if dt <= 1e-6:
            return self.target.copy()  # Cached polling is not another XR sample.
        self.stamp = stamp
        self.sample_interval = dt
        alpha = -math.expm1(-dt / self.TIME_CONSTANT_S)
        self.filtered[:3, 3] += alpha * (pose[:3, 3] - self.filtered[:3, 3])
        delta = rotation_vector(pose[:3, :3] @ self.filtered[:3, :3].T)
        amount = float(np.linalg.norm(delta))
        if amount > 1e-12:
            self.filtered[:3, :3] = rotation(delta / amount, alpha * amount) @ self.filtered[:3, :3]
        self.history.append((stamp, self.filtered.copy()))
        while len(self.history) > 2 and self.history[1][0] <= stamp - self.SETTLE_WINDOW_S:
            self.history.popleft()
        window_ready = stamp - self.history[0][0] >= self.SETTLE_WINDOW_S - 1e-6
        position_delta = np.linalg.norm(self.filtered[:3, 3] - self.position_center)
        rotation_delta = np.linalg.norm(rotation_vector(self.filtered[:3, :3] @ self.rotation_center.T))
        if self.position_held:
            released, self.position_outside = self._released(position_delta, self.POSITION_BAND_M, self.position_outside, stamp)
            self.position_held = not released
        if self.rotation_held:
            released, self.rotation_outside = self._released(rotation_delta, self.ROTATION_BAND_RAD, self.rotation_outside, stamp)
            self.rotation_held = not released
        if not self.position_held:
            self.target[:3, 3] = self.filtered[:3, 3]
            if window_ready:
                points = np.array([p[:3, 3] for _, p in self.history])
                half = len(points)//2
                self.position_held = bool(
                    np.sqrt(np.mean(np.sum((points-points.mean(axis=0))**2,axis=1))) < self.POSITION_BAND_M * .75
                    and np.linalg.norm(points[:half].mean(axis=0)-points[half:].mean(axis=0)) < self.POSITION_BAND_M * .5)
                if self.position_held:
                    self.position_center = points.mean(axis=0)
                    self.position_outside = None
        if not self.rotation_held:
            self.target[:3, :3] = self.filtered[:3, :3]
            if window_ready:
                offsets = np.array([rotation_vector(p[:3, :3] @ self.filtered[:3, :3].T)
                                    for _, p in self.history])
                half = len(offsets)//2
                self.rotation_held = bool(
                    np.sqrt(np.mean(np.sum((offsets-offsets.mean(axis=0))**2,axis=1))) < self.ROTATION_BAND_RAD * .75
                    and np.linalg.norm(offsets[:half].mean(axis=0)-offsets[half:].mean(axis=0)) < self.ROTATION_BAND_RAD * .5)
                if self.rotation_held:
                    mean = offsets.mean(axis=0); length = np.linalg.norm(mean)
                    self.rotation_center = (rotation(mean/length,length) @ self.filtered[:3,:3]
                                            if length>1e-12 else self.filtered[:3,:3].copy())
                    self.rotation_outside = None
        return self.target.copy()
