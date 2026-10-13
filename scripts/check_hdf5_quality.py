#!/usr/bin/env python3
"""Read-only Galaxis HDF5 signal audit; usage: python3 scripts/check_hdf5_quality.py FILE.h5."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

try:
    import h5py
    import numpy as np
except ImportError as exc:
    raise SystemExit("缺少依赖，请在运行脚本的 Python 环境执行：python3 -m pip install numpy h5py") from exc


VERSION = 1
ANOMALY_COLUMNS = [
    "signal", "order", "metric", "sample_start_index", "sample_end_index",
    "relative_time_s", "value", "z_score",
]


def z_score(values):
    """Population Z-score within one file/channel/order; no reference dataset."""
    values = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(values)
    result = np.full(values.shape, np.nan)
    selected = values[finite]
    if not selected.size:
        return result, {"count": 0, "mean": None, "std": None, "constant": False}
    # Rescale before computing mean/std to avoid overflow on large finite inputs.
    scale = float(np.max(np.abs(selected))) or 1.0
    scaled = selected / scale
    mean_scaled = float(np.mean(scaled))
    std_scaled = float(np.std(scaled, ddof=0))
    constant = bool(np.all(selected == selected[0]))
    if constant or std_scaled == 0:
        result[finite] = 0.0
    else:
        result[finite] = (scaled - mean_scaled) / std_scaled
    return result, {
        "count": int(selected.size), "mean": mean_scaled * scale,
        "std": std_scaled * scale, "constant": constant,
    }


def describe(values, threshold):
    z, basic = z_score(values)
    selected = values[np.isfinite(values)]
    flagged = np.isfinite(z) & (np.abs(z) > threshold)
    basic.update({
        "min": float(np.min(selected)) if selected.size else None,
        "max": float(np.max(selected)) if selected.size else None,
        "max_abs_z": float(np.max(np.abs(z[np.isfinite(z)]))) if selected.size else None,
        "flagged_count": int(flagged.sum()),
        "flagged_fraction": float(flagged.sum() / selected.size) if selected.size else None,
    })
    return z, basic


def validate_timestamps(timestamps):
    timestamps = np.asarray(timestamps)
    if timestamps.ndim != 1 or timestamps.dtype.kind not in "iu":
        raise ValueError("timestamps_ns 必须是一维整数纳秒数组")
    if timestamps.dtype.kind == "u" and timestamps.size and int(timestamps.max()) > np.iinfo(np.int64).max:
        raise ValueError("timestamps_ns 超出 int64 范围")
    return timestamps.astype(np.int64, copy=False)


def timing(timestamps, gap_factor):
    timestamps = validate_timestamps(timestamps)
    if timestamps.size and int(timestamps.max()) - int(timestamps.min()) > np.iinfo(np.int64).max:
        raise ValueError("时间戳跨度超出 int64 范围")
    relative = ((timestamps - timestamps[0]).astype(np.float64) / 1e9
                if timestamps.size else np.array([], dtype=np.float64))
    dt = np.diff(timestamps).astype(np.float64) / 1e9
    positive = dt[dt > 0]
    median = float(np.median(positive)) if positive.size else None
    gap = dt > median * gap_factor if median is not None else np.zeros(dt.shape, dtype=bool)
    return relative, (dt > 0) & ~gap, {
        "sample_count": int(timestamps.size),
        "first_timestamp_ns": int(timestamps[0]) if timestamps.size else None,
        "last_timestamp_ns": int(timestamps[-1]) if timestamps.size else None,
        "duration_s": float(relative[-1]) if relative.size else None,
        "duplicate_intervals": int((dt == 0).sum()),
        "backward_intervals": int((dt < 0).sum()),
        "gap_intervals": int(gap.sum()),
        "median_dt_ms": median * 1000 if median is not None else None,
        "median_rate_hz": 1 / median if median else None,
        "max_dt_ms": float(np.max(positive) * 1000) if positive.size else None,
        "gap_rule": f"dt > {gap_factor:g} × median(positive dt)",
        # Full timing arrays are in series.npz; bound the JSON/HTML preview.
        "bad_interval_end_indices_preview": (np.flatnonzero((dt <= 0) | gap)[:100] + 1).tolist(),
    }


def analyze_signal(timestamps, values, max_order=3, threshold=3.0, gap_factor=5.0):
    timestamps = validate_timestamps(timestamps)
    values = np.asarray(values)
    if values.ndim != 1 or values.dtype.kind not in "biuf":
        raise ValueError("values 必须是一维实数数组")
    if timestamps.size != values.size:
        raise ValueError("timestamps_ns 与 values 长度不一致")
    values = values.astype(np.float64)
    times, intervals_ok, clock = timing(timestamps, gap_factor)
    levels = []
    raw = values.copy()
    derivative = values.copy()
    valid = np.isfinite(values)
    derived_overflow = 0
    for order in range(max_order + 1):
        if order:
            # Never sort, interpolate, bridge bad samples, or differentiate over a gap.
            valid = valid[:-1] & valid[1:]
            if order == 1:
                valid &= intervals_ok
            delta_time = np.diff(times)
            valid &= np.isfinite(delta_time) & (delta_time > 0)
            with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
                raw = np.diff(raw)
                derivative = np.divide(np.diff(derivative), delta_time,
                                       out=np.full(delta_time.shape, np.nan), where=valid)
            derived_overflow += int((valid & (~np.isfinite(raw) | ~np.isfinite(derivative))).sum())
            valid &= np.isfinite(raw) & np.isfinite(derivative)
            times = (times[:-1] + times[1:]) / 2
        raw = np.where(valid, raw, np.nan)
        derivative = np.where(valid, derivative, np.nan)
        raw_z, raw_stats = describe(raw, threshold)
        derivative_z, derivative_stats = describe(derivative, threshold)
        levels.append({
            "order": order, "time_s": times.copy(), "raw_difference": raw,
            "raw_z": raw_z, "time_derivative": derivative, "derivative_z": derivative_z,
            "stats": {"raw_difference": raw_stats, "time_derivative": derivative_stats},
        })
    errors = []
    warnings = []
    if not values.size:
        errors.append("空信号")
    invalid_count = int((~np.isfinite(values)).sum())
    if invalid_count:
        errors.append(f"存在 {invalid_count} 个 NaN/Inf")
    if clock["duplicate_intervals"] or clock["backward_intervals"]:
        errors.append("时间戳存在重复或倒序；相关差分已排除")
    if derived_overflow:
        errors.append(f"差分计算出现 {derived_overflow} 个非有限结果；已排除")
    if clock["gap_intervals"]:
        warnings.append(f"存在 {clock['gap_intervals']} 个长间隔；未跨间隔求差分")
    if any(level["stats"]["raw_difference"]["count"] < 2 for level in levels):
        warnings.append("部分阶次少于 2 个有效样本，无法进行有意义的统计判断")
    flagged = any(s["flagged_count"] for level in levels for s in level["stats"].values())
    if flagged:
        warnings.append(f"存在 |Z| > {threshold:g} 的样本，需结合实际动作复核")
    return {
        "timestamps_ns": timestamps, "values_original": values, "levels": levels,
        "summary": {"timing": clock, "nonfinite_values": invalid_count,
                    "errors": errors, "warnings": warnings,
                    "constant": levels[0]["stats"]["raw_difference"]["constant"],
                    "orders": [{"order": level["order"], **level["stats"]} for level in levels]},
    }


def sample_plot(times, values, z, limit=900, max_gap=None):
    """Min/max bins preserve spikes; CSV/NPZ always keep full-resolution results."""
    n = len(values)
    if n <= limit:
        chosen = np.arange(n)
    else:
        chosen = {0, n - 1}
        for chunk in np.array_split(np.arange(n), max(1, limit // 4)):
            good = chunk[np.isfinite(values[chunk])]
            if good.size:
                chosen.update((int(good[np.argmin(values[good])]), int(good[np.argmax(values[good])])))
                chosen.add(int(good[np.argmax(np.abs(z[good]))]))
            bad = chunk[~np.isfinite(values[chunk])]
            if bad.size:
                chosen.add(int(bad[0]))
        chosen = np.array(sorted(chosen))
    # Keep break semantics even when a NaN/gap is omitted by plot downsampling.
    blocked = ~np.isfinite(values) | ~np.isfinite(times)
    dt = np.diff(times)
    blocked[1:] |= (dt <= 0) | ~np.isfinite(dt)
    if max_gap is not None:
        blocked[1:] |= dt > max_gap
    prefix = np.cumsum(blocked)
    result = []
    previous = None
    for i in chosen:
        must_break = previous is None or bool(prefix[i] - prefix[previous])
        result.append([float(times[i]) if np.isfinite(times[i]) else None,
                       float(values[i]) if np.isfinite(values[i]) else None,
                       float(z[i]) if np.isfinite(z[i]) else None, must_break])
        previous = i
    return result


def save_array(archive, name, values):
    with archive.open(name + ".npy", "w", force_zip64=True) as stream:
        np.lib.format.write_array(stream, np.asarray(values), allow_pickle=False)


def quaternion_checks(handle, norm_tolerance):
    result = []
    groups = {str(group.attrs.get("key", name)): group for name, group in handle["signals"].items()
              if isinstance(group, h5py.Group)}
    for part in ("head", "left_wrist", "right_wrist"):
        keys = [f"{part}.orientation.{axis}" for axis in "xyzw"]
        present = [key in groups for key in keys]
        if not any(present):
            continue
        if not all(present):
            result.append({"part": part, "warning": "四元数分量不完整，无法检查"})
            continue
        try:
            timestamps = [validate_timestamps(groups[key]["timestamps_ns"][:]) for key in keys]
            if not all(np.array_equal(timestamps[0], t) for t in timestamps[1:]):
                raise ValueError("四元数分量的时间戳未对齐，未强行插值")
            q = np.column_stack([groups[key]["values"][:] for key in keys]).astype(float)
            if q.shape != (len(timestamps[0]), 4):
                raise ValueError("四元数分量长度不一致")
            with np.errstate(over="ignore", invalid="ignore"):
                norms = np.linalg.norm(q, axis=1)
            good = np.isfinite(norms) & (norms > 0)
            norm_bad = ~good | (np.abs(norms - 1) > norm_tolerance)
            normalized = np.divide(q, norms[:, None], out=np.zeros_like(q), where=good[:, None])
            flips = ((normalized[:-1] * normalized[1:]).sum(axis=1) < 0) & good[:-1] & good[1:]
            result.append({"part": part, "norm_tolerance": norm_tolerance,
                           "norm_outside_tolerance": int(norm_bad.sum()),
                           "possible_sign_flips": int(flips.sum()),
                           "sign_flip_end_indices_preview": (np.flatnonzero(flips)[:100] + 1).tolist(),
                           "note": "q 与 -q 表示同一姿态；负点积只提示可能符号翻转，不等于真实旋转突变。"})
        except (KeyError, ValueError, TypeError) as exc:
            result.append({"part": part, "warning": str(exc)})
    return result


def build_report(path, output, max_order=3, threshold=3.0, gap_factor=5.0, norm_tolerance=0.05):
    path = Path(path).expanduser().resolve(strict=True)
    output = Path(output).expanduser().resolve()
    report = {
        "schema_version": VERSION, "input": str(path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": {"max_order": max_order, "z_threshold": threshold, "ddof": 0,
                     "gap_factor": gap_factor, "quaternion_norm_tolerance": norm_tolerance},
        "method": {
            "difference": "Δ^0 x=x; Δ^k x=diff(Δ^(k-1) x)",
            "time_derivative": "D^0 x=x; D^k x=diff(D^(k-1) x)/diff(t^(k-1)); t^k=(t[:-1]+t[1:])/2",
            "z_score": "(value - mean)/std(ddof=0)，每个信号、阶次、指标独立计算，仅用有限有效值；常量 Z=0",
            "scope": "检查 /signals/* 的全部实数曲线、视频时间轴和校准元信息；不检查视频画面内容或解析原始 CDR。",
            "limitations": [
                "Z-score 是本文件内部统计线索，不是物理阈值或正常数据基准；匀速漂移、整段偏移等可能无法检出。",
                "长序列、多通道和多阶检验容易出现超阈值点，应结合比例和原曲线复核，不能以任意一个超阈值点直接判废。",
                "高阶差分会放大噪声和时间戳抖动；不规则采样下 D^2/D^3 为离散估计。未平滑、补点、排序或修改原值。",
                "四元数逐分量变化率不是物理角速度、角加速度；未自动做符号连续化。",
                "常量信号可能是静止，也可能是卡死，仅靠本文件无法判定。",
                "Z-score 对二值按键、追踪状态等离散信号只供参考。",
                "时间轴主要是录制侧时间，不能据此推断硬件到电脑的完整传输延迟。",
            ],
        },
        "file_errors": [], "signals": [], "videos": [], "calibration": {}, "datasets": [],
    }
    plots = []
    with h5py.File(path, "r") as handle:
        if "signals" not in handle or not isinstance(handle["signals"], h5py.Group) or not len(handle["signals"]):
            raise ValueError("该文件没有非空 /signals 数值曲线组；此脚本面向 App 的 Galaxis HDF5，不解析旧版原始 CDR 文件")
        if "complete" in handle.attrs and not bool(handle.attrs["complete"]):
            report["file_errors"].append("HDF5 未标记 complete，可能尚未写完或录制未完成")
        report["signal_profile"] = str(handle.attrs.get("signal_profile", "all / 未标记"))
        def inventory(name, obj):
            if isinstance(obj, h5py.Dataset):
                report["datasets"].append({"path": "/" + name, "shape": list(obj.shape), "dtype": str(obj.dtype)})
        handle.visititems(inventory)
        output.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(output / "series.npz", "w", compression=zipfile.ZIP_DEFLATED) as archive, \
                (output / "anomalies.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            csv_writer = csv.writer(stream)
            csv_writer.writerow(ANOMALY_COLUMNS)
            seen = set()
            for index, (name, group) in enumerate(handle["signals"].items()):
                key = str(group.attrs.get("key", name))
                prefix = f"signal_{index:04d}"
                summary = {"key": key, "hdf5_path": group.name, "npz_prefix": prefix}
                try:
                    if key in seen:
                        raise ValueError("重复的 signal key")
                    seen.add(key)
                    data = analyze_signal(group["timestamps_ns"][:], group["values"][:],
                                          max_order, threshold, gap_factor)
                    summary.update(data["summary"])
                    save_array(archive, prefix + "/timestamps_ns", data["timestamps_ns"])
                    save_array(archive, prefix + "/values_original", data["values_original"])
                    plot = {"key": key, "levels": []}
                    for level in data["levels"]:
                        order = level["order"]
                        median_ms = summary["timing"]["median_dt_ms"]
                        plot_gap = median_ms / 1000 * gap_factor if order == 0 and median_ms else None
                        for array_name in ("time_s", "raw_difference", "raw_z", "time_derivative", "derivative_z"):
                            save_array(archive, f"{prefix}/order_{order}/{array_name}", level[array_name])
                        plot["levels"].append({"order": order,
                            "raw": sample_plot(level["time_s"], level["raw_difference"], level["raw_z"], max_gap=plot_gap),
                            "derivative": sample_plot(level["time_s"], level["time_derivative"], level["derivative_z"], max_gap=plot_gap)})
                        for metric, z_name in (("raw_difference", "raw_z"), ("time_derivative", "derivative_z")):
                            if order == 0 and metric == "time_derivative":
                                continue  # order 0 is the same raw signal
                            z = level[z_name]
                            for i in np.flatnonzero(np.isfinite(z) & (np.abs(z) > threshold)):
                                csv_key = "'" + key if key.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else key
                                csv_writer.writerow([csv_key, order, metric, int(i), int(i + order),
                                                     level["time_s"][i], level[metric][i], z[i]])
                    plots.append(plot)
                except (KeyError, ValueError, TypeError, OverflowError) as exc:
                    summary.update(errors=[str(exc)], warnings=[], orders=[])
                summary["status"] = ("结构错误" if summary.get("errors") else
                                     "需要复核" if summary.get("warnings") else "未发现规则触发")
                report["signals"].append(summary)
            for name, group in handle.get("videos", {}).items():
                video = {"camera": name}
                try:
                    ts = validate_timestamps(group["timestamps_ns"][:])
                    _, _, stats = timing(ts, gap_factor)
                    frames = group["frame_indices"][:]
                    video.update(stats)
                    if frames.ndim != 1 or frames.dtype.kind not in "iu" or len(frames) != len(ts):
                        raise ValueError("视频帧编号和时间戳形状/类型不匹配")
                    video["frame_index_discontinuities"] = int((np.diff(frames.astype(np.int64)) != 1).sum())
                    video["frame_count_attribute_matches"] = int(group.attrs.get("frame_count", len(ts))) == len(ts)
                    video["note"] = "只检查已保存时间轴；连续帧编号并不能证明相机源头没有丢帧。"
                    save_array(archive, f"video_{name}/timestamps_ns", ts)
                    save_array(archive, f"video_{name}/frame_indices", frames)
                except (KeyError, ValueError, TypeError) as exc:
                    video["error"] = str(exc)
                report["videos"].append(video)
        for name, value in handle.get("calibration", {}).attrs.items() if "calibration" in handle else []:
            try:
                value = float(value)
                if not np.isfinite(value):
                    raise ValueError("非有限值")
                report["calibration"][name] = value
            except (TypeError, ValueError):
                report["file_errors"].append(f"校准元信息 {name} 不是有效数值")
        report["quaternions"] = quaternion_checks(handle, norm_tolerance)
    video_findings = any(v.get("error") or v.get("duplicate_intervals") or v.get("backward_intervals")
                         or v.get("gap_intervals") or v.get("frame_index_discontinuities")
                         or v.get("frame_count_attribute_matches") is False or v.get("sample_count") == 0
                         for v in report["videos"])
    quaternion_findings = any(q.get("warning") or q.get("norm_outside_tolerance") or q.get("possible_sign_flips")
                              for q in report["quaternions"])
    report["summary"] = {
        "signal_count": len(report["signals"]),
        "error_signals": sum(bool(s.get("errors")) for s in report["signals"]),
        "review_signals": sum(s["status"] == "需要复核" for s in report["signals"]),
        "unflagged_signals": sum(s["status"] == "未发现规则触发" for s in report["signals"]),
        "video_findings": video_findings, "quaternion_findings": quaternion_findings,
    }
    report["has_findings"] = bool(report["file_errors"] or report["summary"]["error_signals"]
                                   or report["summary"]["review_signals"] or video_findings or quaternion_findings)
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "report.html").write_text(render_html(report, plots), encoding="utf-8")
    return report


def render_html(report, plots):
    def safe_json(obj):
        return json.dumps(obj, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    rows = []
    for signal in report["signals"]:
        orders = signal.get("orders", [])
        cells = []
        for order in orders:
            raw, derivative = order["raw_difference"], order["time_derivative"]
            def number(value):
                return "—" if value is None else f"{value:.3g}"
            def percent(value):
                return "—" if value is None else f"{100 * value:.2f}%"
            cells.append(f"<td>{number(raw['max_abs_z'])} / {raw['flagged_count']} ({percent(raw['flagged_fraction'])})<br>"
                         f"{number(derivative['max_abs_z'])} / {derivative['flagged_count']} ({percent(derivative['flagged_fraction'])})</td>")
        cells.extend("<td>—</td>" for _ in range(report["settings"]["max_order"] + 1 - len(cells)))
        notes = signal.get("errors", []) + signal.get("warnings", [])
        if signal.get("constant"):
            notes.append("原值为常量；需结合任务判断静止或卡死")
        rows.append(f"<tr><td>{html.escape(signal['key'])}</td><td>{signal['status']}</td>"
                    + "".join(cells) + f"<td>{html.escape('；'.join(notes))}</td></tr>")
    headers = "".join(f"<th>{order} 阶<br>差分 / 变化率</th>" for order in range(report["settings"]["max_order"] + 1))
    return HTML_TEMPLATE.replace("__TITLE__", html.escape(Path(report["input"]).name)).replace(
        "__ROWS__", "".join(rows)).replace("__HEADERS__", headers).replace(
        "__REPORT__", safe_json(report)).replace("__PLOTS__", safe_json(plots))


HTML_TEMPLATE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>HDF5 检查 · __TITLE__</title>
<style>
*{box-sizing:border-box}body{font:15px/1.65 system-ui,sans-serif;background:#f4f6fa;color:#182338;margin:0}
main{max-width:1400px;margin:auto;padding:28px}h1{font-size:25px;margin:0}h2{font-size:19px}
.card{background:white;border:1px solid #dce2ea;border-radius:12px;padding:20px;margin:18px 0}
.muted{color:#526078}select{font:inherit;padding:7px;max-width:100%;border:1px solid #aab7ca;border-radius:6px}
label{display:inline-block;margin:0 16px 10px 0}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;border-bottom:1px solid #e2e7ef;padding:9px;vertical-align:top}th{white-space:nowrap;background:#f3f6fa}
td:first-child{overflow-wrap:anywhere;min-width:220px}canvas{display:block;width:100%;height:260px}.notice{border-left:4px solid #d89510}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#245bb4}summary{cursor:pointer}
</style><main><h1>HDF5 数据检查</h1><div class="muted">__TITLE__</div>
<section class="card notice"><div id="overview"></div><p>“需要复核”不等于数据错误，“未发现规则触发”也不代表数据一定正常。默认阈值 |Z| &gt; 3，只比较本文件内部数据。</p>
<a href="summary.json">统计明细 JSON</a> · <a href="anomalies.csv">超阈值明细 CSV</a> · <a href="series.npz">全部差分与 Z-score 数组 NPZ</a>
<!-- STATIC_PLOTS --></section>
<section class="card"><h2>曲线查看</h2><label>信号 <select id="signal"></select></label>
<label>阶次 <select id="order"></select></label><label>指标 <select id="metric"><option value="derivative">按时间归一化的变化率</option><option value="raw">普通差分</option></select></label>
<p id="plotLabel" class="muted"></p><canvas id="curve"></canvas><canvas id="zchart"></canvas>
<div class="muted">横轴：相对本信号首条记录的秒数；高阶结果定位于递归中点时间。蓝线为数值，红点为超阈值样本，Z 图虚线为阈值。图表按极值分桶抽样，仅作浏览；完整计算结果在 NPZ，全部超阈值点在 CSV。断档两侧不连线。</div></section>
<section class="card"><h2>逐信号结果</h2><p class="muted">每阶单元格：第一行普通差分，第二行时间变化率；格式为“最大 |Z| / 超阈值样本数（占有效样本比例）”。0 阶为原值。长序列出现少量极值并不罕见，不应仅凭这一点判废。</p>
<div class="scroll"><table><thead><tr><th>信号</th><th>检查结论</th>__HEADERS__<th>说明</th></tr></thead><tbody>__ROWS__</tbody></table></div></section>
<section class="card"><h2>时间轴、四元数与校准信息</h2><div id="extras"></div></section>
<section class="card"><h2>方法与限制</h2><pre id="method"></pre></section>
<script id="report-data" type="application/json">__REPORT__</script><script id="plot-data" type="application/json">__PLOTS__</script>
<script>
const R=JSON.parse(document.getElementById('report-data').textContent),P=JSON.parse(document.getElementById('plot-data').textContent);
const $=id=>document.getElementById(id),T=R.settings.z_threshold;
$('overview').textContent=`${R.summary.signal_count} 路信号：${R.summary.error_signals} 路结构错误，${R.summary.review_signals} 路需要复核，${R.summary.unflagged_signals} 路未发现规则触发。`;
$('method').textContent=JSON.stringify(R.method,null,2);
for(const [name,data] of [['文件问题',R.file_errors],['四元数',R.quaternions],['视频时间轴',R.videos],['校准常量',R.calibration],['各信号采样时间',R.signals.map(s=>({key:s.key,...s.timing}))],['HDF5 数据集清单',R.datasets]]){
 const d=document.createElement('details'),s=document.createElement('summary'),p=document.createElement('pre');s.textContent=name;p.textContent=JSON.stringify(data,null,2);d.append(s,p);$('extras').append(d);
}
P.forEach((p,i)=>{const o=new Option(p.key,i);$('signal').add(o)});
for(let i=0;i<=R.settings.max_order;i++)$('order').add(new Option(i===0?'0 阶：原值':`${i} 阶`,i));
function draw(id,points,zMode){
 const c=$(id),dpr=window.devicePixelRatio||1,w=c.clientWidth,h=260;c.width=w*dpr;c.height=h*dpr;
 const g=c.getContext('2d');g.scale(dpr,dpr);g.clearRect(0,0,w,h);
 const rows=points.filter(p=>Number.isFinite(p[0])&&Number.isFinite(p[zMode?2:1]));
 if(!rows.length){g.fillText('没有足够的有效样本',55,50);return;}
 let xmin=Math.min(...rows.map(p=>p[0])),xmax=Math.max(...rows.map(p=>p[0]));if(xmax===xmin)xmax=xmin+1;
 let ymin=Math.min(...rows.map(p=>p[zMode?2:1])),ymax=Math.max(...rows.map(p=>p[zMode?2:1]));
 if(zMode){ymin=Math.min(ymin,-T);ymax=Math.max(ymax,T)}
 if(ymin===ymax){ymin-=1;ymax+=1}const pad=(ymax-ymin)*.08;ymin-=pad;ymax+=pad;
 const X=x=>62+(x-xmin)/(xmax-xmin)*(w-84),Y=y=>220-(y-ymin)/(ymax-ymin)*190;
 g.font='12px system-ui';g.fillStyle='#526078';g.strokeStyle='#dce2ea';
 for(let i=0;i<5;i++){const v=ymin+(ymax-ymin)*i/4,y=Y(v);g.beginPath();g.moveTo(62,y);g.lineTo(w-22,y);g.stroke();g.fillText(v.toPrecision(3),2,y+4)}
 g.fillText(xmin.toFixed(3)+' s',62,244);g.fillText(xmax.toFixed(3)+' s',Math.max(62,w-105),244);
 if(zMode){g.setLineDash([5,4]);g.strokeStyle='#d68a19';for(const t of [-T,T]){g.beginPath();g.moveTo(62,Y(t));g.lineTo(w-22,Y(t));g.stroke()}g.setLineDash([])}
 g.strokeStyle='#2868c8';g.lineWidth=1.4;g.beginPath();let prev=null;
 for(const p of points){const y=p[zMode?2:1];if(!Number.isFinite(p[0])||!Number.isFinite(y)){prev=null;continue}
 if(!prev||p[3]){g.moveTo(X(p[0]),Y(y))}else g.lineTo(X(p[0]),Y(y));prev=p;}g.stroke();
 g.fillStyle='#cd3545';for(const p of rows)if(Math.abs(p[2])>T){g.beginPath();g.arc(X(p[0]),Y(p[zMode?2:1]),2.5,0,Math.PI*2);g.fill()}
 g.fillStyle='#182338';g.fillText(zMode?'Z-score':'数值',62,16);
}
function refresh(){const p=P[Number($('signal').value)];if(!p)return;const order=Number($('order').value),metric=$('metric').value;
 const points=p.levels[order][metric];
 $('plotLabel').textContent=`${p.key} · ${order} 阶 · ${metric==='raw'?'普通差分（原单位）':'时间变化率（原单位 / 秒^'+order+'）'}；四元数分量的变化率不是物理角速度。`;
 draw('curve',points,false);draw('zchart',points,true);
}
['signal','order','metric'].forEach(id=>$(id).addEventListener('change',refresh));window.addEventListener('resize',refresh);refresh();
</script></main></html>'''


def positive_float(text):
    value = float(text)
    if not np.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的有限数")
    return value


def generate_static_plots(output, metric="difference"):
    """Use the active Python, or the existing system Python with plotting deps.

    The plotter consumes NPZ/JSON only, so it does not need h5py. Do not install
    packages or alter either Python environment just to generate the pictures.
    """
    output = Path(output).resolve()
    plotter = Path(__file__).resolve().with_name("plot_hdf5_quality.py")
    if not plotter.is_file():
        raise RuntimeError(f"缺少绘图脚本：{plotter}")
    candidates = list(dict.fromkeys([sys.executable, "/usr/bin/python3"]))
    with tempfile.TemporaryDirectory(prefix="hdf5-plot-cache-") as cache:
        env = dict(os.environ, MPLCONFIGDIR=cache)
        selected = None
        for candidate in candidates:
            if not Path(candidate).is_file():
                continue
            try:
                probe = subprocess.run(
                    [candidate, "-c", "import numpy, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot"],
                    env=env, capture_output=True, text=True, timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                selected = candidate
                break
        if selected is None:
            raise RuntimeError("没有可用的绘图环境。请执行 python3 -m pip install numpy matplotlib 后重试；"
                               "也可添加 --no-plots 仅生成交互式报告和计算结果。")
        process = subprocess.run(
            [selected, str(plotter), str(output), "--output-dir", str(output / "curves"),
             "--metric", metric], env=env, capture_output=True, text=True,
        )
        if process.returncode:
            raise RuntimeError((process.stderr or process.stdout or "绘图进程失败")[-4000:].strip())
    count = len(list((output / "curves").glob("*.png")))
    if not (output / "curves" / "index.html").is_file() or not count:
        raise RuntimeError("绘图进程未生成完整的曲线图集")
    return {"status": "complete", "metric": metric, "image_count": count,
            "index": "curves/index.html"}


def attach_static_plots(report, output, info):
    report["static_plots"] = info
    output = Path(output)
    (output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    page = output / "report.html"
    link = f'<p><a href="curves/index.html">查看自动生成的全部曲线图片（{info["image_count"]} 张）</a></p>'
    page.write_text(page.read_text(encoding="utf-8").replace("<!-- STATIC_PLOTS -->", link), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hdf5", type=Path, help="要检查的 HDF5 文件；仅以只读方式打开")
    parser.add_argument("--output-dir", type=Path, help="新报告目录；已存在则拒绝覆盖")
    parser.add_argument("--max-order", type=int, choices=range(4), default=3, help="最高差分阶数，默认 3")
    parser.add_argument("--z-threshold", type=positive_float, default=3.0, help="标记 |Z| 大于该值，默认 3")
    parser.add_argument("--gap-factor", type=positive_float, default=5.0, help="长间隔相对中位间隔的倍数，须大于 1，默认 5")
    parser.add_argument("--quaternion-norm-tolerance", type=positive_float, default=0.05)
    parser.add_argument("--no-plots", action="store_true", help="不生成独立 PNG 图片；默认自动生成")
    parser.add_argument("--plot-metric", choices=("difference", "derivative"), default="difference",
                        help="自动图片绘制普通差分或时间变化率；默认 difference。交互式报告始终包含两者")
    parser.add_argument("--fail-on-findings", action="store_true", help="发现需复核或结构问题时返回退出码 2；默认成功生成报告即返回 0")
    args = parser.parse_args(argv)
    if args.gap_factor <= 1:
        parser.error("--gap-factor 必须大于 1")
    output = args.output_dir or (Path.cwd() / "hdf5-reports" /
             (args.hdf5.stem + "_" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
    try:
        report = build_report(args.hdf5, output, args.max_order, args.z_threshold,
                              args.gap_factor, args.quaternion_norm_tolerance)
    except (OSError, ValueError, TypeError) as exc:
        print(f"检查失败：{exc}", file=sys.stderr)
        return 1
    summary = report["summary"]
    print(f"检查完成：{summary['signal_count']} 路信号；结构错误 {summary['error_signals']} 路；需复核 {summary['review_signals']} 路。")
    print("结论：" + ("有规则触发，请查看报告；不等于已确认数据异常。" if report["has_findings"] else "未发现规则触发；不代表已证明数据正常。"))
    print(f"HTML 报告：{output.resolve() / 'report.html'}")
    print("同时保存 summary.json、anomalies.csv、series.npz。原 HDF5 未修改。")
    if not args.no_plots:
        print("正在自动生成曲线图片和图集…", flush=True)
        try:
            info = generate_static_plots(output, args.plot_metric)
            attach_static_plots(report, output, info)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"检测报告已保存，但曲线图片生成失败：{exc}", file=sys.stderr)
            print("可在修复绘图依赖后，用 scripts/plot_hdf5_quality.py 对上述报告目录补画。", file=sys.stderr)
            return 1
        print(f"已自动生成 {info['image_count']} 张曲线图：{output.resolve() / info['index']}")
    return 2 if args.fail_on_findings and report["has_findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
