import argparse
import contextlib
import ctypes
import ipaddress
import json
import math
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .model import Models, ROOT
from .launcher import PicoLauncher, ensure_service
from .xr_input import SDKInput
from .protocol import VRServer
from .robot import Robot, SimRobot
from .control import Controller, check_start_pose
from .diagnostics import network_hint, operator_hint, probe_feedback
from .mirror import joint_symmetry

CONTROL_REVISION = "mirror_stable_constrained_ik_v11"


def exception_details(error):
    """Keep the triggering failure visible when stop/close also fails."""
    messages, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        messages.append(f"{type(error).__name__}: {error}")
        error = error.__cause__ or error.__context__
    return " -> 后续处理失败: ".join(dict.fromkeys(reversed(messages)))


def configuration(path):
    c = json.loads(Path(path).read_text())
    retired = {"following_error_soft_deg", "max_following_error_deg"} & c.keys()
    if retired:
        raise ValueError(
            "已删除的跟随误差限流参数仍存在："
            + ", ".join(sorted(retired))
            + "；请从当前配置删除"
        )
    from .tool_frame import validate_tool_config

    c["tool_tcp"] = validate_tool_config(c)
    # Reject old empirical reflections rather than silently reverse a physical axis.
    if (
        c.get("operator_translation_signs", [1, 1, 1]) != [1, 1, 1]
        or any(
            values != [1, 1, 1]
            for values in c.get("operator_rotation_signs", {}).values()
        )
        or any(
            value is not None for value in c.get("wrist_axis_correction", {}).values()
        )
    ):
        raise ValueError(
            "旧方向反转配置已停用；请删除 operator_translation_signs、"
            "operator_rotation_signs、wrist_axis_correction，使用同向相对映射"
        )
    ipaddress.IPv4Address(c["robot_ip"])
    if set(c["arm_channels"]) != {"left", "right"} or set(
        c["arm_channels"].values()
    ) != {"A", "B"}:
        raise ValueError("arm_channels must uniquely map left/right to A/B")
    c.setdefault("resume_gap_s", 0.6)
    for key, value in {
        "ik_rotation_weight_m": 0.35,
        "ik_shoulder_motion_weight": 1.0,
        "ik_singular_rotation_weight_m": 0.05,
        "ik_singular_sigma_low": 0.02,
        "ik_singular_sigma_high": 0.08,
        "joint_limit_soft_margin_deg": 30.0,
        "joint_limit_reserve_deg": 3.0,
        "joint_limit_escape_speed_deg_s": 20.0,
    }.items():
        c.setdefault(key, value)
    ranges = {
        "scale": (0.01, 1),
        "resume_gap_s": (0.0, 1.0),
        "rate_hz": (10, 100),
        "vr_timeout_s": (0.05, 0.3),
        "robot_timeout_s": (0.05, 0.3),
        # All Marvin M6 joints are rated at 180 deg/s in PNVA. Controller
        # construction below still checks the loaded limits for both arms.
        "max_joint_speed_deg_s": (0.1, 180),
        "max_joint_accel_deg_s2": (1, 450),
        "ik_rotation_weight_m": (0.05, 1.0),
        "ik_shoulder_motion_weight": (1.0, 10.0),
        "ik_singular_rotation_weight_m": (0.05, 1.0),
        "ik_singular_sigma_low": (0.001, 0.2),
        "ik_singular_sigma_high": (0.001, 0.3),
        "joint_limit_soft_margin_deg": (0.0, 45.0),
        "joint_limit_reserve_deg": (0.0, 10.0),
        "joint_limit_escape_speed_deg_s": (0.0, 30.0),
        "max_hand_step_m": (0.01, 0.2),
        "max_hand_step_rad": (0.01, 1),
        "max_displacement_m": (0.01, 0.5),
        "collision_margin_m": (0.005, 0.1),
    }
    for key, (lo, hi) in ranges.items():
        if (
            type(c[key]) not in (int, float)
            or not math.isfinite(c[key])
            or not lo <= c[key] <= hi
        ):
            raise ValueError(f"{key} must be in [{lo}, {hi}]")
    if c["ik_singular_rotation_weight_m"] > c["ik_rotation_weight_m"]:
        raise ValueError("singular rotation weight must not exceed normal weight")
    if c["ik_singular_sigma_low"] >= c["ik_singular_sigma_high"]:
        raise ValueError("singular sigma low must be below high")
    if c["joint_limit_reserve_deg"] >= c["joint_limit_soft_margin_deg"]:
        raise ValueError("joint limit reserve must be below soft margin")
    for key in ("port", "status_port"):
        if type(c[key]) is not int or not 1024 <= c[key] <= 65535:
            raise ValueError(f"invalid {key}")
    return c


