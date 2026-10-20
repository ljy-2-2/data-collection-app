"""Measured base-frame registration, shared by live kinematics and replay.

Only proper rotations are fitted. Mount centres remain at the supplied CAD
positions. Measurements are TCP offsets from EACH arm's own mounting centre,
expressed in the operator's forward/left/up directions, in millimetres.
No motor signs, joint zeros, tool dimensions or geometric scale are fitted.
"""

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import time
import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
SCHEMA = "marvin_measured_mount_rotation_v1"
AXES = ["forward", "left", "up"]
MAX_ERROR_MM = 5.0


def model_fingerprint(arm):
    from .model import MOUNT, ROOT

    digest = hashlib.sha256()
    for path in (arm.path, MOUNT, ROOT / "CommonConfig/ccs_m6_40.MvKDCfg"):
        digest.update(path.read_bytes())
    digest.update(np.asarray(arm.tool, dtype="<f8").tobytes())
    return digest.hexdigest()


def _points(arm, samples):
    local, measured = [], []
    for sample in samples:
        q = np.asarray(sample["joints_deg"], dtype=float)
        p = np.asarray(sample["tcp_from_mount_mm"], dtype=float)
        if not arm.valid_joints(q) or p.shape != (3,) or not np.isfinite(p).all():
            raise ValueError(f"{arm.side}: 无效关节角或实测 XYZ")
        # Recover vendor-base coordinates regardless of current installation.
        pose = arm.fk(q)
        local.append(1000 * (arm.base[:3, :3].T @ (pose[:3, 3] - arm.base[:3, 3])))
        measured.append(p)
    return np.asarray(local), np.asarray(measured)


def fit_arm(arm, definition):
    if definition.get("model_sha256") != model_fingerprint(arm):
        raise ValueError(f"{arm.side}: 标定与当前 URDF/厂家参数/控制点不匹配")
    fit = [s for s in definition["samples"] if s.get("use") == "fit"]
    check = [s for s in definition["samples"] if s.get("use") == "check"]
    if len(fit) < 3 or not check:
        raise ValueError(f"{arm.side}: 至少需要 3 个拟合姿态和 1 个独立检查姿态")
    local, measured = _points(arm, fit)
    held_local, held_measured = _points(arm, check)
    # Origin is independently known (mount centre); three spanning vectors
    # disambiguate reflection. Repeated/near-planar vectors are inadequate.
    for points in (local, measured):
        spread = np.linalg.svd(points / np.sqrt(len(points)), compute_uv=False)
        if spread[-1] < 20 or spread[-1] / spread[0] < 0.05:
            raise ValueError(f"{arm.side}: 测量方向分布不足，请选不同高度和前后位置")
    if any(np.min(np.linalg.norm(local - p, axis=1)) < 30 for p in held_local):
        raise ValueError(f"{arm.side}: 检查姿态需与拟合姿态相距至少 30 mm")
    u, _, vt = np.linalg.svd(local.T @ measured)
    unconstrained = vt.T @ u.T
    if np.linalg.det(unconstrained) < 0:
        raise ValueError(
            f"{arm.side}: 实测点更符合镜像而非安装旋转；"
            "请核查测量轴、关节零位/符号和模型，不能把 X 取反当作安装变换"
        )
    r = unconstrained
    fit_errors = np.linalg.norm(local @ r.T - measured, axis=1)
    check_errors = np.linalg.norm(held_local @ r.T - held_measured, axis=1)
    if max(np.max(fit_errors), np.max(check_errors)) > MAX_ERROR_MM:
        raise ValueError(
            f"{arm.side}: 安装旋转无法解释测量：拟合最大 {max(fit_errors):.2f} mm，"
            f"独立检查最大 {max(check_errors):.2f} mm（上限 {MAX_ERROR_MM:g} mm）；"
            "核查测量、零位与模型，不能强制套用"
        )
    base = arm.base.copy()
    base[:3, :3] = r
    return base, {
        "fit_samples": len(fit),
        "check_samples": len(check),
        "fit_max_error_mm": float(max(fit_errors)),
        "check_max_error_mm": float(max(check_errors)),
        "world_from_base": base.tolist(),
    }


