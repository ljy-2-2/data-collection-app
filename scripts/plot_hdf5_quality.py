#!/usr/bin/env python3
"""Generate offline PNG plots from check_hdf5_quality.py's summary.json/series.npz."""
from __future__ import annotations

import argparse
from datetime import datetime
import html
import json
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties


COLORS = ["#2765b1", "#d07c18", "#288878", "#8e5aa9"]


def configure_style():
    font = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    if font.exists():
        from matplotlib import font_manager
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = FontProperties(fname=str(font)).get_name()
    plt.rcParams.update({"axes.unicode_minus": False, "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.edgecolor": "#cbd3df", "axes.labelcolor": "#475569",
                         "text.color": "#182338", "xtick.color": "#475569",
                         "ytick.color": "#475569", "figure.facecolor": "white",
                         "savefig.facecolor": "white"})


def friendly(key):
    for part, label in (("head", "头显"), ("left_wrist", "左手柄"), ("right_wrist", "右手柄")):
        if key.startswith(part + ".position."):
            return f"{label}位置 {key.rsplit('.', 1)[-1].upper()}"
        if key.startswith(part + ".orientation."):
            return f"{label}四元数 {key.rsplit('.', 1)[-1].upper()}"
    if key.startswith("galaxis.gripper.") and key.endswith(".position"):
        return "左夹爪开合角度" if ".left." in key else "右夹爪开合角度"
    return key


def split_for_plot(t, values, median_dt_ms, gap_factor):
    """Insert separators without removing real points or drawing across gaps."""
    if not len(t):
        return t, values
    dt = np.diff(t)
    blocked = (dt <= 0) | ~np.isfinite(dt)
    if median_dt_ms:
        blocked |= dt > median_dt_ms / 1000 * gap_factor
    indices = np.flatnonzero(blocked) + 1
    return np.insert(t, indices, np.nan), np.insert(values, indices, np.nan)


def plot_trace(ax, t, values, z, threshold, color, label, summary, settings, z_mode=False):
    y = z if z_mode else values
    x_plot, y_plot = split_for_plot(t, y, summary.get("timing", {}).get("median_dt_ms"), settings["gap_factor"])
    ax.plot(x_plot, y_plot, color=color, linewidth=0.8, label=label)
    flagged = np.isfinite(z) & np.isfinite(y) & (np.abs(z) > threshold)
    if np.any(flagged):
        ax.scatter(t[flagged], y[flagged], s=7, color="#ce3346", alpha=0.7, zorder=3)
    ax.grid(alpha=0.2)
    ax.set_xlabel("相对该信号起点的时间 / s")


