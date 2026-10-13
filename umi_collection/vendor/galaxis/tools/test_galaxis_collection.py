"""Unit tests for the Galaxis collection command-line workflow (local offline mode)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import galaxis_collection as gc


class LocalTaskStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "tasks.yaml"
        patcher = patch.object(gc, "TASK_STORE_PATH", self.store_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_task_round_and_version(self):
        task, version = gc.create_task(
            name="Galaxis",
            description="演示任务",
            deadline=None,
            robot_type="galaxis_portable",
        )
        self.assertEqual(task["id"], "Galaxis")
        self.assertEqual(version["id"], "Galaxis-version-1")
        subtasks = task["subtasks"]
        self.assertEqual(len(subtasks), 1)
        self.assertEqual(subtasks[0]["name"], "采集回合")

    def test_create_task_rejects_duplicate(self):
        gc.create_task(
            name="Galaxis", description="", deadline=None, robot_type=""
        )
        with self.assertRaises(gc.CollectionError):
            gc.create_task(
                name="Galaxis", description="", deadline=None, robot_type=""
            )

    def test_ensure_offline_task_puts_requested_first(self):
        gc.create_task(
            name="First", description="", deadline=None, robot_type=""
        )
        gc.create_task(
            name="Second", description="", deadline=None, robot_type=""
        )
        second = gc.resolve_task("Second")
        gc.ensure_offline_task(second)
        store = gc.load_task_store()
        self.assertEqual(store["tasks"][0]["id"], "Second")
        self.assertEqual(
            store["robot"], {"id": "galaxis-portable", "name": "Galaxis Portable"}
        )

    def test_ensure_offline_task_merges_round_index(self):
        """已存在的任务必须保存新传入的 round_index,保证跨会话回合编号递增。"""
        gc.create_task(
            name="Galaxis", description="旧描述", deadline=None, robot_type=""
        )
        task = gc.resolve_task("Galaxis")
        task["round_index"] = 5
        gc.ensure_offline_task(task)
        stored = gc.resolve_task("Galaxis")
        self.assertEqual(stored.get("round_index"), 5)
        self.assertEqual(stored.get("description"), "旧描述")

    def test_resolve_task_by_name_or_missing(self):
        gc.create_task(
            name="Galaxis", description="", deadline=None, robot_type=""
        )
        self.assertEqual(gc.resolve_task("Galaxis")["id"], "Galaxis")
        with self.assertRaises(gc.CollectionError):
            gc.resolve_task("Missing")

    def test_ensure_task_category_auto_creates_missing(self):
        task = gc.ensure_task_category("新大类")
        self.assertEqual(task["name"], "新大类")
        again = gc.ensure_task_category("新大类")
        self.assertEqual(again["id"], task["id"])
        self.assertEqual(len(gc.task_list()), 1)

    def test_ensure_task_category_reuses_sanitized_name(self):
        task = gc.ensure_task_category("UMI 双臂采集")
        self.assertEqual(task["name"], "UMI_双臂采集")
        again = gc.ensure_task_category("UMI 双臂采集")
        self.assertEqual(again["id"], task["id"])
        self.assertEqual(len(gc.task_list()), 1)

    def test_next_round_index_accumulates_over_sessions(self):
        with patch.object(gc, "DATA_DIR", Path(self._tmp.name)):
            task_dir = gc.DATA_DIR / "rosbags" / "Galaxis"
            task_dir.mkdir(parents=True)
            for name in (
                "Galaxis_001_20260819-101010",
                "Galaxis_005_20260819-111111",
            ):
                (task_dir / name).mkdir()
            self.assertEqual(gc.next_round_index("Galaxis"), 6)
            self.assertEqual(gc.next_round_index("不存在"), 1)

    def test_next_round_index_uses_selected_format_directory(self):
        with patch.object(gc, "DATA_DIR", Path(self._tmp.name)):
            task_dir = gc.DATA_DIR / "hdf5" / "Galaxis"
            task_dir.mkdir(parents=True)
            (task_dir / "Galaxis_003_20260819-101010").mkdir()
            self.assertEqual(gc.next_round_index("Galaxis", "hdf5"), 4)

    def test_recording_base_dir_is_created_for_operator(self):
        with patch.object(gc, "DATA_DIR", Path(self._tmp.name)):
            path = gc.ensure_recording_base_dir("hdf5")
            self.assertEqual(path, Path(self._tmp.name) / "hdf5")
            self.assertEqual(path.stat().st_uid, gc.os.getuid())
            self.assertEqual(path.stat().st_gid, gc.os.getgid())


class OfflineRuntimeTest(unittest.TestCase):
    def test_apply_offline_runtime_writes_single_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            config_dir = project / "galaxis_bringup" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "galaxis_config.yaml").write_text(
                "robot_config:\n"
                "  /**:\n"
                "    ros__parameters:\n"
                "      api_key: keep-me\n",
                encoding="utf-8",
            )
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)

            with (
                patch.object(gc, "PROJECT_DIR", project),
                patch.object(gc, "run_checked", side_effect=fake_run),
                patch.object(gc, "ensure_collection_images"),
                patch.object(gc, "ensure_background_finalizer"),
                patch.object(gc, "compose_command", side_effect=lambda f, *a: [str(f), *a]),
            ):
                gc.apply_offline_runtime()

            text = (config_dir / "galaxis_config.yaml").read_text(encoding="utf-8")
            self.assertIn("offline_mode: true", text)
            self.assertIn("task_file: /opt/data/tasks.yaml", text)
            self.assertIn("record_format: rosbag", text)
            self.assertIn("record_base_dir: /opt/data/rosbags", text)
            self.assertIn("api_key: keep-me", text)
            self.assertIn("delete_after_upload: false", text)
            self.assertIn("local_retention_hours: 0", text)
            self.assertEqual(len(calls), 1, "启动前只应生成配置，不应重复启动容器")
            self.assertEqual(Path(calls[0][1]).name, "build_runtime_configs.py")

    def test_apply_offline_runtime_selects_hdf5(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            config_dir = project / "galaxis_bringup" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "galaxis_config.yaml").write_text(
                "robot_config:\n  /**:\n    ros__parameters: {}\n",
                encoding="utf-8",
            )
            with (
                patch.object(gc, "PROJECT_DIR", project),
                patch.object(gc, "run_checked"),
                patch.object(gc, "ensure_collection_images"),
                patch.object(gc, "ensure_background_finalizer"),
            ):
                gc.apply_offline_runtime("hdf5")

            config = gc.yaml.safe_load(
                (config_dir / "galaxis_config.yaml").read_text(encoding="utf-8")
            )
            params = config["robot_config"]["/**"]["ros__parameters"]
            self.assertEqual(params["record_format"], "hdf5")
            self.assertEqual(params["record_base_dir"], "/opt/data/hdf5")


class CommandLineTest(unittest.TestCase):
    def test_sample_format_defaults_to_rosbag(self):
        args = gc.build_parser().parse_args(["sample", "--task", "Galaxis"])
        self.assertEqual(args.record_format, "rosbag")

    def test_sample_accepts_hdf5_format(self):
        args = gc.build_parser().parse_args(
            [
                "sample", "--task", "Galaxis", "--device-setup",
                "--format", "hdf5", "--collection-profile", "single-left",
            ]
        )
        self.assertEqual(args.record_format, "hdf5")
        self.assertEqual(args.collection_profile, "single-left")

    def test_hdf5_staging_round_is_visible_as_stopped(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            gc, "DATA_DIR", Path(tmp)
        ):
            round_dir = Path(tmp) / "hdf5" / "Galaxis" / "Galaxis_001"
            staging = round_dir / ".rosbag_staging"
            staging.mkdir(parents=True)
            (staging / "metadata.yaml").write_text("rosbag2_bagfile_information: {}")

            self.assertEqual(gc.recording_directories("hdf5"), {round_dir})

    def test_startup_failure_stops_stack_and_releases_preview_devices(self):
        task = {"id": "Galaxis", "name": "Galaxis"}
        with (
            patch.object(gc, "ensure_recording_base_dir"),
            patch.object(gc, "collection_profile_available", return_value=True),
            patch.object(gc, "read_collection_profile", return_value=gc.DEFAULT_COLLECTION_PROFILE),
            patch.object(gc, "center_camera_source", return_value="usb"),
            patch.object(gc, "stop_g2_camera_stream"),
            patch.object(gc, "ensure_task_category", return_value=task),
            patch.object(gc, "next_round_index", return_value=1),
            patch.object(gc, "ensure_offline_task"),
            patch.object(gc, "apply_offline_runtime"),
            patch.object(gc, "terminate_legacy_robotics_service"),
            patch.object(gc, "ensure_collection_stack"),
            patch.object(gc, "start_vr", side_effect=gc.CollectionError("PICO missing")),
            patch.object(gc, "stop_collection_stacks") as stop_stacks,
        ):
            result = gc.main([
                "sample", "--task", "Galaxis", "--skip-device-setup", "--no-open"
            ])

        self.assertEqual(result, 1)
        stop_stacks.assert_called_once_with(None)

    def test_startup_interrupt_stops_stack_before_monitor_loop(self):
        task = {"id": "Galaxis", "name": "Galaxis"}
        with (
            patch.object(gc, "ensure_recording_base_dir"),
            patch.object(gc, "collection_profile_available", return_value=True),
            patch.object(gc, "read_collection_profile", return_value=gc.DEFAULT_COLLECTION_PROFILE),
            patch.object(gc, "center_camera_source", return_value="usb"),
            patch.object(gc, "stop_g2_camera_stream"),
            patch.object(gc, "ensure_task_category", return_value=task),
            patch.object(gc, "next_round_index", return_value=1),
            patch.object(gc, "ensure_offline_task"),
            patch.object(gc, "apply_offline_runtime"),
            patch.object(gc, "terminate_legacy_robotics_service"),
            patch.object(gc, "ensure_collection_stack"),
            patch.object(gc, "start_vr", side_effect=KeyboardInterrupt),
            patch.object(gc, "stop_collection_stacks") as stop_stacks,
        ):
            result = gc.main([
                "sample", "--task", "Galaxis", "--skip-device-setup", "--no-open"
            ])

        self.assertEqual(result, 0)
        stop_stacks.assert_called_once_with(None)

    def test_partial_stack_start_failure_or_interrupt_releases_owned_services(self):
        for error, expected_code in ((gc.CollectionError("compose failed"), 1), (KeyboardInterrupt(), 0)):
            with self.subTest(error=type(error).__name__):
                task = {"id": "Galaxis", "name": "Galaxis"}
                events = []
                with (
                    patch.object(gc, "ensure_recording_base_dir"),
                    patch.object(gc, "read_collection_profile", return_value=gc.DEFAULT_COLLECTION_PROFILE),
                    patch.object(gc, "stop_g2_camera_stream"),
                    patch.object(gc, "ensure_task_category", return_value=task),
                    patch.object(gc, "next_round_index", return_value=1),
                    patch.object(gc, "ensure_offline_task"),
                    patch.object(gc, "apply_offline_runtime"),
                    patch.object(gc, "terminate_legacy_robotics_service", side_effect=lambda: events.append("legacy")),
                    patch.object(gc, "ensure_collection_stack", side_effect=error),
                    patch.object(gc, "stop_collection_stacks", side_effect=lambda *_: events.append("cleanup")) as cleanup,
                ):
                    code = gc.main(["sample", "--task", "Galaxis", "--skip-device-setup", "--no-open"])
                self.assertEqual(code, expected_code)
                self.assertEqual(events, ["legacy", "cleanup"])
                cleanup.assert_called_once_with(None)

    def test_legacy_cleanup_failure_does_not_stop_or_start_any_collection_stack(self):
        task = {"id": "Galaxis", "name": "Galaxis"}
        with (
            patch.object(gc, "ensure_recording_base_dir"),
            patch.object(gc, "read_collection_profile", return_value=gc.DEFAULT_COLLECTION_PROFILE),
            patch.object(gc, "stop_g2_camera_stream"),
            patch.object(gc, "ensure_task_category", return_value=task),
            patch.object(gc, "next_round_index", return_value=1),
            patch.object(gc, "ensure_offline_task"),
            patch.object(gc, "apply_offline_runtime"),
            patch.object(gc, "terminate_legacy_robotics_service", side_effect=gc.CollectionError("permission denied")),
            patch.object(gc, "ensure_collection_stack") as start,
            patch.object(gc, "stop_collection_stacks") as cleanup,
        ):
            code = gc.main(["sample", "--task", "Galaxis", "--skip-device-setup", "--no-open"])
        self.assertEqual(code, 1)
        start.assert_not_called()
        cleanup.assert_not_called()


class PicoListenerReadinessTest(unittest.TestCase):
    @staticmethod
    def diagnostics(name, listening, error=""):
        return (
            f"status:\n- level: '0'\n  name: {name}\n  message: listener state\n  values:\n"
            f"  - key: listening\n    value: '{listening}'\n"
            f"  - key: listen_error\n    value: '{error}'\n---\n"
        )

    def test_latest_matching_bridge_snapshot_wins_over_earlier_bind_failure(self):
        output = (
            self.diagnostics("pico_bridge", "False", "[Errno 98] Address already in use")
            + self.diagnostics("pico_bridge", "True")
            + self.diagnostics("pico_bridge_other", "False", "unrelated error")
        )
        with patch.object(gc.subprocess, "run", return_value=SimpleNamespace(stdout=output)):
            status = gc.read_pico_bridge_diagnostics()
        self.assertEqual(status["listening"], "True")
        self.assertFalse(status.get("listen_error"))

    def test_latest_failure_is_not_hidden_by_earlier_success(self):
        output = self.diagnostics("pico_bridge", "True") + self.diagnostics(
            "pico_bridge", "False", "[Errno 98] Address already in use"
        )
        with patch.object(gc.subprocess, "run", return_value=SimpleNamespace(stdout=output)):
            status = gc.read_pico_bridge_diagnostics()
        self.assertEqual(status["listening"], "False")
        self.assertIn("[Errno 98]", status["listen_error"])

    def test_transient_port_conflict_waits_for_bridge_retry(self):
        now = [0.0]
        with (
            patch.object(gc.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(gc.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)),
            patch.object(gc, "read_pico_bridge_diagnostics", side_effect=[
                {"listening": "False", "listen_error": "[Errno 98] Address already in use"},
                {},  # Other diagnostic publishers may fill one polling window.
                {"listening": "True"},
            ]) as snapshots,
            patch.object(gc, "port_listener_detail") as owner,
        ):
            gc.wait_for_pico_bridge_listener(timeout=2.0)
        self.assertEqual(snapshots.call_count, 3)
        owner.assert_not_called()

    def test_persistent_conflict_exhausts_timeout_and_identifies_owner(self):
        now = [0.0]
        with (
            patch.object(gc.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(gc.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)),
            patch.object(gc, "read_pico_bridge_diagnostics", return_value={
                "listening": "False", "listen_error": "[Errno 98] Address already in use",
            }),
            patch.object(gc, "port_listener_detail", return_value="RoboticsService PID 1234") as owner,
        ):
            with self.assertRaisesRegex(gc.CollectionError, r"Errno 98.*RoboticsService PID 1234"):
                gc.wait_for_pico_bridge_listener(timeout=1.0)
        self.assertGreaterEqual(now[0], 1.0)
        owner.assert_called_once_with(63901)

    def test_non_conflict_errors_fail_immediately_instead_of_retrying(self):
        with (
            patch.object(gc, "read_pico_bridge_diagnostics", return_value={
                "listening": "False", "listen_error": "[Errno 13] Permission denied",
            }) as snapshots,
            patch.object(gc, "port_listener_detail", return_value=""),
            patch.object(gc.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(gc.CollectionError, "Errno 13"):
                gc.wait_for_pico_bridge_listener(timeout=20.0)
        self.assertEqual(snapshots.call_count, 1)
        sleep.assert_not_called()


class LegacyBridgePathTest(unittest.TestCase):
    def test_env_selected_packaged_bridge_is_matched_by_exact_resolved_executable(self):
        for variable in ("TELEOP_XR_SERVICE", "TELEOP_XR_LAUNCHER"):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                installed = root / "installed runtime/xr"
                installed.mkdir(parents=True)
                (installed / "runService.sh").touch()
                alias = root / "user-workspace"
                alias.symlink_to(installed, target_is_directory=True)
                proc = root / "proc"
                proc.mkdir()
                targets = {
                    11001: str(installed / "RoboticsServiceProcess"),
                    11002: str(installed / "RoboticsServiceProcess") + " (deleted)",
                    11003: str(installed / "RoboticsServiceProcess-other"),
                    11004: str(root / "unrelated/RoboticsServiceProcess"),
                    11005: str(gc.LEGACY_ROBOTICS_SERVICE),
                    gc.os.getpid(): str(installed / "RoboticsServiceProcess"),
                }
                for pid, executable in targets.items():
                    entry = proc / str(pid)
                    entry.mkdir()
                    (entry / "exe").symlink_to(executable)
                with patch.dict(gc.os.environ, {
                    "TELEOP_XR_SERVICE": "", "TELEOP_XR_LAUNCHER": "",
                    variable: str(alias / "runService.sh"),
                }), patch.object(gc.os, "kill", side_effect=AssertionError("read-only lookup")):
                    self.assertEqual(gc.legacy_robotics_service_pids(proc), [11001, 11002, 11005])

    def test_service_variable_takes_priority_over_legacy_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pid, folder in ((11001, "selected"), (11002, "old")):
                entry = root / str(pid)
                entry.mkdir()
                (entry / "exe").symlink_to(root / folder / "RoboticsServiceProcess")
            with patch.dict(gc.os.environ, {
                "TELEOP_XR_SERVICE": str(root / "selected/runService.sh"),
                "TELEOP_XR_LAUNCHER": str(root / "old/runService.sh"),
            }), patch.object(gc.os, "kill", side_effect=AssertionError("read-only lookup")):
                self.assertEqual(gc.legacy_robotics_service_pids(root), [11001])


class FlowHelpersTest(unittest.TestCase):
    def test_start_vr_wakes_after_bridge_listener_without_relaunch(self):
        with (
            patch.object(gc, "discover_pico_serial", return_value="PICO123"),
            patch.object(gc, "terminate_legacy_robotics_service"),
            patch.object(gc, "wait_for_vr_streaming", side_effect=[False, True]),
            patch.object(gc, "wait_for_pico_bridge_listener") as listener,
            patch.object(gc, "run_checked") as tunnel,
            patch.object(gc.subprocess, "run") as run,
        ):
            assert gc.start_vr() == "PICO123"

        listener.assert_called_once_with(timeout=20.0)
        tunnel.assert_called_once_with([str(gc.PICO_ADB_SCRIPT), "PICO123"])
        self.assertEqual(
            run.call_args.args[0],
            ["adb", "-s", "PICO123", "shell", "input", "keyevent", "KEYCODE_WAKEUP"],
        )

    def test_create_episode_local_returns_expected_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(gc, "TASK_STORE_PATH", Path(tmp) / "tasks.yaml"):
                gc.create_task(
                    name="Galaxis", description="", deadline=None, robot_type=""
                )
                episode, task, robot = gc.create_episode_for_task(
                    task_identifier="Galaxis", robot_identifier="Galaxis Portable"
                )
        self.assertEqual(episode["status"], "created")
        self.assertEqual(task["name"], "Galaxis")
        self.assertEqual(robot["id"], "galaxis-portable")

    def test_open_monitor_returns_monitor_url(self):
        with patch.object(gc.subprocess, "Popen", return_value=None), \
             patch.object(gc, "shutil_which", return_value="/usr/bin/xdg-open"):
            self.assertEqual(gc.open_monitor(), gc.MONITOR_URL)
        self.assertEqual(gc.MONITOR_URL, "http://localhost:8770/")


if __name__ == "__main__":
    unittest.main()


class BackgroundWorkerLauncherTest(unittest.TestCase):
    def test_cpu_budget_adapts_to_other_computers_and_affinity_limits(self):
        for count, expected in ((1, 1), (2, 1), (4, 2), (8, 4), (24, 6)):
            with self.subTest(cpus=count), patch.object(gc.os, 'sched_getaffinity', return_value=set(range(count))):
                self.assertEqual(gc.finalizer_cpu_budget(), expected)
        with patch.object(gc.os, 'sched_getaffinity', side_effect=OSError), patch.object(gc.os, 'cpu_count', return_value=None):
            self.assertEqual(gc.finalizer_cpu_budget(), 1)

    def test_worker_is_owned_offline_persistent_and_uses_configured_paths(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / 'portable-app'; data = Path(td) / 'other-user-data'
            source = project / 'galaxis-core/galaxis-core/galaxis_core'; source.mkdir(parents=True)
            for name in ('mp4_worker.py', 'video_bundle.py', 'recording_priority.py', 'camera_quality.py'):
                (source / name).write_text('# fixture')
            (project / 'umi-runtime.json').write_text(json.dumps({'image': 'offline-test:1'}))
            with patch.object(gc, 'PROJECT_DIR', project), patch.object(gc, 'DATA_DIR', data), \
                 patch.object(gc, '_inspect_finalizer', return_value=None), \
                 patch.object(gc, '_finalizer_require_running'), \
                 patch.object(gc, '_finalizer_docker', return_value=SimpleNamespace(returncode=0, stdout='a' * 64)) as run:
                gc.ensure_background_finalizer()
            command = ['docker', *run.call_args.args]
            assert command[:3] == ['docker', 'run', '--detach']
            assert command[command.index('--network') + 1] == 'none'
            assert command[command.index('--restart') + 1] == 'unless-stopped'
            assert str(data) + ':/opt/data' in command
            assert str(source.parent) + ':/processing-src:ro' in command
            assert '--privileged' not in command and '--device' not in command
            assert 'offline-test:1' in command

    def test_worker_never_replaces_unowned_container(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            project = Path(td); source = project / 'galaxis-core/galaxis-core/galaxis_core'
            source.mkdir(parents=True)
            for name in ('mp4_worker.py', 'video_bundle.py', 'recording_priority.py', 'camera_quality.py'):
                (source / name).write_text('# fixture')
            old = SimpleNamespace(returncode=0, stdout=json.dumps([{'Config': {'Labels': {}}}]))
            with patch.object(gc, 'PROJECT_DIR', project), patch.object(gc, 'DATA_DIR', project / 'data'), \
                 patch.object(gc.subprocess, 'run', return_value=old), patch.object(gc, 'run_checked') as run:
                with self.assertRaises(gc.CollectionError):
                    gc.ensure_background_finalizer()
                run.assert_not_called()
