"""Recover only owned disposable workers; never discard their durable queue."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "umi_collection/vendor/galaxis/tools/galaxis_collection.py"
spec = importlib.util.spec_from_file_location("finalizer_recovery_collection", SOURCE)
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)


@pytest.fixture
def docker(tmp_path, monkeypatch):
    project = tmp_path / "current-workspace"
    source = project / "galaxis-core/galaxis-core"
    (source / "galaxis_core").mkdir(parents=True)
    for name in ("mp4_worker.py", "video_bundle.py", "recording_priority.py", "camera_quality.py", "capture_recovery.py", "capture_guard.py"):
        (source / "galaxis_core" / name).write_bytes(b"fixture")
    data = tmp_path / "operator-data"
    episode = data / "rosbags/task/001"
    episode.mkdir(parents=True)
    (episode / "source.mcap").write_bytes(b"original recording")
    (episode / ".mp4-pending.json").write_text('{"author":"operator"}')
    version = "galaxis-core:latest:" + hashlib.sha256(b"fixture" * 6).hexdigest() + ":cpu2"
    template = {
        "Id": "a" * 64,
        "Config": {"Labels": {gc._FINALIZER_LABEL: str(data), gc._FINALIZER_LABEL + ".version": version},
                   "Image": "galaxis-core:latest", "Entrypoint": ["python3"], "Cmd": gc._FINALIZER_COMMAND},
        "HostConfig": {"NetworkMode": "none", "Privileged": False},
        "State": {"Running": False, "Paused": False, "Restarting": False, "Status": "exited", "ExitCode": 255,
                  "Error": "RWLayer of container is unexpectedly nil"},
        "Mounts": [{"Type": "bind", "Destination": "/opt/data", "Source": str(data), "RW": True},
                   {"Type": "bind", "Destination": "/processing-src", "Source": str(source), "RW": False}],
    }

    class Docker:
        def __init__(self):
            self.container = copy.deepcopy(template)
            self.commands = []
            self.errors = {}
            self.inspect_count = 0
            self.race_on_reinspect = False
            self.exit_after_start = False
            self.exit_after_run = False

        def run(self, command, **kwargs):
            self.commands.append(command)
            operation = command[1]
            if operation in self.errors:
                return subprocess.CompletedProcess(command, 1, "", self.errors[operation])
            if operation == "inspect":
                self.inspect_count += 1
                if self.container is None:
                    return subprocess.CompletedProcess(command, 1, "[]", "Error: No such object: finalizer")
                if self.race_on_reinspect and self.inspect_count > 1:
                    self.container["State"]["Running"] = True
                return subprocess.CompletedProcess(command, 0, json.dumps([self.container]), "")
            if operation == "rm":
                assert command == ["docker", "rm", "a" * 64]
                assert not self.container["State"]["Running"]
                self.container = None
            elif operation == "stop":
                assert command == ["docker", "stop", "--time", "10", "a" * 64]
                self.container["State"]["Running"] = False
            elif operation == "start":
                self.container["State"]["Running"] = not self.exit_after_start
            elif operation == "run":
                self.container = copy.deepcopy(template)
                self.container["Id"] = "b" * 64
                self.container["State"]["Running"] = not self.exit_after_run
                return subprocess.CompletedProcess(command, 0, "b" * 64 + "\n", "")
            elif operation == "logs":
                return subprocess.CompletedProcess(command, 0, "ModuleNotFoundError: example\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        def assert_queue_preserved(self):
            assert (episode / "source.mcap").read_bytes() == b"original recording"
            assert (episode / ".mp4-pending.json").read_text() == '{"author":"operator"}'

    instance = Docker()
    monkeypatch.setattr(gc, "PROJECT_DIR", project)
    monkeypatch.setattr(gc, "DATA_DIR", data)
    monkeypatch.setattr(gc, "finalizer_cpu_budget", lambda: 2)
    monkeypatch.setattr(gc.subprocess, "run", instance.run)
    monkeypatch.setattr(gc.time, "sleep", lambda _: None)
    yield instance
    instance.assert_queue_preserved()


@pytest.mark.parametrize("error", ["RWLayer of container is unexpectedly nil", "OCI runtime create failed: missing runtime state"])
def test_stopped_broken_worker_is_recreated_with_current_config_and_queue(docker, error):
    docker.errors["start"] = error
    gc.ensure_background_finalizer()
    assert [c[1] for c in docker.commands] == ["inspect", "start", "inspect", "rm", "run", "inspect"]
    command = next(c for c in docker.commands if c[1] == "run")
    assert str(gc.DATA_DIR) + ":/opt/data" in command
    assert str(gc.PROJECT_DIR / "galaxis-core/galaxis-core") + ":/processing-src:ro" in command
    assert docker.container["State"]["Running"]


@pytest.mark.parametrize("running", [False, True])
def test_same_digest_old_workspace_is_updated_only_after_stopping(docker, running):
    docker.container["Mounts"][1]["Source"] = "/removed-release/galaxis-core/galaxis-core"
    docker.container["State"]["Running"] = running
    gc.ensure_background_finalizer()
    operations = [c[1] for c in docker.commands]
    assert "start" not in operations
    assert operations == (["inspect", "stop", "inspect", "rm", "run", "inspect"] if running
                          else ["inspect", "inspect", "rm", "run", "inspect"])


@pytest.mark.parametrize("operation,error", [
    ("inspect", "permission denied while trying to connect to Docker"),
    ("start", "permission denied"), ("start", "operation not permitted"),
    ("start", "device or resource busy"), ("start", "Cannot connect to the Docker daemon"),
    ("start", "no space left on device"), ("start", "device out of space"),
])
def test_permissions_and_busy_errors_remain_visible_and_never_remove(docker, operation, error):
    docker.errors[operation] = error
    with pytest.raises(gc.CollectionError, match=error):
        gc.ensure_background_finalizer()
    assert not any(c[1] in {"rm", "run", "stop"} for c in docker.commands)


@pytest.mark.parametrize("changed", ["label", "data", "source_rw", "command", "network", "privileged"])
def test_untrusted_container_config_is_never_mutated(docker, changed):
    if changed == "label":
        docker.container["Config"]["Labels"][gc._FINALIZER_LABEL] = "/another-user"
    elif changed == "data":
        docker.container["Mounts"][0]["Source"] = "/unrelated-data"
    elif changed == "source_rw":
        docker.container["Mounts"][1]["RW"] = True
    elif changed == "command":
        docker.container["Config"]["Cmd"] = ["-m", "another_service"]
    elif changed == "network":
        docker.container["HostConfig"]["NetworkMode"] = "host"
    else:
        docker.container["HostConfig"]["Privileged"] = True
    with pytest.raises(gc.CollectionError, match="无法确认归属"):
        gc.ensure_background_finalizer()
    assert [c[1] for c in docker.commands] == ["inspect"]


def test_concurrent_start_blocks_removal(docker):
    docker.errors["start"] = "RWLayer of container is unexpectedly nil"
    docker.race_on_reinspect = True
    with pytest.raises(gc.CollectionError, match="未删除容器"):
        gc.ensure_background_finalizer()
    assert not any(c[1] in {"rm", "run", "stop"} for c in docker.commands)


def test_recreation_error_contains_original_and_new_error(docker):
    docker.errors.update(start="RWLayer is unexpectedly nil", run="failed to create shim task")
    with pytest.raises(gc.CollectionError) as failure:
        gc.ensure_background_finalizer()
    assert "RWLayer" in str(failure.value) and "failed to create shim task" in str(failure.value)


@pytest.mark.parametrize("new", [False, True])
def test_detached_success_with_dead_worker_reports_state_and_logs(docker, new):
    if new:
        docker.container = None
        docker.exit_after_run = True
    else:
        docker.exit_after_start = True
    with pytest.raises(gc.CollectionError, match="ModuleNotFoundError"):
        gc.ensure_background_finalizer()
    assert not any(c[1] in {"rm", "stop"} for c in docker.commands)


def test_healthy_current_worker_is_reused(docker):
    docker.container["State"]["Running"] = True
    gc.ensure_background_finalizer()
    assert [c[1] for c in docker.commands] == ["inspect"]


def test_failed_stop_does_not_remove_running_worker(docker):
    docker.container["Mounts"][1]["Source"] = "/old-release/galaxis-core/galaxis-core"
    docker.container["State"]["Running"] = True
    docker.errors["stop"] = "device or resource busy"
    with pytest.raises(gc.CollectionError, match="device or resource busy"):
        gc.ensure_background_finalizer()
    assert [c[1] for c in docker.commands] == ["inspect", "stop"]


def test_quiet_command_retains_actual_docker_stderr(docker):
    docker.errors["start"] = "RWLayer of container is unexpectedly nil"
    with pytest.raises(gc.CollectionError, match="RWLayer of container is unexpectedly nil"):
        gc.run_checked(["docker", "start", "a" * 64], quiet=True)


@pytest.mark.parametrize("operation", ["inspect", "start"])
def test_timeout_preserves_container_and_surfaces_real_failure(docker, monkeypatch, operation):
    original = docker.run

    def run(command, **kwargs):
        if command[1] == operation:
            assert kwargs["timeout"] == (60 if operation == "start" else 30)
            docker.commands.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original(command, **kwargs)

    monkeypatch.setattr(gc.subprocess, "run", run)
    with pytest.raises(gc.CollectionError, match="操作超时"):
        gc.ensure_background_finalizer()
    assert not any(c[1] in {"rm", "run", "stop"} for c in docker.commands)