def generate(report_dir, output_dir, metric="difference"):
    configure_style()
    report_dir = Path(report_dir)
    report = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))
    settings = report["settings"]
    max_order = settings["max_order"]
    threshold = settings["z_threshold"]
    value_key, z_key = (("raw_difference", "raw_z") if metric == "difference"
                        else ("time_derivative", "derivative_z"))
    metric_label = "普通差分" if metric == "difference" else "时间归一化变化率"
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    signals = [s for s in report["signals"] if s.get("orders")]
    groups = []
    for part, label in (("head", "头显"), ("left_wrist", "左手柄"), ("right_wrist", "右手柄")):
        for kind, name in (("position", "位置 XYZ"), ("orientation", "四元数 XYZW")):
            members = [s for s in signals if s["key"].startswith(f"{part}.{kind}.")]
            if members:
                groups.append((f"{label} · {name}", members))
    grippers = [s for s in signals if s["key"].startswith("galaxis.gripper.") and s["key"].endswith(".position")]
    if grippers:
        groups.append(("左右夹爪 · 开合角度", grippers))
    grouped = {s["npz_prefix"] for _, members in groups for s in members}
    for s in signals:
        if s["npz_prefix"] not in grouped:
            groups.append((friendly(s["key"]), [s]))
    entries = []
    with np.load(report_dir / "series.npz", allow_pickle=False) as arrays:
        def data(s, order):
            prefix = f"{s['npz_prefix']}/order_{order}/"
            return (arrays[prefix + "time_s"], arrays[prefix + value_key], arrays[prefix + z_key])

        # Overview plots, grouped by physical device, with consistent axis colors.
        for order in range(max_order + 1):
            for z_mode in (False, True):
                rows = max(1, (len(groups) + 1) // 2)
                fig, axes = plt.subplots(rows, 2, figsize=(15, 3.25 * rows), squeeze=False)
                label = "原始数据" if order == 0 else f"{order} 阶{metric_label}"
                title = label + (" · Z-score" if z_mode else " · 曲线总览")
                fig.suptitle(title, fontsize=20, y=0.995)
                for ax, (group_name, members) in zip(axes.flat, groups):
                    for i, signal in enumerate(members):
                        t, values, z = data(signal, order)
                        legend = friendly(signal["key"]) if signal in grippers else signal["key"].rsplit(".", 1)[-1].upper()
                        plot_trace(ax, t, values, z, threshold, COLORS[i % len(COLORS)], legend,
                                   signal, settings, z_mode)
                    ax.set_title(group_name, loc="left", fontsize=12)
                    ax.set_ylabel("Z-score" if z_mode else ("原单位" if metric == "difference" or order == 0 else f"原单位 / s^{order}"))
                    if z_mode:
                        ax.axhline(threshold, color="#b45f0c", linestyle="--", linewidth=0.8)
                        ax.axhline(-threshold, color="#b45f0c", linestyle="--", linewidth=0.8)
                    ax.legend(loc="upper right", fontsize=8, ncol=len(members))
                for ax in list(axes.flat)[len(groups):]:
                    ax.axis("off")
                    ax.text(0.02, 0.9, f"红点：|Z| > {threshold:g}\n仅提示复核，不等于数据错误。\n\n四元数分量变化率不是物理角速度。\n各通道从各自起点计时，未作硬件同步。",
                            va="top", fontsize=12, linespacing=1.8)
                fig.tight_layout(rect=(0, 0.02, 1, 0.97))
                name = f"overview_order_{order}" + ("_z.png" if z_mode else ".png")
                fig.savefig(output / name, dpi=140)
                plt.close(fig)
                entries.append((title, name))

        for signal in signals:
            fig, axes = plt.subplots(max_order + 1, 2, figsize=(13, 2.65 * (max_order + 1)), squeeze=False)
            title = friendly(signal["key"])
            fig.suptitle(title, fontsize=19, y=0.995)
            for order in range(max_order + 1):
                t, values, z = data(signal, order)
                left, right = axes[order]
                plot_trace(left, t, values, z, threshold, COLORS[0], title, signal, settings)
                plot_trace(right, t, values, z, threshold, COLORS[0], title, signal, settings, True)
                left.set_title("原始值" if order == 0 else f"{order} 阶{metric_label}", loc="left")
                left.set_ylabel("原单位" if metric == "difference" or order == 0 else f"原单位 / s^{order}")
                right.set_title(f"对应 Z-score · {int(np.sum(np.isfinite(z) & (np.abs(z) > threshold)))} 个超阈值点", loc="left")
                right.set_ylabel("Z-score")
                right.axhline(threshold, color="#b45f0c", linestyle="--", linewidth=0.8)
                right.axhline(-threshold, color="#b45f0c", linestyle="--", linewidth=0.8)
            fig.text(0.02, 0.008, signal["key"] + f"   |   红点：|Z| > {threshold:g}；需结合动作复核。", fontsize=8)
            fig.tight_layout(rect=(0, 0.025, 1, 0.97))
            name = signal["npz_prefix"] + ".png"
            fig.savefig(output / name, dpi=140)
            plt.close(fig)
            entries.append((title + f" · 0～{max_order} 阶与 Z-score", name))
    blocks = "\n".join(f'<section><h2>{html.escape(title)}</h2><a href="{name}"><img loading="lazy" src="{name}" alt="{html.escape(title)}"></a></section>'
                       for title, name in entries)
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>HDF5 曲线图</title><style>body{font:16px/1.6 system-ui,sans-serif;margin:24px auto;padding:0 20px;max-width:1350px;background:#f4f6fa;color:#182338}'
            'section{background:white;padding:16px;margin:24px 0;border-radius:10px}img{width:100%;height:auto}h2{font-size:20px}</style>'
            f'<h1>HDF5 曲线图 · {metric_label}</h1><p>{html.escape(Path(report["input"]).name)}</p>'
            f'<p>{len(signals)} 路信号；红点为统计超阈值点，不代表已确认异常。点击图片查看大图。全部曲线按完整分辨率绘制。</p>'
            + blocks + '</html>')
    (output / "index.html").write_text(page, encoding="utf-8")
    return output / "index.html", len(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_dir", type=Path, help="包含 summary.json 和 series.npz 的检查结果目录")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--metric", choices=("difference", "derivative"), default="difference")
    args = parser.parse_args()
    output = args.output_dir or args.report_dir / ("curves-" + args.metric + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    index, count = generate(args.report_dir, output, args.metric)
    print(f"已生成 {count} 张曲线图：{index.resolve()}")


if __name__ == "__main__":
    main()
