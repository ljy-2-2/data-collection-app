import json
from pathlib import Path
import sys
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "teleop_control" / "src"))

from teleop_control.app import _umi_camera_upstream, create_app  # noqa: E402
from teleop_control.galaxis import GalaxisHeadsetSetup  # noqa: E402
from teleop_control.main import MockController, prepare_umi_device_check  # noqa: E402
from teleop_control.umi import UmiManager, _CameraPreviewWorker, _failure_detail  # noqa: E402
from teleop_control import umi as umi_module  # noqa: E402
from teleop_control.models import UmiStartRequest  # noqa: E402


def test_standalone_routes_expose_collection_and_platform_status():
    app = create_app(
        MockController(),
        cors_origins=["http://127.0.0.1:4173"],
        platform_api_url="http://127.0.0.1:9",
    )
    paths = {route.path for route in app.routes}
    assert {
        "/healthz",
        "/status",
        "/platform/status",
        "/sessions",
        "/umi/sessions",
        "/umi/app-shutdown",
    } <= paths


@pytest.mark.parametrize(
    ("camera", "expected_url"),
    [
        (
            {"key": "left", "direct_url": "http://127.0.0.1:9091/stream?topic=/left", "proxy_url": "/proxy/stream?topic=/left"},
            "http://127.0.0.1:9091/stream?topic=/left",
        ),
        (
            {"key": "left", "proxy_url": "/proxy/stream?topic=/left"},
            "http://127.0.0.1:8770/proxy/stream?topic=/left",
        ),
    ],
)
def test_umi_camera_prefers_native_stream_without_fps_cap(camera, expected_url):
    assert _umi_camera_upstream(camera, "http://127.0.0.1:8770") == expected_url


@pytest.mark.parametrize("native", [False, True])
def test_umi_monitor_native_mode_is_explicit_and_independent_of_raw_transport(monkeypatch, native):
    import asyncio
    from teleop_control import app as module
    calls = []

    async def stream(url, **options):
        calls.append((url, options))
        yield b"camera-jpeg"

    monkeypatch.setattr(module, "preview_stream", stream)
    provider = SimpleNamespace(
        monitor_url="http://127.0.0.1:8770",
        status=lambda: SimpleNamespace(monitor={"cameras": [
            {"key": "left", "direct_url": "http://127.0.0.1:9091/stream?topic=/left"}
        ]}),
    )
    app = create_app(MockController(), cors_origins=[], umi=provider)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/umi/cameras/{camera_key}")

    async def request():
        response = await endpoint("left", raw=True, native=native)
        assert response.media_type == "application/octet-stream"
        assert b"".join([chunk async for chunk in response.body_iterator]) == b"camera-jpeg"

    asyncio.run(request())
    assert calls == [("http://127.0.0.1:9091/stream?topic=/left", {"native": native})]


@pytest.mark.parametrize("raw", [False, True])
def test_umi_preview_raw_transport_keeps_video_bytes_and_releases_stream(raw):
    import asyncio

    closed = []

    def frames():
        try:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\xff\xd9\r\n"
        finally:
            closed.append(True)

    app = create_app(
        MockController(), cors_origins=[],
        umi=SimpleNamespace(candidate_camera_stream=lambda key: frames()),
    )
    endpoint = next(route.endpoint for route in app.routes if route.path == "/umi/device-cameras/{camera_key}")

    async def request():
        response = await endpoint("video2", raw=raw)
        chunks = [chunk async for chunk in response.body_iterator]
        return response, b"".join(chunks)

    response, body = asyncio.run(request())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/octet-stream" if raw else "multipart/x-mixed-replace"
    )
    assert b"\xff\xd8\xff\xd9" in body
    assert closed == [True]


