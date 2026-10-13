#!/usr/bin/env python3
"""M0 diagnostic: dump the live bundle schema, camera payloads, and rates.

    source env.sh && $GDK_PY scripts/probe.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import agibot_gdk as gdk  # noqa: E402

from collector import gdk_io  # noqa: E402
from collector.bundle_codec import decode_tensor  # noqa: E402


def main():
    print("initializing GDK (DDS discovery ~3 s)...")
    src = gdk_io.BundleSource()
    fetcher = gdk_io.CameraFetcher()
    time.sleep(3.0)

    bd = None
    for _ in range(100):
        bd = src.poll()
        if bd is not None:
            break
        time.sleep(0.05)
    if bd is None:
        print("no bundle received — is the robot on this LAN and cosine running?")
        sys.exit(1)

    tensors, cam_ts = gdk_io.split_bundle(bd)
    print(f"\nbundle timestamp_ns: {bd.timestamp_ns}")
    print(f"tensors ({len(tensors)}):")
    for k in sorted(tensors):
        v = tensors[k]
        if isinstance(v, list):
            print(f"  {k}: strings {v}")
        else:
            print(f"  {k}: {v.dtype}{list(v.shape)} = {np.round(v.astype(float), 4).tolist()}"
                  if v.size <= 8 else f"  {k}: {v.dtype}{list(v.shape)}")
    print(f"\ncameras declared in bundle ({len(cam_ts)}):")
    for cam, ts in sorted(cam_ts.items()):
        img, _ = fetcher.fetch_exact(gdk_io.camera_type(cam), ts, retries=2,
                                     retry_sleep_ms=5.0)
        if img is None:
            print(f"  {cam}: declared ts {ts} — exact frame NOT in buffer")
        else:
            print(f"  {cam}: {img.width}x{img.height} {img.encoding} "
                  f"{img.color_format} {len(img.data)} B  exact-match OK  "
                  f"delta_vs_bundle={(bd.timestamp_ns - ts) / 1e6:.2f} ms")

    print("\nmeasuring bundle rate over 5 s...")
    n = 0
    t0 = time.time()
    while time.time() - t0 < 5.0:
        if src.poll() is not None:
            n += 1
        time.sleep(0.003)
    print(f"bundle rate: {n / 5.0:.1f} Hz")
    print(f"PC clock (gdk.Clock.now_ns): {gdk.Clock.now_ns()}")

    fetcher.close()
    src.close()


if __name__ == "__main__":
    main()
