#!/usr/bin/env python3
"""Live 3-D trajectory window fed by lines on stdin.

Backward-compatible ``x y z`` lines append executed points. ``T x y z`` adds
points to the complete target path and ``A x y z`` appends actual points.
"""

import argparse
import queue
import sys
import threading

import numpy as np


def read_points(points):
    try:
        for line in sys.stdin:
            fields = line.split()
            if len(fields) == 3:
                kind, values = "A", fields
            elif len(fields) == 4 and fields[0] in ("T", "A"):
                kind, values = fields[0], fields[1:]
            else:
                continue
            point = np.asarray([float(value) for value in values], dtype=float)
            if np.all(np.isfinite(point)):
                points.put((kind, point))
    finally:
        points.put(None)


def equal_limits(ax, xyz):
    lo = np.min(xyz, axis=0)
    hi = np.max(xyz, axis=0)
    center = (lo + hi) * 0.5
    radius = max(0.15, float(np.max(hi - lo)) * 0.6)
    ax.set_xlim(center[0] - radius, center[0] + radius)
    ax.set_ylim(center[1] - radius, center[1] + radius)
    ax.set_zlim(center[2] - radius, center[2] + radius)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frame-label", default="head camera optical frame")
    ap.add_argument("--max-points", type=int, default=3000)
    args = ap.parse_args()

    # Force an interactive backend. Importing pyplot before this selection can
    # silently choose the non-interactive Agg backend in a subprocess.
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt

    incoming = queue.Queue()
    threading.Thread(target=read_points, args=(incoming,), daemon=True).start()

    plt.ion()
    fig = plt.figure("Right EE trajectory — head camera frame")
    ax = fig.add_subplot(111, projection="3d")
    target_line, = ax.plot(
        [], [], [], color="#8e8e93", linewidth=1.5, linestyle="--",
        label="target",
    )
    line, = ax.plot([], [], [], color="#147df5", linewidth=2.2, label="actual")
    tip, = ax.plot([], [], [], "o", color="#ff3b30", markersize=7)
    ax.scatter([0.0], [0.0], [0.0], color="black", marker="+", s=60)
    ax.set_xlabel("X right [m]")
    ax.set_ylabel("Y down [m]")
    ax.set_zlabel("Z forward [m]")
    ax.set_title(f"Right arm end-effector\n{args.frame_label}")
    ax.legend(loc="upper right")
    ax.view_init(elev=-20, azim=-65)
    ax.grid(True)

    target = []
    history = []
    alive = True
    while alive and plt.fignum_exists(fig.number):
        changed = False
        while True:
            try:
                item = incoming.get_nowait()
            except queue.Empty:
                break
            if item is None:
                alive = False
                break
            kind, point = item
            if kind == "T":
                target.append(point)
            else:
                history.append(point)
                if len(history) > args.max_points:
                    del history[:len(history) - args.max_points]
            changed = True

        if changed:
            clouds = [np.zeros((1, 3))]
            if target:
                target_xyz = np.asarray(target)
                target_line.set_data_3d(
                    target_xyz[:, 0], target_xyz[:, 1], target_xyz[:, 2]
                )
                clouds.append(target_xyz)
            if history:
                xyz = np.asarray(history)
                line.set_data_3d(xyz[:, 0], xyz[:, 1], xyz[:, 2])
                tip.set_data_3d(xyz[-1:, 0], xyz[-1:, 1], xyz[-1:, 2])
                clouds.append(xyz)
            equal_limits(ax, np.vstack(clouds))
            fig.canvas.draw_idle()
        plt.pause(0.03)

    plt.close("all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
