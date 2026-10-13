"""The collector core: bundle-paced poll loop + camera fetch workers.

Pace by arrival, not wall clock: poll get_latest_bundle at ~poll_hz and act
only on a new timestamp_ns. Camera pixels are fetched off the poll thread by
persistent workers (one GDK Camera handle per worker, created on the worker
thread and warmed up once at startup — handles are not assumed thread-safe),
keyed to each bundle's declared per-camera timestamp with the exact-match
integrity rule enforced live.
"""

from __future__ import annotations

import queue
import threading
import time

from .config import CollectorCfg
from .episode_writer import EpisodeWriter
from .frame_gate import Counters, FrameGate
from . import gdk_io


class _FetchWorker(threading.Thread):
    """Owns one Camera handle; serves exact-ts fetches for a shard of cameras.

    Persistent across episodes: set_episode() points it at the current
    episode's writers/counters, end_episode() drains the queue and detaches.
    """

    def __init__(self, wid: int, cam_cfgs: dict, depth: int):
        super().__init__(name=f"fetch-{wid}", daemon=True)
        self.q: queue.Queue = queue.Queue(maxsize=depth)
        self._cam_cfgs = cam_cfgs           # name -> CameraCfg (this shard only)
        self._writers: dict | None = None
        self._counters: Counters | None = None
        self._lock = threading.Lock()
        self._fetcher = None                # created on-thread
        self._shutdown = threading.Event()
        self.ready = threading.Event()

    def set_episode(self, writers: dict, counters: Counters):
        with self._lock:
            self._writers = writers
            self._counters = counters

    def end_episode(self):
        # Wait for queued fetches of this episode to finish, then detach.
        deadline = time.time() + 10.0
        while not self.q.empty() and time.time() < deadline and self.is_alive():
            time.sleep(0.02)
        self.q.join() if self.is_alive() else None
        with self._lock:
            self._writers = None
            self._counters = None

    def submit(self, cam: str, ts_ns: int) -> bool:
        try:
            self.q.put_nowait((cam, ts_ns))
            return True
        except queue.Full:
            with self._lock:
                if self._counters:
                    self._counters.bump(f"drop.queue_full.{cam}")
            return False

    def run(self):
        self._fetcher = gdk_io.CameraFetcher()
        cam_types = {n: gdk_io.camera_type(n) for n in self._cam_cfgs}
        self.ready.set()
        while not self._shutdown.is_set():
            try:
                cam, ts_ns = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    writers, counters = self._writers, self._counters
                if writers is None or counters is None:
                    continue  # between episodes; late items are dropped
                cfg = self._cam_cfgs[cam]
                img, retries = self._fetcher.fetch_exact(
                    cam_types[cam], ts_ns, cfg.retries, cfg.retry_sleep_ms)
                if retries:
                    counters.bump(f"retry.{cam}", retries)
                if img is None:
                    counters.bump(f"miss.exact.{cam}")
                    continue
                try:
                    writers[cam].write(img)
                    counters.bump(f"frames.{cam}")
                except Exception as e:  # disk full etc. — count, don't die
                    counters.bump(f"error.write.{cam}")
                    print(f"[collector] write error {cam}: {e}")
            finally:
                self.q.task_done()

    def shutdown(self):
        self._shutdown.set()

    def close(self):
        if self._fetcher is not None:
            self._fetcher.close()