def test_umi_status_retains_latest_device_check(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    manager._device_check = {
        "ready": True,
        "cameras": 3,
        "expected_cameras": 3,
        "encoders": 2,
        "expected_encoders": 2,
    }
    monkeypatch.setattr(manager, "_monitor_state", lambda: (False, {}))
    monkeypatch.setattr(manager, "import_counts", lambda: (0, 0))

    status = manager.status()

    assert status.monitor["device_check"]["ready"] is True
    assert status.monitor["device_check"]["cameras"] == 3
    assert status.monitor["device_check"]["encoders"] == 2


def test_umi_status_recovers_live_collector_from_saved_session(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    manager._session_state_file.write_text(
        json.dumps({
            "pid": 12345,
            "command": "/collector.py",
            "session_id": "saved-session",
            "task": "测试采集",
            "collection_profile": "dual",
        }),
        encoding="utf-8",
    )

    class RunningProcess:
        pid = 12345
        returncode = None

        def __init__(self, pid, command):
            assert (pid, command) == (12345, "/collector.py")

        def poll(self):
            return None

    monkeypatch.setattr(umi_module, "_RecoveredProcess", RunningProcess)
    monkeypatch.setattr(manager, "_monitor_state", lambda: (True, {"ts": 1, "vr": {}, "encoders": {}}))
    monkeypatch.setattr(manager, "import_counts", lambda: (0, 0))

    status = manager.status()

    assert status.session_id == "saved-session"
    assert status.state == "starting"
    assert status.monitor_connected is True


def test_umi_external_collector_blocks_device_previews(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    probes = []

    def inspect(command, **kwargs):
        probes.append(command)
        return SimpleNamespace(returncode=0, stdout="true\n")

    monkeypatch.setattr(umi_module.subprocess, "run", inspect)
    monkeypatch.setattr(manager, "_monitor_state", lambda: (False, {}))
    monkeypatch.setattr(manager, "import_counts", lambda: (0, 0))

    check = manager.check().monitor["device_check"]
    preview = manager.encoder_preview()

    assert probes == [["docker", "inspect", "--format", "{{.State.Running}}", "galaxis"]]
    assert check["stack_occupied"] is True
    assert check["ready"] is False
    assert check["hardware_ready"] is False
    assert preview["preview_suspended"] is True
    assert "占用串口" in preview["message"]
    assert manager._encoder_threads == []


def test_umi_shutdown_only_stops_own_container(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path, tmp_path / "data", python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(manager, "_monitor_state", lambda: (False, {}))
    commands = []
    running = True

    def run(command, **_kwargs):
        nonlocal running
        commands.append(command)
        if command[1] == "inspect":
            if command[-1] != "galaxis":
                return SimpleNamespace(returncode=1, stdout="", stderr="No such object")
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "com.docker.compose.project.config_files": str(tmp_path / "docker-compose.yml"),
            }) + f"|{str(running).lower()}\n", stderr="")
        running = False
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(umi_module.subprocess, "run", run)

    manager._stop_owned_stack()
    assert ["docker", "stop", "--time", "1", "galaxis"] in commands
    assert commands[0][-1] == "galaxis"
    assert running is False


def test_fresh_install_without_docker_can_initialize_and_close_app(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path, tmp_path / "data", python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(manager, "_monitor_state", lambda: (False, {}))

    def missing_docker(command, **kwargs):
        assert command[0] == "docker"
        raise FileNotFoundError("docker")

    monkeypatch.setattr(umi_module.subprocess, "run", missing_docker)
    assert manager.shutdown_for_app().state == "idle"
    with pytest.raises(RuntimeError, match="未安装 Docker，UMI 采集暂不可用"):
        manager.check()
    assert manager.shutdown_for_app().state == "idle"


@pytest.mark.parametrize("activity", ["session", "process", "monitor"])
def test_missing_docker_does_not_waive_active_collection_cleanup(tmp_path, monkeypatch, activity):
    manager = UmiManager(
        tmp_path, tmp_path / "data", python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(manager, "_monitor_state", lambda: (activity == "monitor", {}))
    if activity == "session":
        manager._session_id = "unfinished"
    if activity == "process":
        manager._process = SimpleNamespace(poll=lambda: None)

    def missing_docker(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(umi_module.subprocess, "run", missing_docker)
    with pytest.raises(RuntimeError, match="无法确认采集容器已停止"):
        manager._stop_owned_stack()


def test_umi_shutdown_ignores_other_container(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path, tmp_path / "data", python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(manager, "_monitor_state", lambda: (False, {}))
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "com.docker.compose.project.config_files": "/different/docker-compose.yml",
        }) + "|true\n", stderr="")

    monkeypatch.setattr(umi_module.subprocess, "run", run)

    manager._stop_owned_stack()
    assert len(commands) == 4
    assert all(command[1] == "inspect" for command in commands)


def test_umi_shutdown_refuses_to_stop_recording_container(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path, tmp_path / "data", python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )

    monkeypatch.setattr(manager, "_monitor_state", lambda: (True, {"recording": True}))
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "com.docker.compose.project.config_files": str(tmp_path / "docker-compose.yml"),
        }) + "|true\n", stderr="")

    monkeypatch.setattr(umi_module.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="仍在录制"):
        manager._stop_owned_stack()
    assert len(commands) == 4


