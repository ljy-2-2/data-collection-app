from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import uvicorn

from .app import create_app
from .collection_uploader import CollectionUploader
from .collection_gate import CollectionGate
from .platform_auth import PlatformLogin
from .controller import TeleopController
from .devices import DeviceDetector
from .galaxis import GalaxisHeadsetSetup
from .gdk_env import add_xr_runtime_env, build_gdk_env
from .models import (
    DaggerStatus,
    DeviceStatus,
    HilSerlArtifacts,
    HilSerlStatus,
    MotionStatus,
    SessionStatus,
)
from .preview import GdkPreview
from .umi import UmiManager
from .umi_handoff import UmiControlHandoff
from .umi_uploader import UmiRosbagUploader
from .xr_service import XrServiceLauncher
from .runtime_paths import resolve_robot_python, resolve_gdk_root, resolve_xr_service


class MockController:
    hardware_enabled = False

    def status(self):
        return (
            [
                DeviceStatus(
                    id="robot", name="智元 G2", status="connected", detail="mock"
                ),
                DeviceStatus(
                    id="xr", name="Galaxis VR", status="connected", detail="mock"
                ),
                DeviceStatus(
                    id="headset", name="Pico", status="connected", detail="mock"
                ),
            ],
            SessionStatus(state="idle"),
            MotionStatus(state="idle"),
        )

    def start_session(self, _request):
        raise RuntimeError("mock mode does not execute teleop")

    def check_devices(self):
        return self.status()

    def stop_session(self, _session_id):
        raise RuntimeError("mock mode does not execute teleop")

    def move(self, _request):
        raise RuntimeError("mock mode does not move the robot")

    def dagger_status(self):
        return DaggerStatus(state="idle")

    def start_dagger(self, _request):
        raise RuntimeError("mock mode does not execute DAgger")

    def dagger_command_request(self, _request):
        raise RuntimeError("mock mode does not execute DAgger")

    def hil_serl_status(self):
        return HilSerlStatus(state="idle")

    def hil_serl_artifacts(self):
        return HilSerlArtifacts(root="/mock/hil-serl")

    def start_hil_serl(self, _request):
        raise RuntimeError("mock mode does not execute HIL-SERL")

    def hil_serl_command(self, _command):
        raise RuntimeError("mock mode does not execute HIL-SERL")


def resolve_gdk_python(
    configured: str | None, *, home: Path | None = None
) -> str | None:
    bundled = resolve_robot_python(configured)
    if bundled:
        return bundled
    user_home = home or Path.home()
    candidates = [
        user_home / "miniforge3/envs/bot/bin/python",
        user_home / "miniconda3/envs/bot/bin/python",
        user_home / ".conda/envs/bot/bin/python",
        user_home / "miniforge3/envs/g2/bin/python",
    ]
    return next(
        (
            str(path)
            for path in candidates
            if path.is_file() and os.access(path, os.X_OK)
        ),
        None,
    )


def default_collection_project() -> str:
    return str(
        Path(__file__).resolve().parents[3]
        / "teleop_control"
        / "vendor"
        / "gdk_data_collection"
    )


def default_umi_project() -> str:
    return str(
        Path(__file__).resolve().parents[3] / "umi_collection" / "vendor" / "galaxis"
    )