class Status:
    def __init__(self, port):
        self.value = {"state": "STARTING"}
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/", "/status"):
                    self.send_error(404)
                    return
                with outer.lock:
                    body = (
                        Path(__file__).with_name("status.html").read_bytes()
                        if self.path == "/"
                        else json.dumps(
                            outer.value, ensure_ascii=False, allow_nan=False
                        ).encode()
                    )
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    ("text/html" if self.path == "/" else "application/json")
                    + "; charset=utf-8",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def main():
    parser = argparse.ArgumentParser(
        description="Marvin M6 CCS 双臂 PICO 遥操：默认仿真，--live 实机"
    )
    parser.add_argument("--collection-telemetry", action="store_true",
                        help="向本机采集脚本发布只读反馈和 A/B 按键；保留原 B/Y 急停")
    parser.add_argument(
        "--config", default=str(Path(__file__).with_name("config.json"))
    )
    parser.add_argument(
        "--installation-calibration",
        type=Path,
        help="加载实测安装标定；同步用于双臂 FK、雅可比、IK 和碰撞模型",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="连接实机；左右握把独立接管")
    mode.add_argument(
        "--check-robot", action="store_true", help="只读取实机连接/关节状态后退出"
    )
    mode.add_argument(
        "--check-feedback",
        action="store_true",
        help="只读观测双臂反馈停更与故障，默认10秒，可用 --duration 指定",
    )
    mode.add_argument("--doctor", action="store_true", help="离线模型、依赖、端口检查")
    mode.add_argument(
        "--check-gripper",
        action="store_true",
        help="通过天机末端 CAN 只读查询夹爪参数；不使能机械臂或夹爪",
    )
    mode.add_argument(
        "--scan-gripper-ids",
        action="store_true",
        help="通过末端 CAN 只读扫描电机 ID，不使能/改参数",
    )
    parser.add_argument("--scan-id-start", type=int, default=0)
    parser.add_argument("--scan-id-end", type=int, default=255)
    parser.add_argument(
        "--gripper-config", type=Path, help="启用独立夹爪模块的配置文件；默认不连接夹爪"
    )
    parser.add_argument(
        "--no-adb", action="store_true", help="跳过头显自动启动，用于 Wi-Fi 或离线测试"
    )
    parser.add_argument(
        "--duration", type=float, default=0, help="自动退出秒数，0=一直运行"
    )
    parser.add_argument("--verbose", action="store_true", help="输出完整 JSON 调试日志")
    parser.add_argument(
        "--record-xyz",
        action="store_true",
        help="20 Hz 后台记录手柄/末端完整位姿、关节与暂停原因；./replay.sh 离线回放",
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="可选：执行固定弯肘准备动作；日常遥操无需此参数",
    )
    parser.add_argument(
        "--input",
        choices=("sdk", "tcp"),
        help="sdk=参考工程的现有服务；tcp=直接接原版 VR 包",
    )
    parser.add_argument("--viewer", dest="viewer", action="store_true",
                        help="启动双臂与电机夹爪的反馈观察窗口，跳过命令行询问")
    parser.add_argument("--no-viewer", dest="viewer", action="store_false",
                        help="不启动观察窗口，跳过命令行询问")
    parser.set_defaults(viewer=None)
    args = parser.parse_args()
    from .visualization import choose_viewer

    args.viewer = choose_viewer(args.viewer, diagnostic=any((
        args.doctor, args.check_robot, args.check_feedback,
        args.check_gripper, args.scan_gripper_ids,
    )))
    c = configuration(args.config)
    gripper_config = None
    if args.gripper_config:
        from gripper.config import load as load_gripper_config

        gripper_config = load_gripper_config(args.gripper_config, live=args.live)
    if (args.check_gripper or args.scan_gripper_ids) and (
        not gripper_config or gripper_config["transport"] != "marvin_can"
    ):
        parser.error(
            "gripper checks require --gripper-config with transport=marvin_can"
        )
    if not 0 <= args.scan_id_start <= args.scan_id_end <= 2047:
        parser.error("scan ID range must be within 0..2047")
    print(
        f"控制版本：{CONTROL_REVISION}\n"
        f"实际配置：{Path(args.config).resolve()}\n"
        "跟随策略：100 Hz 直接目标流；实机跟不上不断开、不下使能、不等待反馈追上",
        flush=True,
    )
    if args.installation_calibration:
        c["installation_calibration"] = str(args.installation_calibration.resolve())
    if args.input:
        c["input"] = args.input
    models = Models(c)
    report = models.validate()
    print("双臂 FK/IK 与 URDF 校验通过:", json.dumps(report), flush=True)
    print(
        "实机安装坐标：",
        models.mapping_report["active_mount_source"],
        "；头显首次同向校准，左右独立相对控制",
        flush=True,
    )
    if models.mapping_report["installation_calibration"]:
        print(
            "已加载实测安装标定：",
            json.dumps(
                models.mapping_report["installation_calibration"], ensure_ascii=False
            ),
            flush=True,
        )
    print(
        "当前控制点：",
        json.dumps(models.mapping_report["control_points"], ensure_ascii=False),
        flush=True,
    )
    if args.verbose or args.doctor:
        print("同向映射与安装校验:", json.dumps(models.mapping_report), flush=True)
    if args.doctor:
        lib = ctypes.CDLL(str(ROOT / "SDK_PYTHON/libMarvinSDK.so"))
        for symbol in (
            "OnLinkTo",
            "OnGetBuf",
            "OnSetSend",
            "OnSetTargetState_A",
            "OnSetTargetState_B",
        ):
            getattr(lib, symbol)
        print("SDK 动态库加载正常；离线检查不连接机器人。")
        if c["input"] == "sdk":
            import importlib

            importlib.import_module("xrobotoolkit_sdk")

            print("xrobotoolkit_sdk 可加载；63901/60061 为现有服务端口，可复用。")
        for port in (
            [c["port"], c["status_port"]] if c["input"] == "tcp" else [c["status_port"]]
        ):
            with socket.socket() as probe:
                try:
                    probe.bind((c["host"] if port == c["port"] else "127.0.0.1", port))
                    print(f"端口 {port} 可用")
                except OSError as e:
                    print(f"端口 {port} 不可用: {e}")
                    return 1
        return 0
    if args.live and c.get("site_verified") is not True:
        raise ValueError(
            f"实机首次使用：核对 IP、A/B 接线、安装方向、急停和离线轴向后，将 {Path(args.config).resolve()} 的 site_verified 设为 true。可先运行 ./start.sh --check-robot 只读检查连接"
        )
    if (
        args.live
        or args.check_robot
        or args.check_feedback
        or args.check_gripper
        or args.scan_gripper_ids
    ):
        print(network_hint(c["robot_ip"]), flush=True)
    else:
        print(
            "【仿真模式】不会控制真实机械臂。要控制实机，请退出后运行 ./start-live.sh。",
            flush=True,
        )
    with contextlib.ExitStack() as stack:
        robot = (
            Robot(c)
            if args.live
            or args.check_robot
            or args.check_feedback
            or args.check_gripper
            or args.scan_gripper_ids
            else SimRobot()
        )
        if args.prepare and not (
            args.live
            or args.check_robot
            or args.check_feedback
            or args.check_gripper
            or args.scan_gripper_ids
        ):
            # Demonstrate the measured zero-pose startup problem in simulation.
            robot.q = {s: q * 0 for s, q in robot.q.items()}
        stack.callback(robot.close)
        if args.scan_gripper_ids:
            from gripper.marvin_can import scan_ids

            robot.wait_ready()
            report = scan_ids(
                robot,
                gripper_config,
                args.scan_id_start,
                args.scan_id_end,
                progress=lambda message: print(message, flush=True),
            )
            print(
                "只读 ID 扫描结果（未使能、未改参数）：",
                json.dumps(report, ensure_ascii=False, indent=2),
                flush=True,
            )
            return (
                0 if all(r["completed"] and r["found"] for r in report.values()) else 1
            )
        if args.check_gripper:
            from gripper.marvin_can import inspect

            robot.wait_ready()
            gripper_report = inspect(robot, gripper_config)
            print(
                "夹爪只读参数（未使能、未移动）：",
                json.dumps(gripper_report, ensure_ascii=False, indent=2),
                flush=True,
            )
            return 0 if all(item["ok"] for item in gripper_report.values()) else 1
        if args.check_feedback:
            report = probe_feedback(robot, args.duration if args.duration > 0 else 10)
            print(
                "只读反馈观测（未使能、未清错、未移动）:",
                json.dumps(report, ensure_ascii=False),
                flush=True,
            )
            return 0 if report["passed"] else 1
        if args.live or args.check_robot:
            try:
                robot.wait_ready()
            except RuntimeError as error:
                raise RuntimeError(
                    f"机器人启动预检未通过：{error}；{network_hint(c['robot_ip'])}"
                ) from error
        if args.check_robot:
            deadline = time.monotonic() + 3
            good = 0
            while time.monotonic() < deadline:
                try:
                    q = robot.read()
                    good += 1
                    if good >= 10:
                        print(
                            "机器人双臂反馈正常（未发送控制命令）:",
                            {s: v.tolist() for s, v in q.items()},
                        )
                        try:
                            check_start_pose(
                                models, q, c["collision_margin_m"], allow_singular=True
                            )
                            print(
                                "当前起始姿态：模型检查通过；接管时还会重新检查静止状态和实时反馈。",
                                flush=True,
                            )
                        except ValueError as error:
                            print(f"当前起始姿态不能接管：{error}", flush=True)
                        if c.get("site_verified") is not True:
                            print(
                                f"现场核对标记尚未设置：{Path(args.config).resolve()} 中 site_verified=false。",
                                flush=True,
                            )
                        if args.prepare:
                            from .preparation import PreparationPlan

                            for side in ("left", "right"):
                                try:
                                    plan = PreparationPlan(models, q, c, sides={side})
                                except ValueError as error:
                                    raise RuntimeError(
                                        f"反馈已连接，但 {side} 准备路径不可执行：{error}"
                                    ) from error
                                print(
                                    f"{side} 独立准备路径预检通过（未移动机器人）:",
                                    json.dumps(plan.report(), ensure_ascii=False),
                                    flush=True,
                                )
                        return 0
                except ValueError:
                    good = 0
                time.sleep(0.02)
            raise RuntimeError("机器人反馈检查失败；检查 UDP、防火墙、控制器报错")
        if c["input"] == "sdk":
            ensure_service(c)
            vr = SDKInput(c["vr_timeout_s"])
        else:
            vr = VRServer(c["host"], c["port"], c["vr_timeout_s"])
        stack.callback(vr.close)
        status = Status(c["status_port"])
        stack.callback(status.close)
        collection_publisher = None
        if args.collection_telemetry:
            from .collection_telemetry import Publisher
            collection_publisher = Publisher()
            stack.callback(collection_publisher.close)
        observer = None
        if args.viewer:
            from .visualization import Observer

            observer = Observer(c["status_port"], args.gripper_config, ROOT)
            # Register before motion callbacks: stop motion before waiting for the viewer.
            stack.callback(observer.close)
            observer.start()
            info = observer.status()
            print(info["error"] or f"同步观察窗口已启动，日志：{info['log_path']}", flush=True)
        launcher = None
        if not args.no_adb:
            launcher = PicoLauncher(c)
            launcher.start()
            stack.callback(launcher.close)
        ctrl = Controller(models, robot, c, prepare=args.prepare)
        from .xyz_recording import XYZSampler, XYZRecorder

        xyz_sampler = XYZSampler()
        xyz_value, xyz_last_sample = None, -float("inf")
        recorder = None
        if args.record_xyz:
            recorder = XYZRecorder(
                Path(__file__).resolve().parent.parent / "logs",
                {
                    "mode": "live" if args.live else "simulation",
                    "reverse_right_j2": False,
                    "scale": c["scale"],
                    "world_axes": "X forward, Y left, Z up",
                    "world_origin": "fixture URDF base_link origin",
                    "world_from_bases": {
                        s: a.base.tolist() for s, a in models.arms.items()
                    },
                    "control_points": models.mapping_report["control_points"],
                    "mount_orientation": models.mapping_report["mount_orientation"],
                    "coordinate_revision": models.mapping_report["coordinate_revision"],
                    "arm_geometry": models.mapping_report["arm_geometry"],
                    "geometry_transform": models.mapping_report["geometry_transform"],
                    "active_mount_source": models.mapping_report["active_mount_source"],
                    "installation_calibration": models.mapping_report[
                        "installation_calibration"
                    ],
                    "operator_convention": models.mapping_report["operator_convention"],
                    "robot_from_operator_rotation": models.robot_from_operator.tolist(),
                    "tool_collision": c.get("tool_collision"),
                    "controller_settings": {
                        key: c[key]
                        for key in (
                            "rate_hz",
                            "scale",
                            "max_joint_speed_deg_s",
                            "max_joint_accel_deg_s2",
                            "max_displacement_m",
                            "collision_margin_m",
                            "ik_rotation_weight_m",
                            "ik_shoulder_motion_weight",
                            "ik_singular_rotation_weight_m",
                            "ik_singular_sigma_low",
                            "ik_singular_sigma_high",
                            "joint_limit_soft_margin_deg",
                            "joint_limit_reserve_deg",
                            "joint_limit_escape_speed_deg_s",
                            "vr_timeout_s",
                            "robot_timeout_s",
                        )
                    },
                    "control_revision": CONTROL_REVISION,
                    "delta_origin": "per-arm grip engagement; compare rows in the same segment",
                },
            )
            stack.callback(recorder.close)
            print(
                f"XYZ 记录文件：{recorder.path}（毫米，20 Hz，反馈位置由关节 FK 计算）",
                flush=True,
            )
        # Register last so motion is stopped before closing HTTP/ADB/network services.
        stack.callback(robot.stop)
        grippers = None
        if gripper_config:
            from gripper.runtime import Grippers

            grippers = Grippers(
                gripper_config, args.live, c["vr_timeout_s"], robot=robot
            )
            stack.callback(grippers.close)
            print(
                "夹爪已接入：食指扳机控制开合；先松开再按下接管，B/Y 同时停止机械臂和夹爪。",
                flush=True,
            )
        shutdown = threading.Event()

        def stop_signal(*_):
            shutdown.set()

        signal.signal(signal.SIGINT, stop_signal)
        signal.signal(signal.SIGTERM, stop_signal)
        ctrl.start_session()
        symmetry = joint_symmetry(robot.q)
        if symmetry:
            print(f"J1 镜像对照：左={symmetry['left_j1_deg']:.2f}°，右={symmetry['right_j1_deg']:.2f}°，"
                  f"镜像偏差（左+右）={symmetry['j1_mirror_error_deg']:.2f}°。镜像动作应比较异号角度。", flush=True)
            if max(abs(x) for x in symmetry['mirror_error_deg']) > 5:
                print("当前双臂起始构型不对称；相似手柄动作不保证肩部角度相同。"
                      "需对称对照时可用 --prepare，并分别完成双臂准备后再接管。", flush=True)
        print(
            f"模式={'实机' if args.live else '仿真（不连接机器人）'} 状态=http://127.0.0.1:{c['status_port']}/status",
            flush=True,
        )
        print(
            "已选择固定准备动作，需持续握把等待该臂到位；日常直接遥操请去掉 --prepare。"
            if args.prepare
            else "启动时使能并保持当前位置；握把只负责接管/保持，松手不下电。",
            flush=True,
        )
        started = time.monotonic()
        last_print = 0.0
        last_state = None
        previous_cycle = None
        period = 1 / c["rate_hz"]
        last_gripper_fault = ""
        while not shutdown.is_set():
            now = time.monotonic()
            if args.duration > 0 and now - started >= args.duration:
                break
            interval_ms = (
                (now - previous_cycle) * 1000 if previous_cycle is not None else None
            )
            previous_cycle = now
            frame, emergency, error, connected = vr.snapshot()
            if grippers:
                grippers.submit(frame, connected, emergency or ctrl.fault)
            try:
                gripper_fault = grippers.status()["fault"] if grippers else ""
                if gripper_fault and gripper_fault != last_gripper_fault:
                    print("夹爪已独立锁停，双臂继续控制：" + gripper_fault, flush=True)
                last_gripper_fault = gripper_fault
                ctrl.tick(frame, connected, emergency, now)
            except (ValueError, RuntimeError) as e:
                if not ctrl.fault:
                    print(f"控制故障（停止前）: {e}", flush=True)
                    report = {
                        "error": str(e),
                        "arm_control": ctrl.status(),
                        "grips": frame.grips if frame else {},
                        "feedback_history": list(
                            getattr(robot, "feedback_history", [])
                        ),
                    }
                    try:
                        ctrl.pause(str(e), fault=True)
                    finally:
                        try:
                            directory = Path(__file__).resolve().parent.parent / "logs"
                            directory.mkdir(exist_ok=True)
                            path = directory / f"fault-{time.time_ns()}.json"
                            path.write_text(
                                json.dumps(report, ensure_ascii=False, indent=2)
                            )
                            print(f"故障记录: {path}", flush=True)
                        except OSError as log_error:
                            print(f"故障记录写入失败: {log_error}", flush=True)
            except Exception:
                ctrl.pause("unexpected exception", fault=True)
                raise
            if grippers:
                grippers.submit(frame, connected, emergency or ctrl.fault)
            value = {
                "published_monotonic_s": now,
                "viewer": observer.status() if observer else {"state": "disabled"},
                "arm_model_sources": models.mapping_report["arm_model_sources"],
                "grippers": grippers.status() if grippers else None,
                "control_timing": {"slow_cycles": ctrl.slow_cycles,
                                   "deferred_sends": ctrl.deferred_sends},
                "triggers": frame.triggers if frame else {},
                "mode": "live" if args.live else "simulation",
                "state": ctrl.state,
                "preparing": ctrl.preparing,
                "ik_solver": "marvin_vendor_jacobian_mirror_stable_v4",
                "mount_orientation": models.mapping_report["mount_orientation"],
                "coordinate_revision": models.mapping_report["coordinate_revision"],
                "arm_geometry": models.mapping_report["arm_geometry"],
                "geometry_transform": models.mapping_report["geometry_transform"],
                "active_mount_source": models.mapping_report["active_mount_source"],
                "world_from_bases": models.mapping_report["world_from_bases"],
                "world_from_mount": models.mapping_report["world_from_mount"],
                "installation_calibration": models.mapping_report[
                    "installation_calibration"
                ],
                "control_points": models.mapping_report["control_points"],
                "arm_control": ctrl.status(),
                "enabled_arms": sorted(robot.enabled_sides),
                "active_arms": sorted(ctrl.active_sides),
                "holding_arms": sorted(set(robot.enabled_sides) - ctrl.active_sides),
                "operator_rotation_signs": {s: [1, 1, 1] for s in models.arms},
                "arming_arms": sorted(robot.pending_sides),
                "stopping_arms": sorted(robot.stopping_sides),
                "operator_alignment": "same_facing",
                "operator_convention": models.mapping_report["operator_convention"],
                "robot_from_operator_rotation": models.robot_from_operator.tolist(),
                "heading_calibrated": ctrl.basis is not None,
                "head_tracking_pose": frame.head.tolist()
                if frame is not None
                else None,
                "calibration_head_tracking_pose": ctrl.calibration_head.tolist()
                if ctrl.basis is not None and ctrl.calibration_head is not None
                else None,
                "heading_policy": "first_clutch_head_yaw_locked",
                "vr_to_robot_rotation": ctrl.basis.tolist()
                if ctrl.basis is not None
                else None,
                "reason": ctrl.reason,
                "operator_hint": operator_hint(args.live, ctrl, frame, connected),
                "vr_connected": connected,
                "vr_frame_age_s": round(now - frame.received, 4) if frame else None,
                "vr_sequence": frame.sequence if frame else None,
                "vr_pose_valid": frame.tracked if frame else False,
                "vr_tracking_flags_available": False,
                "input": c["input"],
                "grips": frame.grips if frame else {},
                "control_grips": (frame.control_grips or frame.grips) if frame else {},
                "operator_translation_signs": c.get(
                    "operator_translation_signs", [1, 1, 1]
                ),
                "loop_interval_ms": round(interval_ms, 2)
                if interval_ms is not None
                else None,
                "commanded_joints_deg": {s: q.tolist() for s, q in ctrl.holds.items()},
                "joint_tracking_error_deg": {
                    s: float(max(abs(robot.q[s] - target)))
                    for s, target in ctrl.holds.items()
                }
                if robot.q is not None
                else {},
                "link_points_m": {
                    s: [a.base[:3, 3].tolist()]
                    + [t[:3, 3].tolist() for t in a.chain(robot.q[s])]
                    + [a.flange_fk(robot.q[s])[:3, 3].tolist()]
                    + [a.fk(robot.q[s])[:3, 3].tolist()]
                    for s, a in models.arms.items()
                }
                if robot.q is not None
                else {},
                "vr_error": error,
                "pico": launcher.status if launcher else "ADB disabled",
                "robot_enabled": robot.enabled,
                "robot_states": getattr(robot, "actual_states", {}),
                "robot_feedback_details": getattr(robot, "feedback_status", {}),
                "robot_feedback": args.live
                and all(now - t < c["robot_timeout_s"] for t in robot.changed.values()),
                "joints_deg": {s: q.tolist() for s, q in robot.q.items()}
                if robot.q is not None
                else None,
            }
            value["joint_symmetry"] = joint_symmetry(value["joints_deg"], value["arm_control"])
            if now - xyz_last_sample >= 0.05:
                xyz_value = xyz_sampler.sample(
                    ctrl,
                    frame,
                    connected,
                    now,
                    loop_ms=(time.monotonic() - now) * 1000,
                    interval_ms=interval_ms,
                )
                xyz_last_sample = now
                if recorder:
                    recorder.submit(xyz_value)
            value["xyz"] = xyz_value
            value["xyz_recording"] = recorder.status() if recorder else None
            value["loop_ms"] = round((time.monotonic() - now) * 1000, 2)
            with status.lock:
                status.value = value
            if collection_publisher is not None:
                collection_publisher.submit(value, frame, connected)
            control_state = (
                tuple((s, a.mode, a.preparing, a.pause_count) for s, a in ctrl.arms.items()),
                value["grippers"]["fault"] if value["grippers"] else "",
            )
            if control_state != last_state or now - last_print >= 2:
                event = {
                    "mode": value["mode"],
                    "run_started_monotonic_s": started,
                    "operator_translation_signs": value["operator_translation_signs"],
                    "operator_rotation_signs": value["operator_rotation_signs"],
                    "enabled_arms": value["enabled_arms"],
                    "active_arms": value["active_arms"],
                    "holding_arms": value["holding_arms"],
                    "loop_ms": value["loop_ms"],
                    "loop_interval_ms": value["loop_interval_ms"],
                    "control_timing": value["control_timing"],
                    "joint_tracking_error_deg": value["joint_tracking_error_deg"],
                    "joint_symmetry": value["joint_symmetry"],
                    "joints_deg": value["joints_deg"],
                    "mapping_version": "same_facing_relative_v2",
                    "control_revision": CONTROL_REVISION,
                    "ik_shoulder_motion_weight": c["ik_shoulder_motion_weight"],
                    "operator_convention": models.mapping_report["operator_convention"],
                    "vr_to_robot_rotation": value["vr_to_robot_rotation"],
                    "flange_poses": {
                        s: a.flange_fk(robot.q[s]).tolist()
                        for s, a in models.arms.items()
                    }
                    if robot.q is not None
                    else {},
                    "tcp_poses": {
                        s: a.fk(robot.q[s]).tolist() for s, a in models.arms.items()
                    }
                    if robot.q is not None
                    else {},
                    "control_points": models.mapping_report["control_points"],
                    "time_ns": time.time_ns(),
                    "ik_solver": value["ik_solver"],
                    "state": ctrl.state,
                    "arm_control": ctrl.status(),
                    "reason": ctrl.reason,
                    "grips": value["grips"],
                    "triggers": value["triggers"],
                    "grippers": value["grippers"],
                    "control_grips": value["control_grips"],
                    "vr_error": error,
                    "vr_age_s": value["vr_frame_age_s"],
                    "robot_feedback": value["robot_feedback_details"],
                    "head": frame.head.tolist() if frame else None,
                    "hands": {s: t.tolist() for s, t in frame.hands.items()}
                    if frame
                    else {},
                }
                try:
                    log_dir = Path(__file__).resolve().parent.parent / "logs"
                    log_dir.mkdir(exist_ok=True)
                    with (log_dir / "control-events.jsonl").open("a") as out:
                        out.write(json.dumps(event, ensure_ascii=False) + "\n")
                except OSError as log_error:
                    print(f"控制事件记录失败: {log_error}", flush=True)
                if args.verbose:
                    print(json.dumps(value, ensure_ascii=False), flush=True)
                else:
                    grips = value["grips"]
                    print(
                        f"[{value['mode']} / {ctrl.state}] VR={'已连接' if connected else '未连接'} "
                        f"握把 左={grips.get('left', 0):.2f} 右={grips.get('right', 0):.2f} "
                        f"| {value['operator_hint']}",
                        flush=True,
                    )
                    if recorder and xyz_value:
                        for side, label in (("left", "左"), ("right", "右")):
                            xyz = xyz_value["arms"][side]["feedback_fk_world_mm"]
                            delta = xyz_value["arms"][side][
                                "feedback_fk_delta_world_mm"
                            ]
                            fmt = lambda v: (
                                ", ".join(f"{x:+.2f}" for x in v)
                                if v is not None
                                else "--"
                            )
                            print(
                                f"[XYZ 毫米 / FK] {label}臂 X,Y,Z=({fmt(xyz)}) 接管后 Δ=({fmt(delta)})",
                                flush=True,
                            )
                        if recorder.error or recorder.dropped:
                            print(f"XYZ 记录状态：{recorder.status()}", flush=True)
                last_print, last_state = now, control_state
            shutdown.wait(max(0, period - (time.monotonic() - now)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"启动/运行失败: {exception_details(error)}", flush=True)
        raise SystemExit(1)