def test_umi_stop_waits_for_process_and_owned_stack_before_returning(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path, tmp_path / "data", python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    events = []

    class Collector:
        pid = 1234
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            events.append(("wait", timeout))
            self.returncode = 0
            return 0

    manager._process = Collector()
    manager._session_id = "session-1"
    manager._session_state_file.write_text("owned", encoding="utf-8")
    monkeypatch.setattr(manager, "_monitor_state", lambda: (True, {"recording": False}))
    monkeypatch.setattr(manager, "_wait_for_hdf5_finalizations", lambda: events.append("finalized"))
    monkeypatch.setattr(manager, "_stop_owned_stack", lambda: events.append("compose-stop"))
    monkeypatch.setattr(umi_module.os, "killpg", lambda pid, sig: events.append(("signal", pid, sig)))

    stopped = manager.stop("session-1")

    assert stopped.state == "idle"
    assert stopped.session_id is None
    assert events == ["finalized", ("signal", 1234, umi_module.signal.SIGINT), ("wait", 5), "compose-stop"]
    assert not manager._session_state_file.exists()


def test_umi_stop_failure_keeps_session_for_retry(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path, tmp_path / "data", python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    manager._session_id = "session-1"
    monkeypatch.setattr(manager, "_monitor_state", lambda: (False, {}))
    monkeypatch.setattr(manager, "_stop_owned_stack", lambda: (_ for _ in ()).throw(RuntimeError("容器仍在运行")))

    with pytest.raises(RuntimeError, match="容器仍在运行"):
        manager.stop("session-1")

    assert manager.status().session_id == "session-1"
    assert manager.status().state == "error"


def test_umi_app_shutdown_saves_active_round_before_stopping(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    manager = UmiManager(
        tmp_path, data_root, python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    events = []
    state = SimpleNamespace(session_id="session-1", recording=True, monitor_connected=True)
    monkeypatch.setattr(manager, "status", lambda: state)

    def save_round(action):
        events.append(action)
        round_dir = data_root / "rosbags" / "task" / "round"
        round_dir.mkdir(parents=True)
        (round_dir / "meta.json").write_text("{}", encoding="utf-8")
        (round_dir / "metadata.yaml").write_text("rosbag2_bagfile_information: {}", encoding="utf-8")
        return state

    monkeypatch.setattr(manager, "record", save_round)
    monkeypatch.setattr(manager, "_wait_for_hdf5_finalizations", lambda: events.append("finalized"))
    monkeypatch.setattr(manager, "stop", lambda session_id: events.append(f"stop:{session_id}"))
    monkeypatch.setattr(manager, "_stop_owned_stack", lambda: events.append("compose-stop"))

    manager.shutdown_for_app()

    assert events == ["stop", "finalized", "stop:session-1", "compose-stop"]
    assert manager._session_id is None


def test_umi_app_shutdown_keeps_collector_if_recording_state_unknown(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path, tmp_path / "data", python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(
        manager, "status",
        lambda: SimpleNamespace(session_id="session-1", recording=False, monitor_connected=False),
    )
    monkeypatch.setattr(manager, "stop", lambda *_args: pytest.fail("collector must be preserved"))

    with pytest.raises(RuntimeError, match="无法确认 UMI 录制状态"):
        manager.shutdown_for_app()


def test_umi_hdf5_shutdown_wait_skips_completed_rounds(tmp_path):
    data_root = tmp_path / "data"
    manager = UmiManager(
        tmp_path, data_root, python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    round_dir = data_root / "hdf5" / "task" / "round"
    staging = round_dir / ".rosbag_staging"
    staging.mkdir(parents=True)
    (staging / "metadata.yaml").write_text("rosbag2_bagfile_information: {}", encoding="utf-8")
    (round_dir / "manifest.json").write_text('{"state":"complete"}', encoding="utf-8")

    manager._wait_for_hdf5_finalizations()


def test_umi_external_collector_releases_preview_after_stopping(tmp_path, monkeypatch):
    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    running = True

    def inspect(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="true" if running else "false")

    monkeypatch.setattr(umi_module.subprocess, "run", inspect)
    assert manager._sync_external_stack() is True
    assert "external-stack" in manager._encoder_preview_blockers

    running = False
    manager._stack_probe_at = 0.0
    assert manager._sync_external_stack() is False
    assert "external-stack" not in manager._encoder_preview_blockers


@pytest.mark.parametrize("has_frame", [True, False])
def test_umi_camera_preview_uses_cached_check_and_requires_frame(
    tmp_path, monkeypatch, has_frame
):
    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    manager._device_check = {"camera_candidates": ["/dev/video2"]}
    monkeypatch.setattr(
        manager,
        "check",
        lambda *_args: pytest.fail("camera preview must not repeat hardware check"),
    )
    monkeypatch.setitem(sys.modules, "cv2", object())
    monkeypatch.setattr(manager, "_selected_camera_mode", lambda _: ([], {}, "fixture"))

    class Subscription:
        closed = False

        def close(self):
            self.closed = True

    subscription = Subscription()

    class Worker:
        error = ""

        def __init__(self, device, on_idle, mode):
            self.device = device

        def subscribe(self):
            return subscription

        def wait_for_frame(self, timeout):
            assert timeout > 0
            return has_frame

    monkeypatch.setattr(umi_module, "_CameraPreviewWorker", Worker)

    if has_frame:
        assert manager.candidate_camera_stream("video2") is subscription
        assert not subscription.closed
    else:
        with pytest.raises(RuntimeError, match="未能读取到相机画面"):
            manager.candidate_camera_stream("video2")
        assert subscription.closed


def test_umi_camera_health_reports_recent_frame(tmp_path):
    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    manager._device_check = {"camera_candidates": ["/dev/video2"]}
    assert manager.candidate_camera_ready("video2") is False
    manager._camera_workers["/dev/video2"] = SimpleNamespace(has_recent_frame=lambda: True)
    assert manager.candidate_camera_ready("video2") is True
    with pytest.raises(ValueError, match="camera not found"):
        manager.candidate_camera_ready("video3")


def test_umi_camera_health_rejects_stale_frame():
    worker = object.__new__(_CameraPreviewWorker)
    worker._condition = threading.Condition()
    worker._payload = b"jpeg"
    worker._last_frame_at = time.monotonic() - 3
    assert not worker.has_recent_frame()
    worker._last_frame_at = time.monotonic()
    assert worker.has_recent_frame()


def test_umi_preflight_prepares_pico_without_legacy_service():
    events = []

    class Headset:
        def probe_ready(self):
            events.append("probe")
            return True

        def wake(self):
            events.append("wake")
            return True

        def ensure_started(self, *, quick=False):
            pytest.fail("running Galaxis VR must not be relaunched")

    result = prepare_umi_device_check(Headset())

    assert events == ["probe", "wake"]
    assert result["vr_service_ready"] is True
    assert result["pico_ready"] is True


def test_umi_preflight_opens_missing_vr_app():
    events = []

    class Headset:
        running = False

        def probe_ready(self):
            events.append("probe")
            return self.running

        def ensure_started(self, *, quick=False):
            assert quick is True
            events.append("start")
            self.running = True
            return True

    result = prepare_umi_device_check(Headset())

    assert events == ["probe", "start", "probe"]
    assert result["vr_service_ready"] is True
    assert result["pico_ready"] is True


def test_umi_preflight_reports_missing_adb_headset():
    class Headset:
        def probe_ready(self):
            return False

        def ensure_started(self, *, quick=False):
            assert quick is True
            return False

    result = prepare_umi_device_check(Headset())

    assert result["vr_service_ready"] is False
    assert result["pico_ready"] is False
    assert "USB 调试" in str(result["pico_detail"])


@pytest.mark.parametrize("installed", [True, False])
def test_pico_uses_bundled_apk_only_when_missing(tmp_path, installed):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[-1] == "devices":
            return SimpleNamespace(returncode=0, stdout="List of devices attached\npico\tdevice\n", stderr="")
        if command[-3:] == ["pm", "path", "com.galaxis.vrteleop"]:
            return SimpleNamespace(returncode=0 if installed else 1, stdout="package:/existing.apk" if installed else "", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    headset = GalaxisHeadsetSetup(run_command=run)
    headset.bundled_apk = tmp_path / "headset.apk"
    headset.bundled_apk.write_bytes(b"fixture")
    headset.ensure_started(quick=True)
    assert any("install" in c for c in commands) is not installed
    assert not any("uninstall" in c or "-r" in c for c in commands)


def test_umi_pico_probe_is_quick_and_read_only():
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs["timeout"]))
        if command == ["adb", "devices"]:
            return subprocess.CompletedProcess(command, 0, "List of devices attached\nPICO123\tdevice\n", "")
        return subprocess.CompletedProcess(command, 0, "1234\n", "")

    headset = GalaxisHeadsetSetup(run_command=run)

    assert headset.probe_ready() is True
    assert commands == [
        (["adb", "devices"], 2.0),
        (["adb", "-s", "PICO123", "shell", "pidof", "com.galaxis.vrteleop"], 2.0),
    ]


def test_umi_pico_wake_does_not_restart_running_app():
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    assert GalaxisHeadsetSetup(run_command=run).wake() is True
    assert commands == [["adb", "shell", "input", "keyevent", "KEYCODE_WAKEUP"]]


@pytest.mark.parametrize("already_running", [True, False])
def test_umi_pico_ensure_only_launches_when_not_running(already_running):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command == ["adb", "devices"]:
            return subprocess.CompletedProcess(
                command, 0, "List of devices attached\nPICO123\tdevice\n", ""
            )
        if "pidof" in command:
            return subprocess.CompletedProcess(
                command, 0 if already_running else 1,
                "1234\n" if already_running else "", ""
            )
        if command[-2:] == ["reverse", "--list"]:
            return subprocess.CompletedProcess(command, 0, "PICO123 tcp:63901 tcp:63901\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    headset = GalaxisHeadsetSetup(run_command=run)

    assert headset.ensure_started(quick=True) is True
    assert any("reverse" in command for command in commands)
    assert any("monkey" in command for command in commands) is not already_running
    assert not any("force-stop" in command for command in commands)


def test_umi_start_rejects_missing_hardware_before_launch(tmp_path, monkeypatch):
    script = tmp_path / "tools" / "galaxis_collection.py"
    script.parent.mkdir()
    script.touch()
    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(
        manager,
        "check",
        lambda *_args: SimpleNamespace(monitor={"device_check": {"ready": False}}),
    )

    with pytest.raises(RuntimeError, match="硬件检测未通过"):
        manager.start(UmiStartRequest(task="测试采集", collection_profile="dual"))
    assert manager._process is None


def test_umi_monitor_requires_fresh_headset_and_encoder_data(tmp_path):
    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    monitor = {
        "task_status": "received_task:测试采集",
        "cameras": [{"key": side} for side in ("left", "center", "right")],
        "encoders": {
            side: {"online": True, "rate_hz": 100} for side in ("left", "right")
        },
        "vr": {
            side: {"online": True, "rate_hz": 90, "tracked": True, "valid": True}
            for side in ("hmd", "left", "right")
        },
    }
    assert manager._monitor_ready(monitor)
    manager._process = SimpleNamespace(poll=lambda: None)
    manager._session_id = "live-session"
    manager._monitor_state = lambda: (True, monitor)
    manager.import_counts = lambda: (0, 0)
    assert manager.status().state == "ready"
    monitor["vr"]["left"]["rate_hz"] = 0
    assert not manager._monitor_ready(monitor)
    monitor["vr"]["left"]["rate_hz"] = 90
    monitor["encoders"]["right"]["rate_hz"] = 0
    assert not manager._monitor_ready(monitor)
    monitor["encoders"]["right"]["rate_hz"] = 100
    monitor["vr"]["right"]["tracked"] = False
    assert not manager._monitor_ready(monitor)


def test_umi_failure_prefers_missing_compose_env_over_task_notice():
    detail = _failure_detail(
        [
            "env file /opt/galaxis/.env not found",
            "ERROR: Command failed (1): docker compose up",
            "任务大类不存在,已自动创建: UMI_双臂采集",
        ]
    )

    assert detail == "env file /opt/galaxis/.env not found"


def test_umi_record_requires_active_session(tmp_path):
    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )

    with pytest.raises(RuntimeError, match="请先启动 UMI 采集服务"):
        manager.record("start")


def test_umi_record_waits_for_monitor_ready(tmp_path, monkeypatch):
    class RunningProcess:
        @staticmethod
        def poll():
            return None

    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    manager._process = RunningProcess()
    manager._session_id = "starting-session"
    monkeypatch.setattr(manager, "_monitor_state", lambda: (False, {}))

    with pytest.raises(RuntimeError, match="正在启动"):
        manager.record("start")


def test_umi_record_requires_live_sensor_data(tmp_path, monkeypatch):
    class RunningProcess:
        @staticmethod
        def poll():
            return None

    manager = UmiManager(
        tmp_path,
        tmp_path / "data",
        python=sys.executable,
        runtime_dir=tmp_path / "runtime",
    )
    manager._process = RunningProcess()
    manager._session_id = "starting-session"
    monkeypatch.setattr(manager, "_monitor_state", lambda: (True, {"vr": {}}))

    with pytest.raises(RuntimeError, match="实时数据尚未就绪"):
        manager.record("start")


def test_umi_missing_head_data_reports_reason_without_hiding_other_streams(tmp_path):
    manager=UmiManager(tmp_path,tmp_path/'data',python=sys.executable,runtime_dir=tmp_path/'runtime')
    monitor={'vr':{'hmd':{'online':False},'left':{'online':True},'right':{'online':True}}, 'camera_quality_issues':[]}
    assert '头盔' in manager._live_readiness_message(monitor)
    monitor['vr']['hmd']['online']=True
    assert manager._live_readiness_message(monitor)==''


def test_umi_exit_accepts_durable_interrupted_round_without_finished_meta(tmp_path, monkeypatch):
    data_root = tmp_path/'data'
    manager = UmiManager(tmp_path, data_root, python=sys.executable, runtime_dir=tmp_path/'runtime')
    state = SimpleNamespace(session_id='session-1', recording=True, monitor_connected=True)
    monkeypatch.setattr(manager, 'status', lambda: state)
    events = []
    def save_round(action):
        events.append(action)
        journals=data_root/'rosbags'/'.capture-journals';journals.mkdir(parents=True)
        (journals/'round.json').write_text(json.dumps({'state':'interrupted', 'folder':'task/round'}))
        return state
    monkeypatch.setattr(manager, 'record', save_round)
    monkeypatch.setattr(manager, '_wait_for_hdf5_finalizations', lambda: None)
    monkeypatch.setattr(manager, 'stop', lambda _: events.append('collector-stop'))
    monkeypatch.setattr(manager, '_stop_owned_stack', lambda: events.append('stack-stop'))
    manager.shutdown_for_app()
    assert events == ['stop','collector-stop','stack-stop']