def resolve_calibration(path, arms):
    path = Path(path)
    if not path.is_absolute():
        path = PROJECT / path
    data = json.loads(path.read_text())
    if (
        data.get("schema") != SCHEMA
        or data.get("axes") != AXES
        or data.get("origin") != "each_arm_mount_center"
        or set(data.get("arms", {})) != {"left", "right"}
    ):
        raise ValueError("标定文件必须使用本侧安装中心及前/左/上轴，包含左右两臂")
    bases, details = {}, {}
    for side, arm in arms.items():
        bases[side], details[side] = fit_arm(arm, data["arms"][side])
    return bases, {
        "source": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "schema": SCHEMA,
        "arms": details,
    }


def new_measurements(models):
    return {
        "schema": SCHEMA,
        "axes": AXES,
        "origin": "each_arm_mount_center",
        "instructions": "真实测量本配置所选 TCP（见各臂 control_point），相对本侧安装面中心，毫米；+X双方前方，+Y双方左方，+Z上。不要填旧FK计算值。",
        "arms": {
            s: {"model_sha256": model_fingerprint(a), "control_point": a.control_point_name, "flange_to_tcp": a.tool.tolist(), "samples": []}
            for s, a in models.arms.items()
        },
    }


def main():
    from .configuration import configuration
    from .model import Models

    parser = argparse.ArgumentParser(
        description="实测安装旋转标定：只读采集，不使能或发送运动指令"
    )
    parser.add_argument("action", choices=("init", "capture", "fit"))
    parser.add_argument(
        "--file", type=Path, default=PROJECT / "calibration/measurements.json"
    )
    parser.add_argument("--config", default=PROJECT / "marvin_teleop/config.json")
    parser.add_argument("--side", choices=("left", "right"))
    parser.add_argument("--use", choices=("fit", "check"), default="fit")
    args = parser.parse_args()
    cfg = configuration(args.config)
    cfg.pop("installation_calibration", None)
    models = Models(cfg)
    if args.action == "init":
        args.file.parent.mkdir(parents=True, exist_ok=True)
        with args.file.open("x") as output:
            json.dump(new_measurements(models), output, ensure_ascii=False, indent=2)
        print(
            f"已创建测量文件：{args.file}。每臂至少采集 3 个 fit 和 1 个 check 姿态。"
        )
        return
    if args.action == "fit":
        _, report = resolve_calibration(args.file, models.arms)
        output = args.file.with_suffix(".fit-report.json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(
            f"通过；启动与回放均可加载 --installation-calibration {args.file.resolve()}"
        )
        return
    if args.side is None:
        parser.error("capture 需要 --side left 或 right")
    from .robot import Robot

    data = json.loads(args.file.read_text())
    definition = data["arms"][args.side]
    if definition["model_sha256"] != model_fingerprint(models.arms[args.side]):
        raise ValueError("测量文件的模型/工具已变化，请重新建立文件")
    # SDK connection only; no enable(), send(), prepare() or idle() calls.
    with contextlib.ExitStack() as stack:
        robot = Robot(cfg)
        stack.callback(robot.close)
        robot.wait_ready()
        before = robot.read()[args.side].copy()
        print(f"当前 {args.side} 关节角：{before.tolist()}")
        text = input(
            "保持这一姿态静止，输入实测 TCP 相对本侧安装中心的 X前 Y左 Z上（毫米，空格分隔）："
        )
        measured = np.asarray([float(v) for v in text.split()])
        after = robot.read()[args.side]
        if np.max(np.abs(after - before)) > 0.05:
            raise ValueError("输入期间机械臂位置改变，未保存；请在静止姿态重新采集")
        _points(
            models.arms[args.side],
            [{"joints_deg": before, "tcp_from_mount_mm": measured}],
        )
        definition["samples"].append(
            {
                "use": args.use,
                "joints_deg": before.tolist(),
                "tcp_from_mount_mm": measured.tolist(),
                "time_ns": time.time_ns(),
            }
        )
    args.file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"已保存 {args.side} {args.use} 实测点，未发送运动指令。")


if __name__ == "__main__":
    main()