class Collector:
    """One Collector per process; run_episode() may be called repeatedly."""

    def __init__(self, cfg: CollectorCfg):
        self.cfg = cfg
        self._bundle_src = gdk_io.BundleSource()
        self._imu_src = gdk_io.ImuSource([i.name for i in cfg.imus])
        self._lidar_src = gdk_io.LidarSource([l.name for l in cfg.lidars])
        self._us = None
        if cfg.ultrasonic:
            import agibot_gdk as gdk
            self._us = gdk.UltrasonicRadar()

        # Persistent fetch workers, cameras sharded round-robin in config order.
        wanted = list(cfg.cameras)
        n_workers = max(1, min(cfg.fetch_workers, len(wanted))) if wanted else 0
        self._workers: list[_FetchWorker] = []
        self._cam_to_worker: dict[str, _FetchWorker] = {}
        for wid in range(n_workers):
            shard = {c.name: c for i, c in enumerate(wanted) if i % n_workers == wid}
            w = _FetchWorker(wid, shard, cfg.queue_depth)
            self._workers.append(w)
            for name in shard:
                self._cam_to_worker[name] = w
            w.start()
        for w in self._workers:
            w.ready.wait(timeout=30.0)
        # One warm-up window for DDS discovery on ALL handles (bundle + cameras).
        time.sleep(3.0)

    def run_episode(self, writer: EpisodeWriter, duration_s: float | None,
                    stop_event: threading.Event) -> dict:
        cfg = self.cfg
        counters = Counters()
        gate = FrameGate(cfg.cameras, cfg.bundle.essential_keys, counters)
        wanted_cams = [c.name for c in cfg.cameras]
        for w in self._workers:
            w.set_episode(writer.cams, counters)

        keep_keys = None if cfg.bundle.record_all else set(cfg.bundle.keys)
        lidar_every = {l.name: l.every_n_frames for l in cfg.lidars}
        poll_dt = 1.0 / cfg.poll_hz
        clock_dt = 1.0 / cfg.clock_offset_hz if cfg.clock_offset_hz > 0 else None
        last_clock = 0.0
        last_stat = time.time()
        seq = 0
        prev_ts = None
        t_end = time.time() + duration_s if duration_s else None

        print(f"[collector] episode {writer.episode_id} recording "
              f"({len(wanted_cams)} cams, {len(self._workers)} workers)")
        while not stop_event.is_set():
            now = time.time()
            if t_end and now >= t_end:
                break
            bd = self._bundle_src.poll()
            if bd is None:
                time.sleep(poll_dt)
                continue
            pc_ns = time.time_ns()
            ts = bd.timestamp_ns
            if prev_ts is not None:
                gap_ms = (ts - prev_ts) / 1e6
                if gap_ms > 50.0:  # > ~1.5 bundle periods -> producer/collector gap
                    counters.bump("gap.bundle_over_50ms")
            prev_ts = ts

            tensors, cam_ts = gdk_io.split_bundle(bd)
            if keep_keys is not None:
                tensors = {k: v for k, v in tensors.items() if k in keep_keys}
            verdict = gate.check(ts, tensors, cam_ts, wanted_cams)
            if not verdict.accept:
                continue

            seq += 1
            for cam in wanted_cams:
                declared = cam_ts.get(cam)
                if declared is not None:
                    self._cam_to_worker[cam].submit(cam, declared)

            writer.write_bundle_row(ts, seq, tensors,
                                    {c: cam_ts[c] for c in wanted_cams if c in cam_ts},
                                    verdict.cam_delta_ms, pc_ns)

            if self.cfg.imus:
                samples = self._imu_src.sample_nearest(ts)
                if samples:
                    writer.imu.write({"bundle_ts_ns": ts, "imus": samples})
            for name, every in lidar_every.items():
                if seq % every == 0:
                    pcs = self._lidar_src.sample_nearest(ts)
                    if name in pcs:
                        writer.write_lidar(name, pcs[name])
            if self._us is not None:
                us = self._us.get_nearest_ultrasonic_radar(ts)
                if us is not None:
                    writer.ultrasonic.write({
                        "bundle_ts_ns": ts,
                        "timestamp_ns": int(us.timestamp_ns),
                        "ranges": [
                            {k: getattr(d, k) for k in dir(d)
                             if not k.startswith("_") and not callable(getattr(d, k))}
                            for d in us.ultrasonic_radar_datas
                        ],
                    })

            if clock_dt and (now - last_clock) >= clock_dt:
                writer.clock_offset.write({"robot_ts_ns": ts, "pc_ts_ns": pc_ns})
                last_clock = now
            if now - last_stat >= 5.0:
                snap = counters.snapshot()
                acc = snap.get("frames.accepted", 0)
                misses = sum(v for k, v in snap.items() if k.startswith("miss."))
                print(f"[collector] {acc} frames, {misses} cam misses, "
                      f"qdrops={sum(v for k, v in snap.items() if k.startswith('drop.'))}")
                last_stat = now

        for w in self._workers:
            w.end_episode()
        meta = writer.finalize(counters.snapshot(), self.cfg.cameras)
        n = meta["frames_recorded"]
        print(f"[collector] episode {writer.episode_id}: {n} frames recorded")
        for cam, info in meta["cameras"].items():
            pct = 100.0 * info["frames"] / n if n else 0.0
            print(f"[collector]   {cam}: {info['frames']} frames ({pct:.1f}% of bundles)")
        return meta

    def close(self):
        for w in self._workers:
            w.shutdown()
        for w in self._workers:
            w.join(timeout=5.0)
            w.close()
        self._bundle_src.close()
        self._imu_src.close()
        self._lidar_src.close()