def prepare_umi_device_check(
    headset_setup: GalaxisHeadsetSetup,
) -> dict[str, object]:
    """Open Galaxis VR on PICO without starting the legacy teleop bridge.

    The UMI collector starts its ROS bridge after preflight. Leave an already
    running headset app untouched so its Send Data switch is preserved.
    """
    pico_ready = headset_setup.probe_ready()
    if pico_ready:
        pico_ready = headset_setup.wake()
    if not pico_ready and headset_setup.ensure_started(quick=True):
        for attempt in range(3):
            pico_ready = headset_setup.probe_ready()
            if pico_ready:
                break
            if attempt < 2:
                time.sleep(0.3)
    return {
        "vr_service_ready": pico_ready,
        "vr_service_detail": (
            "Galaxis VR 已打开，采集数据流将在进入监控后接通"
            if pico_ready
            else "Galaxis VR 未启动；请检查 PICO USB 调试连接"
        ),
        "pico_ready": pico_ready,
        "pico_detail": (
            "PICO 已连接，Galaxis VR 已打开"
            if pico_ready
            else (getattr(headset_setup, "last_error", "")
                  or "请连接并解锁 PICO、允许 USB 调试，并在头显中打开 Galaxis VR")
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Host-side teleop control service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument(
        "--mock", action="store_true", help="report three ready mock devices"
    )
    parser.add_argument(
        "--gdk-python",
        default=resolve_gdk_python(os.getenv("TELEOP_GDK_PYTHON")),
    )
    parser.add_argument(
        "--gdk-app-root",
        default=resolve_gdk_root(os.getenv("TELEOP_GDK_APP_ROOT")),
    )
    parser.add_argument("--adb", default=os.getenv("TELEOP_ADB", "adb"))
    parser.add_argument(
        "--robot-interface", default=os.getenv("TELEOP_ROBOT_INTERFACE", "auto")
    )
    parser.add_argument(
        "--collection-project",
        default=os.getenv("TELEOP_COLLECTION_PROJECT", default_collection_project()),
    )
    parser.add_argument(
        "--output",
        default=os.getenv("TELEOP_OUTPUT", "~/agibot_episodes/dual_arm"),
    )
    parser.add_argument("--task", default=os.getenv("TELEOP_TASK", "web-teleop"))
    parser.add_argument(
        "--api-url",
        default=os.getenv("DATA_PLATFORM_API_URL", "http://127.0.0.1:8000"),
        help="data platform API used for automatic controller-finished uploads",
    )
    parser.add_argument(
        "--right-arm-import-root",
        default=os.getenv("GDK_RIGHT_ARM_IMPORT_ROOT", "~/agibot_episodes/right_arm"),
        help="completed native right-arm GDK episodes discovered and imported automatically",
    )
    parser.add_argument(
        "--xr-service",
        default=resolve_xr_service(),
    )
    parser.add_argument(
        "--umi-project", default=os.getenv("UMI_PROJECT_ROOT", default_umi_project())
    )
    parser.add_argument(
        "--umi-data-root", default=os.getenv("UMI_DATA_ROOT", "~/galaxis_data")
    )
    parser.add_argument("--umi-python", default=os.getenv("UMI_PYTHON", sys.executable))
    parser.add_argument(
        "--umi-monitor-url",
        default=os.getenv("UMI_MONITOR_URL", "http://127.0.0.1:8770"),
    )
    args = parser.parse_args()
    # Explicit CLI paths need the same relocation/tilde handling as env defaults.
    args.gdk_python = resolve_gdk_python(args.gdk_python)
    args.gdk_app_root = resolve_gdk_root(args.gdk_app_root)
    args.xr_service = resolve_xr_service(args.xr_service)
    return args


def build_controller(
    args: argparse.Namespace,
    *,
    umi: UmiManager | None = None,
    headset_setup: GalaxisHeadsetSetup | None = None,
    xr_service: XrServiceLauncher | None = None,
):
    if args.mock:
        return MockController()
    if not args.gdk_python:
        # Login, uploads and UMI must work on machines without a G2 SDK.
        # Real robot checks/actions still report missing SDK instead of faking readiness.
        args.gdk_python = sys.executable
    root = Path(__file__).parents[3]
    recording_status = root / "teleop_control" / "runtime" / "recording-status.json"
    recording_status.unlink(missing_ok=True)
    args.gdk_unavailable_reason = None
    try:
        gdk_env = build_gdk_env(args.gdk_app_root, robot_interface=args.robot_interface)
    except FileNotFoundError:
        args.gdk_unavailable_reason = (
            "未安装 G2 SDK，请配置 TELEOP_GDK_APP_ROOT 和 TELEOP_GDK_PYTHON"
        )
        print(args.gdk_unavailable_reason, file=sys.stderr)
        gdk_env = os.environ.copy()
    gdk_env = add_xr_runtime_env(gdk_env, args.xr_service)
    move = Path(__file__).with_name("gdk_move.py")
    runner = root / "teleop_control" / "vendor" / "run_new_teleop_collection.py"
    guard = Path(__file__).with_name("process_guard.py")
    xr_observer = (
        root / "teleop_control" / "vendor" / "teleop_runtime" / "apps" / "observe_xr.py"
    )
    xr_runtime = xr_observer.parents[1]
    umi_compose = root / "umi_collection" / "vendor" / "galaxis" / "docker-compose.yml"
    gdk_env["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (
            str(xr_runtime),
            str(xr_runtime / "src"),
            gdk_env.get("PYTHONPATH", ""),
        )
        if item
    )
    detector = DeviceDetector(
        robot_interface=args.robot_interface,
        adb_path=args.adb,
        robot_unavailable_reason=args.gdk_unavailable_reason,
    )
    teleop_command = [
        args.gdk_python,
        str(runner),
        "--task",
        args.task,
        "--out",
        args.output,
        "--collection-project",
        args.collection_project,
        "--gdk-app-root",
        args.gdk_app_root,
        "--recording-status",
        str(recording_status),
        "--yes",
    ]
    move_command = [
        args.gdk_python,
        str(move),
        "--gdk-app-root",
        args.gdk_app_root,
    ]
    return TeleopController(
        detector=detector,
        teleop_command=teleop_command,
        move_command=move_command,
        # This standalone collector intentionally omits DAgger and HIL-SERL.
        dagger_command=None,
        dagger_runtime_dir=root / "teleop_control" / "runtime",
        guard_command=[sys.executable, str(guard)],
        xr_service=xr_service or XrServiceLauncher(args.xr_service),
        headset_setup=headset_setup or GalaxisHeadsetSetup(adb_path=args.adb),
        control_preparer=UmiControlHandoff(
            umi_compose,
            monitor_url=getattr(args, "umi_monitor_url", "http://127.0.0.1:8770"),
            stop_umi=umi.release_control if umi is not None else None,
        ),
        xr_preflight_command=[
            args.gdk_python,
            str(xr_observer),
            "--duration",
            "2",
            "--hz",
            "50",
            "--print-hz",
            "1",
        ],
        process_env=gdk_env,
        robot_interface=args.robot_interface,
        hil_serl=None,
    )


def build_preview(args: argparse.Namespace) -> GdkPreview | None:
    if args.mock or not args.gdk_python or getattr(args, "gdk_unavailable_reason", None):
        return None
    worker = Path(__file__).with_name("gdk_preview_worker.py")
    root = Path(__file__).parents[3]
    return GdkPreview(
        [args.gdk_python, str(worker)],
        process_env=build_gdk_env(
            args.gdk_app_root, robot_interface=args.robot_interface
        ),
        recording_status=root / "teleop_control" / "runtime" / "recording-status.json",
        dagger_preview_dir=root / "teleop_control" / "runtime" / "dagger-preview",
        cleanup_orphans=True,
        robot_interface=args.robot_interface,
    )


def main() -> None:
    from .adb_runtime import configure_adb_environment
    configure_adb_environment()
    args = parse_args()
    # The collector must recognize this exact bridge when handing PICO from
    # robot teleoperation back to UMI, including a private packaged runtime.
    os.environ["TELEOP_XR_SERVICE"] = args.xr_service
    origins = os.getenv(
        "TELEOP_CORS_ORIGINS",
        (
            "http://localhost:5173,http://127.0.0.1:5173,"
            "http://localhost:5174,http://127.0.0.1:5174,"
            "http://localhost:4173,http://127.0.0.1:4173,"
            "http://180.100.209.48:5173"
        ),
    )
    root = Path(__file__).parents[3]
    headset_setup = GalaxisHeadsetSetup(adb_path=args.adb)
    xr_service = XrServiceLauncher(args.xr_service)
    login = PlatformLogin(args.api_url)
    gate = CollectionGate(Path(args.umi_data_root).expanduser() / ".collection-platform.json", login)
    gate.start()
    umi = UmiManager(
        args.umi_project,
        args.umi_data_root,
        python=args.umi_python,
        monitor_url=args.umi_monitor_url,
        runtime_dir=root / "teleop_control" / "runtime" / "umi",
        prepare_check=lambda: prepare_umi_device_check(headset_setup),
    )
    controller = build_controller(
        args,
        umi=umi,
        headset_setup=headset_setup,
        xr_service=xr_service,
    )
    umi_uploader = UmiRosbagUploader(args.umi_data_root, args.api_url)
    app = create_app(
        controller,
        cors_origins=[item.strip() for item in origins.split(",")],
        preview=build_preview(args),
        umi=umi,
        umi_uploader=umi_uploader,
        platform_api_url=args.api_url,
        platform_login=login,
    )
    uploaders = [
        CollectionUploader(args.output, args.api_url),
        umi_uploader,
    ]
    tianji_uploader = CollectionUploader(app.state.tianji_manager.collection.output, args.api_url)
    app.state.tianji_manager.collection.uploader = tianji_uploader
    uploaders.append(tianji_uploader)
    output_root = Path(args.output).expanduser().resolve()
    right_arm_root = Path(args.right_arm_import_root).expanduser().resolve()
    if right_arm_root != output_root:
        uploaders.append(
            CollectionUploader(
                right_arm_root,
                args.api_url,
                discover_completed=True,
            )
        )
    for uploader in uploaders:
        uploader.start()
    try:
        # MJPEG responses are intentionally long-lived. Bound graceful shutdown
        # so a browser tab cannot keep a host-service restart waiting forever.
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            timeout_graceful_shutdown=3,
        )
    finally:
        try:
            app.state.tianji_manager.close()
        finally:
            gate.stop()
            for uploader in uploaders:
                uploader.stop()


if __name__ == "__main__":
    main()
