"""Thin wrapper over agibot_gdk handles: name maps, decoding, safe pulls.

GDK handles are NOT assumed thread-safe. Each handle created here must only
be used from the thread that owns it (the collector gives each fetch worker
its own Camera instance).
"""

from __future__ import annotations

import time

import numpy as np

import agibot_gdk as gdk

from .bundle_codec import decode_scalar_u64, decode_tensor

CAM_TS_PREFIX = "timestamp__camera__"
ACTION_PREFIX = "teleop__action__"


def _snake_to_enum(name: str) -> str:
    return "k" + "".join(p.capitalize() for p in name.split("_"))


def camera_type(name: str):
    """snake_case camera name (as used in bundle keys) -> CameraType."""
    enum_name = _snake_to_enum(name)
    ct = getattr(gdk.CameraType, enum_name, None)
    if ct is None:
        valid = sorted(m for m in dir(gdk.CameraType) if m.startswith("k"))
        raise ValueError(f"unknown camera {name!r} ({enum_name}); valid: {valid}")
    return ct


def imu_type(name: str):
    it = getattr(gdk.ImuType, f"kImu{name.capitalize()}", None)
    if it is None:
        raise ValueError(f"unknown imu {name!r} (front|back|chassis)")
    return it


def lidar_type(name: str):
    lt = getattr(gdk.LidarType, f"kLidar{name.capitalize()}", None)
    if lt is None:
        raise ValueError(f"unknown lidar {name!r} (front|back)")
    return lt


def pc_now_ns() -> int:
    return time.time_ns()


class BundleSource:
    """Owns the Bundle handle; poll() dedupes by timestamp_ns.

    The pybind BundleData exposes only {bundle_info, timestamp_ns} — no seq
    index — so dedupe and continuity both key on the trigger timestamp
    (verified live 2026-07-16, ~30.3 Hz unique timestamps).
    """

    def __init__(self):
        self._bundle = gdk.Bundle()
        self._last_ts = 0

    def poll(self):
        """Return a new BundleData or None if nothing new is buffered."""
        bd = self._bundle.get_latest_bundle(0.0)  # timeout param is dead in the impl
        if bd is None or bd.timestamp_ns <= self._last_ts:
            return None
        self._last_ts = bd.timestamp_ns
        return bd

    def close(self):
        try:
            self._bundle.close()
        except Exception:
            pass


def split_bundle(bd) -> tuple[dict, dict[str, int]]:
    """bundle_info -> (decoded tensors by key, camera name -> declared ts)."""
    tensors: dict = {}
    cam_ts: dict[str, int] = {}
    info = bd.bundle_info
    for key in info.keys():
        if key.startswith(CAM_TS_PREFIX):
            cam_ts[key[len(CAM_TS_PREFIX):]] = decode_scalar_u64(info[key])
        else:
            tensors[key] = decode_tensor(info[key])
    return tensors, cam_ts


class CameraFetcher:
    """One Camera handle + exact-timestamp fetch with retries.

    Stock integrity rule (FilterMessagesWithImages) applied live: the image
    is good only if img.timestamp_ns == the bundle's declared ts, exactly.
    Retries cover in-flight frames (large stereo frames can arrive a few ms
    after the bundle that references them).
    """

    def __init__(self):
        self._camera = gdk.Camera()

    def fetch_exact(self, cam_type, ts_ns: int, retries: int, retry_sleep_ms: float):
        attempt = 0
        while True:
            try:
                img = self._camera.get_nearest_image(cam_type, ts_ns, 0.0)
            except RuntimeError:
                # pybind raises instead of returning None when the ring
                # buffer for this camera is empty (e.g. right after init)
                img = None
            if img is not None and img.timestamp_ns == ts_ns:
                return img, attempt
            if attempt >= retries:
                return None, attempt
            attempt += 1
            time.sleep(retry_sleep_ms / 1000.0)

    def close(self):
        try:
            self._camera.close_camera()
        except Exception:
            pass


class ImuSource:
    def __init__(self, names: list[str]):
        self._imu = gdk.Imu() if names else None
        self._types = {n: imu_type(n) for n in names}

    def sample_nearest(self, ts_ns: int) -> dict[str, dict]:
        out = {}
        for name, it in self._types.items():
            d = self._imu.get_nearest_imu(it, ts_ns, 0.0)
            if d is None:
                continue
            out[name] = {
                "timestamp_ns": int(d.timestamp_ns),
                "angular_velocity": _vec3(d.angular_velocity),
                "linear_acceleration": _vec3(d.linear_acceleration),
            }
        return out

    def close(self):
        if self._imu is not None:
            try:
                self._imu.close_imu()
            except Exception:
                pass


class LidarSource:
    def __init__(self, names: list[str]):
        self._lidar = gdk.Lidar() if names else None
        self._types = {n: lidar_type(n) for n in names}

    def sample_nearest(self, ts_ns: int) -> dict[str, object]:
        out = {}
        for name, lt in self._types.items():
            pc = self._lidar.get_nearest_pointcloud(lt, ts_ns, 0.0)
            if pc is not None:
                out[name] = pc
        return out

    def close(self):
        if self._lidar is not None:
            try:
                self._lidar.close_lidar()
            except Exception:
                pass


def _vec3(v) -> list[float]:
    try:
        return [float(v.x), float(v.y), float(v.z)]
    except AttributeError:
        return [float(x) for x in np.asarray(v).ravel()[:3]]
