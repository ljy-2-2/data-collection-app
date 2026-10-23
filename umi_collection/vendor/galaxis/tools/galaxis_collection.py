#!/usr/bin/env python3
"""Command-line task creation and Episode preparation for Galaxis collection."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


PROJECT_DIR = Path(__file__).resolve().parents[1]
ROOT_COMPOSE = PROJECT_DIR / "docker-compose.yml"
MONITOR_SCRIPT = PROJECT_DIR / "tools" / "start_collection_monitor.sh"
DEVICE_SETUP_SCRIPT = PROJECT_DIR / "tools" / "galaxis_udev_setup.sh"
PICO_ADB_SCRIPT = PROJECT_DIR / "tools" / "galaxis_pico_adb.sh"
LEGACY_ROBOTICS_SERVICE = Path("/opt/apps/roboticsservice/RoboticsServiceProcess")
DATA_DIR = Path(os.environ.get("DATA_MOUNT_PATH", "~/galaxis_data")).expanduser()
TASK_STORE_PATH = DATA_DIR / "tasks.yaml"
MONITOR_URL = "http://localhost:8770/"
DEFAULT_ROBOT = "Galaxis Portable"
COLLECTION_PROFILE_FILE = Path("/etc/galaxis/collection_profile.yaml")
G2_CAMERA_STREAM_SCRIPT = PROJECT_DIR / "tools" / "g2_camera_stream.py"
G2_CAMERA_STREAM_PID_FILE = Path("/tmp/galaxis_g2_camera_stream.pid")
G2_CAMERA_STREAM_LOG_FILE = Path("/tmp/galaxis_g2_camera_stream.log")
G2_CAMERA_STREAM_PORT = 64002
DEFAULT_COLLECTION_PROFILE = {
    "collection_profile": "dual",
    "active_cameras": ["left", "right", "center"],
    "active_arms": ["left", "right"],
    "camera_sources": {"center": "usb"},
}
RECORD_FORMATS = ("rosbag", "hdf5")


class CollectionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 本地任务库
#
# 采集流程不再依赖任何外部管理平台:任务定义保存在采集数据目录的
# tasks.yaml 中,由采集栈的离线任务模式加载,行为与在线后端完全一致
# (创建任务、按任务创建 Episode、自动重复回合)。
# ---------------------------------------------------------------------------

def load_task_store(path: Path | None = None) -> dict[str, Any]:
    """读取本地任务库;不存在时返回带默认机器人身份的骨架。"""
    path = path or TASK_STORE_PATH
    store: dict[str, Any]
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            raise CollectionError(f"读取本地任务库失败: {exc}") from exc
        if not isinstance(loaded, dict):
            raise CollectionError(f"本地任务库格式无效: {path}")
        store = loaded
    else:
        store = {}
    store.setdefault("robot", {"id": "galaxis-portable", "name": DEFAULT_ROBOT})
    store.setdefault(
        "user", {"id": "galaxis-operator", "display_name": "Operator"}
    )
    store.setdefault("tasks", [])
    return store


def save_task_store(store: dict[str, Any], path: Path | None = None) -> None:
    path = path or TASK_STORE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(store, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def task_list(*, approved_only: bool = False) -> list[dict[str, Any]]:
    del approved_only  # 本地任务库无需审批流程
    return list(load_task_store().get("tasks", []))


def resolve_task(identifier: str) -> dict[str, Any]:
    tasks = task_list()
    by_id = [task for task in tasks if task.get("id") == identifier]
    if by_id:
        return by_id[0]
    by_name = [task for task in tasks if task.get("name") == identifier]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        ids = ", ".join(str(task.get("id")) for task in by_name)
        raise CollectionError(
            f"Task name {identifier!r} is ambiguous. Use one of these IDs: {ids}"
        )
    available = ", ".join(
        f"{task.get('name')} ({task.get('id')})" for task in tasks
    )
    raise CollectionError(
        f"Task {identifier!r} not found. "
        f"Available: {available or '(none)'}"
    )


def create_task(
    *,
    name: str,
    description: str,
    deadline: str | None,
    robot_type: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    store = load_task_store()
    for task in store["tasks"]:
        if task.get("name") == name or task.get("id") == name:
            raise CollectionError(f"任务 {name!r} 已存在(id={task.get('id')})")
    task = {
        "id": name,
        "name": name,
        "description": description or "",
        "robot_type": robot_type or "",
        "deadline": parse_deadline(deadline),
        "subtasks": [
            {
                "id": f"{name}-subtask-1",
                "name": "采集回合",
                "order_index": 0,
            }
        ],
    }
    store["tasks"].append(task)
    save_task_store(store)
    version = {"id": f"{name}-version-1"}
    return task, version


def ensure_offline_task(task: dict[str, Any]) -> None:
    """把目标任务写入任务库并置顶(离线后端从 tasks[0] 创建 Episode)。

    任务已存在时用本次传入的字段覆盖(尤其 round_index 跨会话累积),
    其余旧字段保留。"""
    store = load_task_store()
    tasks = [t for t in store["tasks"] if t.get("id") == task["id"]]
    if tasks:
        merged = dict(tasks[0])
        merged.update(task)
        tasks = [merged]
    else:
        tasks = [task]
    store["tasks"] = tasks + [
        t for t in store["tasks"] if t.get("id") != task["id"]
    ]
    save_task_store(store)


def sanitize_task_name(name: str) -> str:
    """把任务名规整为可作为目录名的安全形式。"""
    cleaned = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", name.strip())
    return cleaned.strip("_") or "task"


def ensure_task_category(identifier: str) -> dict[str, Any]:
    """sample 的 --task 只是任务大类:不存在时自动创建,不再报错。"""
    try:
        return resolve_task(identifier)
    except CollectionError:
        sanitized = sanitize_task_name(identifier)
        # The CLI accepts human-readable names, while newly created local task
        # IDs/names are filesystem-safe.  On a later run, resolve the saved
        # sanitized task before attempting to create it again.
        if sanitized != identifier:
            try:
                return resolve_task(sanitized)
            except CollectionError:
                pass
        task, _version = create_task(
            name=sanitized,
            description="由 sample 自动创建的任务大类",
            deadline=None,
            robot_type="galaxis_portable",
        )
        print(f"任务大类不存在,已自动创建: {task['name']}")
        return task


def recording_base_dir(record_format: str) -> Path:
    """Return the host directory used by the selected recording format."""
    if record_format == "rosbag":
        return DATA_DIR / "rosbags"
    if record_format == "hdf5":
        return DATA_DIR / "hdf5"
    raise CollectionError(f"不支持的数据格式: {record_format}")


def require_storage_mount() -> None:
    """Never create an external recording directory on the underlying root disk."""
    path = DATA_DIR.resolve()
    if path.parts[1:2] not in (("media",), ("mnt",), ("run",)):
        return
    if path.parts[1:2] == ('run',) and path.parts[2:3] != ('media',):
        raise CollectionError(f'不能将临时内存文件系统作为采集盘：{path}')
    # /run paths other than the desktop's removable-media mount are not a
    # supported persistent data destination either.
    for parent in (path, *path.parents):
        if parent == Path('/') or len(parent.parts) < 3:
            break
        if parent.is_mount():
            return
    raise CollectionError(f"采集磁盘未挂载：{path}。请先挂载外接盘，不会改写到系统盘。")


def ensure_recording_base_dir(record_format: str) -> Path:
    """Create the host output directory with the operator's ownership."""
    require_storage_mount()
    path = recording_base_dir(record_format)
    operator_uid = int(os.environ.get("SUDO_UID", os.getuid()))
    operator_gid = int(os.environ.get("SUDO_GID", os.getgid()))
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, operator_uid, operator_gid)
        os.chmod(path, 0o755)
    except OSError as exc:
        raise CollectionError(
            f"无法准备采集目录 {path}: {exc}。"
            "请确认磁盘可写且该目录属于当前操作员。"
        ) from exc
    return path


def next_round_index(task_name: str, record_format: str = "rosbag") -> int:
    """扫描该任务目录下已有回合,返回下一个可用回合序号(从 1 开始累积)。"""
    task_dir = recording_base_dir(record_format) / task_name
    max_index = 0
    if task_dir.is_dir():
        pattern = re.compile(rf"^{re.escape(task_name)}_(\d+)_.*")
        for entry in task_dir.iterdir():
            if not entry.is_dir():
                continue
            match = pattern.match(entry.name)
            if match:
                try:
                    max_index = max(max_index, int(match.group(1)))
                except ValueError:
                    continue
    return max_index + 1


def apply_offline_runtime(record_format: str = "rosbag") -> None:
    """把离线运行参数写入统一配置文件,并重新生成运行时配置。

    任务文件在容器内位于 /opt/data/tasks.yaml(对应宿主数据目录)。
    """
    single = PROJECT_DIR / "galaxis_bringup" / "config" / "galaxis_config.yaml"
    try:
        cfg = yaml.safe_load(single.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        cfg = {}
    if not isinstance(cfg, dict):
        raise CollectionError(f"统一配置文件格式无效: {single}")
    robot_section = cfg.setdefault("robot_config", {})
    if not isinstance(robot_section, dict):
        raise CollectionError("galaxis_config.yaml 的 robot_config 段格式无效")
    node = robot_section.setdefault("/**", {})
    if not isinstance(node, dict):
        raise CollectionError("robot_config 的 /** 段格式无效")
    params = node.setdefault("ros__parameters", {})
    if not isinstance(params, dict):
        raise CollectionError("robot_config 的 ros__parameters 段格式无效")
    params["offline_mode"] = True
    params["task_file"] = "/opt/data/tasks.yaml"
    if record_format not in RECORD_FORMATS:
        raise CollectionError(f"不支持的数据格式: {record_format}")
    params["record_format"] = record_format
    params['compressed_mcap_enabled'] = record_format == 'rosbag'
    if record_format == 'rosbag':
        params['video_mp4_enabled'] = True  # Retain the durable capture/recovery journal.
    params["collection_gate_file"] = ("/opt/data/.collection-platform.json"
                                      if os.environ.get("DATA_COLLECTION_GATE_FILE") else "")
    params["record_base_dir"] = (
        "/opt/data/rosbags" if record_format == "rosbag" else "/opt/data/hdf5"
    )
    # 保留策略:历史回合文件永不删除、永不清理
    upload_section = cfg.setdefault("upload_targets", {})
    if not isinstance(upload_section, dict):
        raise CollectionError("galaxis_config.yaml 的 upload_targets 段格式无效")
    upload_section["delete_after_upload"] = False
    upload_section["local_retention_hours"] = 0
    single.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # Generate the mounted runtime files once on the host. The following
    # ensure_collection_stack() starts ROS exactly once; starting galaxis and
    # galaxis_core here used to recreate both containers again seconds later.
    run_checked([
        sys.executable,
        str(PROJECT_DIR / "galaxis_bringup" / "tools" / "build_runtime_configs.py"),
        "--single-config",
        str(single),
        "--profile-file",
        str(COLLECTION_PROFILE_FILE),
    ])
    ensure_collection_images()
    if record_format == "rosbag":
        ensure_background_finalizer()


def ensure_collection_images() -> None:
    """Build the local ROS images when this checkout has not been bootstrapped."""
    manifest = PROJECT_DIR / "umi-runtime.json"
    if manifest.is_file():
        expected = json.loads(manifest.read_text())
        result = subprocess.run(
            ["docker", "image", "inspect", "--format={{.Id}}", expected["image"]],
            capture_output=True, text=True, check=False,
        )
        if result.returncode or result.stdout.strip() != expected["image_id"]:
            raise CollectionError("UMI 运行环境未正确安装，请重新安装完整 App 安装包")
        return
    result = subprocess.run(
        ["docker", "image", "inspect", "galaxis-core:latest", "galaxis:latest"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        return
    print("本地采集镜像未就绪，正在首次构建…")
    run_checked(compose_command(ROOT_COMPOSE, "build", "galaxis_core_builder"))
    run_checked(compose_command(ROOT_COMPOSE, "build", "galaxis"))


def finalizer_cpu_budget() -> int:
    # Idle finalization may use every available CPU. The worker restricts its
    # affinity while monitoring, and CapturePriority preempts the whole process
    # group before recording. A static half-CPU quota also throttled idle work.
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available = os.cpu_count() or 1
    return max(1, available)


_FINALIZER_LABEL = "cn.xiaokai.umi.finalizer"
_FINALIZER_COMMAND = ["-m", "galaxis_core.mp4_worker", "--root", "/opt/data/rosbags"]


def _finalizer_docker(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        timeout = 60 if args and args[0] in {"run", "start"} else 30
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CollectionError("后台处理 Docker 操作超时；未强制删除容器，原始采集文件及待处理回合已保留") from exc
    except OSError as exc:
        raise CollectionError(f"后台处理服务无法执行 Docker：{exc}") from exc


def _finalizer_error(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or f"Docker 退出码 {result.returncode}").strip()[-3000:]


def _inspect_finalizer(identity: str, *, allow_missing: bool = False) -> dict[str, Any] | None:
    result = _finalizer_docker("inspect", identity)
    if result.returncode:
        reason = _finalizer_error(result)
        if allow_missing and any(marker in reason.lower() for marker in ("no such object", "no such container")):
            return None
        raise CollectionError(f"读取后台处理容器状态失败：{reason}")
    try:
        records = json.loads(result.stdout)
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError("unexpected inspect result")
        return records[0]
    except (ValueError, TypeError) as exc:
        raise CollectionError("Docker 返回了无效的后台处理容器状态；未修改容器") from exc


def _validate_finalizer(container: dict[str, Any], root: Path) -> str:
    """Prove ownership before starting, stopping or removing any container."""
    config = container.get("Config") or {}
    labels = config.get("Labels") or {}
    host = container.get("HostConfig") or {}
    mounts = container.get("Mounts") or []
    identity = str(container.get("Id", ""))
    version = str(labels.get(_FINALIZER_LABEL + ".version", ""))
    version_parts = version.rsplit(":", 2)
    data = next((mount for mount in mounts if mount.get("Destination") == "/opt/data"), {})
    source = next((mount for mount in mounts if mount.get("Destination") == "/processing-src"), {})
    source_path = Path(str(source.get("Source", "")))
    owned = (
        re.fullmatch(r"[a-f0-9]{64}", identity)
        and labels.get(_FINALIZER_LABEL) == str(root)
        and len(version_parts) == 3 and config.get("Image") == version_parts[0]
        and re.fullmatch(r"[a-f0-9]{64}", version_parts[1])
        and re.fullmatch(r"cpu[1-9][0-9]*", version_parts[2])
        and config.get("Entrypoint") == ["python3"]
        and config.get("Cmd") == _FINALIZER_COMMAND
        and host.get("NetworkMode") == "none" and not host.get("Privileged")
        and len(mounts) == 2
        and data.get("Type") == "bind" and data.get("RW") is True
        and Path(str(data.get("Source", ""))).resolve() == root
        and source.get("Type") == "bind" and source.get("RW") is False
        and source_path.is_absolute() and source_path.parts[-2:] == ("galaxis-core", "galaxis-core")
    )
    if not owned:
        raise CollectionError("后台处理容器名称冲突或配置异常，无法确认归属；未修改容器或采集数据")
    return identity


def _finalizer_running(container: dict[str, Any]) -> bool:
    state = container.get("State") or {}
    return bool(state.get("Running") and not state.get("Paused") and not state.get("Restarting"))


def _finalizer_require_running(identity: str, root: Path) -> None:
    # Detached docker run can succeed even if Python immediately fails.
    time.sleep(1.0)
    container = _inspect_finalizer(identity)
    if _validate_finalizer(container, root) != identity:
        raise CollectionError("后台处理容器身份已变化；请重新检测")
    if _finalizer_running(container):
        return
    state = container.get("State") or {}
    logs = _finalizer_docker("logs", "--tail", "20", identity)
    detail = (logs.stderr or "") + (logs.stdout or "")
    raise CollectionError(
        f"后台处理服务未正常运行（状态 {state.get('Status', 'unknown')}，"
        f"退出码 {state.get('ExitCode', 'unknown')}）：{state.get('Error') or ''}\n"
        f"{detail.strip()[-3000:]}\n原始采集文件及待处理回合已保留"
    )


def _remove_stopped_finalizer(identity: str, root: Path) -> None:
    # Re-read by immutable ID. Docker rm without -f is the final race guard.
    container = _inspect_finalizer(identity)
    if _validate_finalizer(container, root) != identity:
        raise CollectionError("后台处理容器身份已变化；未删除容器")
    state = container.get("State") or {}
    if (state.get("Running") is not False or state.get("Restarting") or state.get("Paused")
            or state.get("Status") not in {"created", "exited", "dead"}):
        raise CollectionError("后台处理容器正在运行或状态变化中；未删除容器，请稍后重试")
    result = _finalizer_docker("rm", identity)
    if result.returncode:
        raise CollectionError(f"清理失效后台处理容器失败：{_finalizer_error(result)}；采集数据未删除")


def _can_recreate_finalizer(reason: str) -> bool:
    # Permissions, a busy runtime or an outage cannot be fixed by replacement.
    # Other startup faults on a verified stopped worker can be retried with
    # fresh metadata and identical durable data mounts.
    lower = reason.lower()
    return not any(marker in lower for marker in (
        "permission denied", "operation not permitted", "access denied", "unauthorized",
        "cannot connect", "is the docker daemon running", "connection refused", "connection reset",
        "context deadline", "timed out", "timeout", "busy", "already running", "in progress",
        "no space left", "out of space", "disk quota exceeded",
    ))


def ensure_background_finalizer() -> None:
    """Own one offline worker per data root, outside the camera/ROS lifecycle."""
    require_storage_mount()
    root = DATA_DIR.resolve()
    (root / "rosbags").mkdir(parents=True, exist_ok=True)
    manifest = PROJECT_DIR / "umi-runtime.json"
    image = json.loads(manifest.read_text())["image"] if manifest.is_file() else "galaxis-core:latest"
    source = (PROJECT_DIR / "galaxis-core/galaxis-core").resolve()
    # A source digest restarts only this worker on software updates. Its queue
    # lives in the mounted data folder, so interrupted work is recoverable.
    digest = hashlib.sha256()
    for name in ("mp4_worker.py", "video_bundle.py", "recording_priority.py", "camera_quality.py", "capture_recovery.py", "capture_guard.py", "mcap_video.py", "compressed_recorder.py", "deferred_raw.py", "live_encoder.py"):
        digest.update((source / "galaxis_core" / name).read_bytes())
    cpu_budget = finalizer_cpu_budget()
    version = image + ":" + digest.hexdigest() + f":cpu{cpu_budget}"
    name = "xiaokai-umi-finalizer-" + hashlib.sha256(str(root).encode()).hexdigest()[:12]
    label = _FINALIZER_LABEL
    container = _inspect_finalizer(name, allow_missing=True)
    startup_error = ""
    if container is not None:
        identity = _validate_finalizer(container, root)
        labels = container["Config"]["Labels"]
        mounted_source = next(mount["Source"] for mount in container["Mounts"]
                              if mount["Destination"] == "/processing-src")
        current = labels[label + ".version"] == version and Path(mounted_source).resolve() == source
        if current and _finalizer_running(container):
            return
        state = container.get("State") or {}
        if state.get("Paused") or state.get("Restarting") or state.get("Status") == "removing":
            raise CollectionError("后台处理容器正忙或正在重启；未修改容器，请稍后重试")
        if current:
            started = _finalizer_docker("start", identity)
            if started.returncode == 0:
                _finalizer_require_running(identity, root)
                return
            startup_error = _finalizer_error(started)
            if not _can_recreate_finalizer(startup_error):
                raise CollectionError(f"启动后台处理服务失败：{startup_error}；原容器和采集数据已保留")
        elif state.get("Running"):
            # SIGTERM preserves original bags and pending jobs during updates.
            stopped = _finalizer_docker("stop", "--time", "10", identity)
            if stopped.returncode:
                raise CollectionError(f"更新后台处理服务失败：{_finalizer_error(stopped)}；未删除容器")
        _remove_stopped_finalizer(identity, root)
    devices = []
    if (source / "galaxis_core/native/mpp-jpeg-h264").is_file():
        for device in ("/dev/dri", "/dev/mpp_service"):
            if not Path(device).exists():
                raise CollectionError(f"板载压缩设备不可用：{device}；暂存数据保留")
            devices += ["--device", device]
    command = [
        "docker", "run", "--detach", "--name", name,
        "--label", label + "=" + str(root), "--label", label + ".version=" + version,
        "--restart", "unless-stopped", "--network", "none", "--cpus", str(cpu_budget),
        "--volume", str(root) + ":/opt/data",
        "--volume", str(source) + ":/processing-src:ro",
        "--env", "PYTHONPATH=/processing-src",
        "--env", "GALAXIS_DATA_UID=" + os.environ.get("SUDO_UID", str(os.getuid())),
        "--env", "GALAXIS_DATA_GID=" + os.environ.get("SUDO_GID", str(os.getgid())),
        *devices, "--entrypoint", "python3", image,
        *_FINALIZER_COMMAND,
    ]
    result = _finalizer_docker(*command[1:])
    if result.returncode:
        previous = f"原启动错误：{startup_error}\n" if startup_error else ""
        raise CollectionError(f"{previous}创建后台处理服务失败：{_finalizer_error(result)}；原始采集文件及待处理回合已保留")
    created = result.stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{64}", created):
        raise CollectionError("Docker 未返回有效后台处理容器 ID；请重新检测，原始采集文件已保留")
    _finalizer_require_running(created, root)


def create_episode_for_task(
    *,
    task_identifier: str,
    robot_identifier: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    del robot_identifier  # 离线模式下机器人身份由任务库定义
    task = resolve_task(task_identifier)
    robot = {"id": "galaxis-portable", "name": DEFAULT_ROBOT}
    episode = {"id": "auto-episode", "status": "created"}
    return episode, task, robot


def read_collection_profile(path: Path = COLLECTION_PROFILE_FILE) -> dict[str, Any]:
    """Read the small setup YAML without requiring PyYAML on the host CLI."""
    profile = dict(DEFAULT_COLLECTION_PROFILE)
    try:
        profile_text = path.read_text(encoding="utf-8")
        for raw_line in profile_text.splitlines():
            if raw_line[:1].isspace():
                continue
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, raw_value = (part.strip() for part in line.split(":", 1))
            if raw_value.startswith("[") and raw_value.endswith("]"):
                profile[key] = [
                    item.strip() for item in raw_value[1:-1].split(",") if item.strip()
                ]
            else:
                profile[key] = raw_value
        source_match = re.search(
            r"^camera_sources:\s*$[\s\S]*?^\s{2}center:\s*['\"]?([^'\"\s#]+)",
            profile_text,
            re.MULTILINE,
        )
        if source_match:
            profile["camera_sources"] = {"center": source_match.group(1)}
    except OSError:
        pass
    expected = {
        "dual": (["left", "right", "center"], ["left", "right"]),
        "single-left": (["left", "center"], ["left"]),
        "single-right": (["right", "center"], ["right"]),
    }
    name = profile.get("collection_profile")
    if name not in expected or (
        profile.get("active_cameras"), profile.get("active_arms")
    ) != expected[name]:
        raise CollectionError(f"无效的采集配置文件: {path}")
    return profile


def collection_profile_available(path: Path = COLLECTION_PROFILE_FILE) -> bool:
    """Return whether Setup has produced a valid reusable hardware profile."""
    if not path.is_file():
        return False
    try:
        read_collection_profile(path)
    except CollectionError:
        return False
    return True


def center_camera_source(profile: dict[str, Any]) -> str:
    sources = profile.get("camera_sources")
    if not isinstance(sources, dict):
        return "usb"
    source = sources.get("center", "usb")
    if source not in {"usb", "g2_head_color"}:
        raise CollectionError(f"不支持的中心相机来源: {source!r}")
    return str(source)


def _g2_stream_pid() -> int | None:
    try:
        pid = int(G2_CAMERA_STREAM_PID_FILE.read_text(encoding="utf-8").strip())
        command_line = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    expected = os.fsencode(str(G2_CAMERA_STREAM_SCRIPT))
    return pid if expected in command_line.split(b"\0") else None


def stop_g2_camera_stream() -> None:
    pid = _g2_stream_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise CollectionError(f"无权结束 G2 中心相机进程 {pid}") from exc
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _g2_stream_pid() == pid:
            time.sleep(0.05)
        if _g2_stream_pid() == pid:
            os.kill(pid, signal.SIGKILL)
    try:
        G2_CAMERA_STREAM_PID_FILE.unlink()
    except FileNotFoundError:
        pass


def start_g2_camera_stream(timeout: float = 25.0) -> None:
    """Start the host CPython 3.10 GDK reader and wait for a fresh G2 frame."""
    stop_g2_camera_stream()
    python = Path("/usr/bin/python3.10")
    if not python.exists():
        raise CollectionError("G2 中心相机需要 /usr/bin/python3.10，但当前主机不存在")
    if not G2_CAMERA_STREAM_SCRIPT.exists():
        raise CollectionError(f"G2 中心相机程序不存在: {G2_CAMERA_STREAM_SCRIPT}")
    log_file = G2_CAMERA_STREAM_LOG_FILE.open("w", encoding="utf-8")
    process_env = os.environ.copy()
    g2_native_lib = PROJECT_DIR.parent / "app" / "lib"
    process_env["LD_LIBRARY_PATH"] = ":".join(
        part
        for part in (str(g2_native_lib), process_env.get("LD_LIBRARY_PATH", ""))
        if part
    )
    process = subprocess.Popen(
        [str(python), str(G2_CAMERA_STREAM_SCRIPT), "--port", str(G2_CAMERA_STREAM_PORT)],
        cwd=PROJECT_DIR,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
        env=process_env,
    )
    log_file.close()
    G2_CAMERA_STREAM_PID_FILE.write_text(str(process.pid), encoding="utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = G2_CAMERA_STREAM_LOG_FILE.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
            stop_g2_camera_stream()
            raise CollectionError(
                "G2 中心相机无法启动"
                + (f": {detail.splitlines()[-1]}" if detail else "")
            )
        try:
            with socket.create_connection(
                ("127.0.0.1", G2_CAMERA_STREAM_PORT), timeout=0.2
            ):
                print("G2 中心相机已连接：GDK 输入 640x400，ROS 输出 640x480@30")
                return
        except OSError:
            time.sleep(0.1)
    stop_g2_camera_stream()
    raise CollectionError(
        "G2 中心相机在启动超时内没有提供实时帧；请确认 G2 已开机、"
        "10.42.1.x 网络正常且 head_color 正在发布。"
    )


def run_checked(
    command: list[str],
    *,
    quiet: bool = False,
    env: dict[str, str] | None = None,
) -> None:
    command_env = os.environ.copy()
    # Compose services run as root for device access, but recordings must be
    # returned to the operator who started this command.  Honour sudo's
    # original identity when this wrapper itself was invoked with sudo.
    command_env["GALAXIS_DATA_UID"] = os.environ.get("SUDO_UID", str(os.getuid()))
    command_env["GALAXIS_DATA_GID"] = os.environ.get("SUDO_GID", str(os.getgid()))
    if env:
        command_env.update(env)
    result = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        env=command_env,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.PIPE if quiet else None,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (getattr(result, "stderr", "") or "").strip()[-3000:]
        raise CollectionError(f"Command failed ({result.returncode}): {' '.join(command)}"
                              + (f"\n{detail}" if detail else ""))


def compose_command(compose_file: Path, *arguments: str) -> list[str]:
    """Build a Docker Compose command without changing its invocation semantics."""
    return ["docker", "compose", "-f", str(compose_file), *arguments]


def legacy_robotics_service_pids(proc_root: Path = Path("/proc")) -> list[int]:
    """Find only the retired PC bridge by its exact executable path."""
    executables = {str(LEGACY_ROBOTICS_SERVICE)}
    configured = os.environ.get("TELEOP_XR_SERVICE") or os.environ.get("TELEOP_XR_LAUNCHER")
    if configured:
        executables.add(str(Path(configured).expanduser().resolve().parent / "RoboticsServiceProcess"))
    matches: list[int] = []
    try:
        entries = proc_root.iterdir()
    except OSError:
        return matches
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            executable = os.readlink(entry / "exe")
        except OSError:
            continue
        if executable.removesuffix(" (deleted)") in executables:
            pid = int(entry.name)
            if pid != os.getpid():
                matches.append(pid)
    return sorted(matches)


def terminate_legacy_robotics_service(
    *, timeout: float = 3.0, poll_interval: float = 0.05
) -> list[int]:
    """Stop the retired bridge so it cannot retain Galaxis TCP port 63901."""
    pids = legacy_robotics_service_pids()
    if not pids:
        return []
    print(
        "正在结束占用 PICO 端口的旧 RoboticsService: "
        + ", ".join(str(pid) for pid in pids)
    )
    permission_errors: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            permission_errors.append(pid)
    if permission_errors:
        raise CollectionError(
            "无权结束旧 RoboticsService 进程: "
            + ", ".join(str(pid) for pid in permission_errors)
            + "。请以原用户运行采集程序，或先用 sudo 结束该精确进程。"
        )

    deadline = time.monotonic() + max(timeout, 0.0)
    remaining = set(pids) & set(legacy_robotics_service_pids())
    while remaining and time.monotonic() < deadline:
        time.sleep(max(poll_interval, 0.01))
        remaining &= set(legacy_robotics_service_pids())
    for pid in sorted(remaining):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise CollectionError(
                f"旧 RoboticsService 进程 {pid} 在 SIGTERM 后仍未退出，且无权强制结束。"
            ) from exc
    return pids


def ensure_collection_stack() -> None:
    run_checked([str(MONITOR_SCRIPT), "--no-open"])


def disconnect_pico(serial: str | None) -> None:
    """Disconnect PICO and remove the retired PC bridge if it survived."""
    if serial:
        result = subprocess.run(
            ["adb", "-s", serial, "reverse", "--remove", "tcp:63901"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        # Removal is idempotent. A missing/offline device or an already absent
        # mapping is also disconnected, so neither case should fail teardown.
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if detail:
                print(f"PICO tunnel 已断开（ADB: {detail}）", file=sys.stderr)
    terminate_legacy_robotics_service()


def stop_collection_stacks(pico_serial: str | None = None) -> None:
    """Stop project services and disconnect PICO, retaining apps and data."""
    failures: list[str] = []
    for name, compose_file in (
        ("ROS 采集与监看服务", ROOT_COMPOSE),
    ):
        try:
            running = subprocess.run(
                compose_command(compose_file, "ps", "-q", "--status", "running"),
                capture_output=True, text=True, timeout=5, check=False,
            )
            if running.returncode != 0:
                raise CollectionError(running.stderr.strip() or "无法读取采集容器")
            container_ids = running.stdout.split()
            if any(not re.fullmatch(r"[a-f0-9]{12,64}", value) for value in container_ids):
                raise CollectionError("采集容器标识无效")
            if container_ids:
                run_checked(["docker", "stop", "--time", "1", *container_ids], quiet=True)
        except (CollectionError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{name}: {exc}")
    disconnect_pico(pico_serial)
    try:
        stop_g2_camera_stream()
    except CollectionError as exc:
        failures.append(f"G2 中心相机: {exc}")
    if failures:
        raise CollectionError("；".join(failures))


def refresh_collection_runtime() -> None:
    """Build current sources, regenerate config, and reload ROS services."""
    print("正在构建当前 ROS 源码（配置、URDF 与节点代码）…")
    # Both images contain ROS install trees.  Building them from the current
    # checkout, rather than compiling in an ephemeral container, makes every
    # saved source/configuration edit survive the following service recreate.
    # Docker layer caching keeps this incremental when nothing changed.
    # `galaxis_core_builder` uses the repository root as its build context, which
    # includes galaxis_pico as well as galaxis-core.  Build it before `galaxis`, whose
    # Dockerfile is based on the resulting galaxis-core image.
    run_checked(compose_command(ROOT_COMPOSE, "build", "galaxis_core_builder"))
    run_checked(compose_command(ROOT_COMPOSE, "build", "galaxis"))

    print("正在同步运行时配置（本地采集数据永久保留）…")
    # config-init is an intentional one-shot Compose service.  A previously
    # completed instance must be removed before creating the next one; Docker
    # Compose otherwise sometimes tries to rename it and errors with a name
    # conflict.  This removes only that disposable configuration container,
    # never recordings, or runtime services.
    run_checked(
        compose_command(ROOT_COMPOSE, "rm", "-sf", "config-init")
    )
    # This must be synchronous.  `up -d config-init` returns while the YAML is
    # still being generated, allowing galaxis/galaxis_core to start with stale files.
    # `run --rm` makes a configuration failure visible to the caller and gives
    # the following recreate an atomic, fully-written runtime snapshot.
    run_checked(compose_command(ROOT_COMPOSE, "run", "--rm", "config-init"))
    # Parameters are read only when ROS nodes start.  Recreate these local
    # services only after the generator exits so they receive the newly written
    # mappings, calibration, profile, and camera-control settings before a new
    # Episode can be accepted.  --no-deps avoids recreating config-init
    # alongside them, which previously caused container-name conflicts.
    run_checked(
        compose_command(
            ROOT_COMPOSE,
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            "galaxis",
            "galaxis_core",
        )
    )


def recording_is_active() -> bool:
    command = [
        "docker",
        "exec",
        "galaxis_core",
        "bash",
        "-lc",
        (
            "source /root/ros_entrypoint.sh >/dev/null 2>&1 && "
            "timeout 3 ros2 topic echo --once /record_manager/recording"
        ),
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "data: true" in result.stdout.lower()


def run_device_setup(
    pico_serial: str | None = None,
    collection_profile: str = "auto",
) -> dict[str, Any]:
    if recording_is_active():
        raise CollectionError(
            "当前仍在录制。请按左手柄 Y 或右手柄 B 保存并暂停，再重新执行 sample。"
        )
    print("正在打开本机设备设置(Device Setup)…")
    print("先选择双臂 3 相机/2 执行器，或单臂 2 相机/1 执行器；确认画面并 Capture MIN。")
    print("APPLY 会保存校准并自动切换到运行时监看；窗口关闭仅为释放相机/串口，采集流程不会退出。")
    setup_args = [
        str(DEVICE_SETUP_SCRIPT),
        "--variant",
        "portable",
        "--collection-profile",
        collection_profile,
    ]
    if os.geteuid() == 0:
        setup_command = setup_args
    elif sys.stdin.isatty():
        setup_command = ["sudo", "-E", *setup_args]
    elif shutil_which("pkexec"):
        # Web-launched collection has no terminal, so sudo cannot ask for a
        # password.  Keep the desktop session variables required by both the
        # graphical polkit prompt and the Tk device-selection window.
        desktop_values = {
            name: os.environ.get(name, "")
            for name in (
                "DISPLAY",
                "XAUTHORITY",
                "WAYLAND_DISPLAY",
                "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS",
            )
        }
        runtime_dir = desktop_values["XDG_RUNTIME_DIR"]
        if not desktop_values["DISPLAY"]:
            desktop_values["DISPLAY"] = ":0"
        if runtime_dir and not desktop_values["XAUTHORITY"]:
            xauth_candidates = sorted(
                Path(runtime_dir).glob(".mutter-Xwaylandauth.*"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if xauth_candidates:
                desktop_values["XAUTHORITY"] = str(xauth_candidates[0])
        desktop_env = [
            f"{name}={value}"
            for name, value in desktop_values.items()
            if value
        ]
        setup_command = [
            "pkexec",
            "env",
            *desktop_env,
            f"GALAXIS_PICO_SERIAL={pico_serial or ''}",
            *setup_args,
        ]
    else:
        raise CollectionError(
            "网页启动设备设置需要 pkexec 图形授权；请安装 policykit-1"
        )
    try:
        run_checked(
            setup_command,
            env={"GALAXIS_PICO_SERIAL": pico_serial or ""},
        )
    except (CollectionError, KeyboardInterrupt) as setup_error:
        print("设备设置未完成，正在恢复采集服务…", file=sys.stderr)
        try:
            run_checked(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(ROOT_COMPOSE),
                    "up",
                    "-d",
                    "galaxis",
                ],
                quiet=True,
            )
        except CollectionError as restore_error:
            raise CollectionError(
                f"{setup_error}; 采集服务自动恢复也失败: {restore_error}"
            ) from setup_error
        if isinstance(setup_error, KeyboardInterrupt):
            raise CollectionError("设备设置已被中断，采集服务已经恢复") from setup_error
        raise
    print("设备分配和闭合位置已保存。接下来将启动 ROS 与 Galaxis Monitor。")
    return read_collection_profile()


def discover_pico_serial(requested_serial: str | None = None) -> str:
    if not shutil_which("adb"):
        raise CollectionError("找不到 adb；请先安装 Android platform-tools")
    result = subprocess.run(
        ["adb", "devices"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CollectionError(f"adb devices 失败: {result.stderr.strip()}")
    devices: dict[str, str] = {}
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and not fields[0].startswith("emulator-"):
            devices[fields[0]] = fields[1]
    if requested_serial:
        state = devices.get(requested_serial)
        if state != "device":
            raise CollectionError(
                f"PICO {requested_serial} 未授权或未连接（状态: {state or 'missing'}）"
            )
        return requested_serial
    authorized = [serial for serial, state in devices.items() if state == "device"]
    if len(authorized) == 1:
        return authorized[0]
    if not authorized:
        raise CollectionError(
            "没有已授权的 PICO。请解锁头显、接受 USB 调试，然后运行 adb devices -l"
        )
    raise CollectionError(
        "检测到多台 PICO，请使用 --pico-serial 指定序列号: "
        + ", ".join(authorized)
    )


def start_vr(requested_serial: str | None = None) -> str:
    serial = discover_pico_serial(requested_serial)
    terminate_legacy_robotics_service()
    # Reuse a healthy sender immediately. No collection path may terminate or
    # launch Unity because doing so resets its Send Data toggle.
    if wait_for_vr_streaming(timeout=1.0):
        print(f"PICO VR 已在持续输出，复用现有连接: {serial}")
        return serial

    print(f"正在连接已打开的 PICO VR: {serial}")
    wait_for_pico_bridge_listener(timeout=20.0)
    # Ctrl+C intentionally removes this mapping, so rebuild it immediately
    # instead of spending twelve seconds waiting for an impossible reconnect.
    run_checked([str(PICO_ADB_SCRIPT), serial])
    # Preflight may have opened Galaxis VR before ROS owned the USB devices.
    # Wake the headset again after the listener exists; never relaunch a
    # running Unity app, which would reset its Head/Controllers/Send Data UI.
    subprocess.run(
        ["adb", "-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"],
        capture_output=True, text=True, timeout=5.0, check=False,
    )
    if wait_for_vr_streaming(timeout=10.0):
        print("PICO VR 实时数据流已连接")
        return serial

    # A connected bridge with a working heartbeat is not a crashed sender.
    # Restarting Unity in this state is harmful: its Teleoperation toggles may
    # reset to off, and repeated restarts used to accumulate stale send threads.
    if vr_bridge_connected():
        raise CollectionError(
            "PICO VR TCP 连接和心跳正常，但未收到追踪数据。"
            "请在头显 Galaxis Teleoperation 页面开启 Head、Controllers 和 Send Data；"
            "程序不会重启健康的 Unity 连接。"
        )

    raise CollectionError(
        "已重建 ADB tunnel，但 ROS bridge 仍未收到 PICO 实时数据。"
        "程序不会重启或退出头显里的 Galaxis VR；请确认该应用已打开，"
        "Head、Controllers 与 Send Data 保持开启。"
    )


def recover_vr_if_needed(serial: str) -> bool:
    """Recover a lost paused-session stream without touching a healthy app."""
    if wait_for_vr_streaming(timeout=1.0):
        return True
    print("PICO VR 推流中断，正在恢复 tunnel（不会重启 VR 应用）…")
    try:
        start_vr(serial)
    except CollectionError as exc:
        print(f"警告：PICO VR 自动恢复失败：{exc}", file=sys.stderr)
        return False
    return True


def port_listener_detail(port: int) -> str:
    """Return the ss line for a host listener, including its process when visible."""
    result = subprocess.run(
        ["ss", "-lntp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    matches = [line.strip() for line in result.stdout.splitlines() if f":{port}" in line]
    return " | ".join(matches)


def read_pico_bridge_diagnostics() -> dict[str, str]:
    """Read the latest diagnostic snapshot from the ROS bridge itself."""
    # Use the monitor's persistent ROS subscription. Repeatedly starting the
    # ROS CLI on ARM can spend the entire two-second window in DDS discovery.
    # Missing/stale diagnostics in a new monitor are authoritative; only old
    # monitors without this field fall back to the CLI.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(MONITOR_URL + "api/state", timeout=1.0) as response:
            snapshot = json.load(response)
        if isinstance(snapshot, dict) and "pico_bridge" in snapshot:
            status = snapshot["pico_bridge"]
            return status if isinstance(status, dict) else {}
    except (OSError, ValueError):
        pass
    command = [
        "docker",
        "exec",
        "galaxis",
        "bash",
        "-lc",
        (
            "source /root/ros_entrypoint.sh >/dev/null 2>&1 && "
            # Several nodes publish separate DiagnosticArray messages. Wait
            # through the short window so the result cannot accidentally be
            # only recording_gate's first message and miss pico_bridge.
            "timeout 2 ros2 topic echo /diagnostics"
        ),
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = result.stdout
    snapshots = list(re.finditer(
        r"^\s*name:\s*['\"]?pico_bridge['\"]?\s*$", output, re.M
    ))
    if not snapshots:
        return {}
    # A bind retry can succeed inside this two-second window. An earlier
    # EADDRINUSE snapshot must not override the bridge's recovered state.
    section = re.split(
        r"^\s*(?:---\s*$|name:)", output[snapshots[-1].end():], maxsplit=1, flags=re.M
    )[0]
    status: dict[str, str] = {}
    message = re.search(r"^\s*message:\s*['\"]?(.*?)['\"]?\s*$", section, re.M)
    if message:
        status["message"] = message.group(1)
    for key, value in re.findall(
        r"- key:\s*([^\n]+)\n\s*value:\s*['\"]?([^'\"\n]+)", section
    ):
        status[key.strip()] = value.strip()
    return status


def wait_for_pico_bridge_listener(timeout: float) -> None:
    """Require the Galaxis ROS bridge—not merely any process—to own port 63901."""
    deadline = time.monotonic() + timeout
    last_status: dict[str, str] = {}
    last_listen_error = ""
    while time.monotonic() < deadline:
        last_status = read_pico_bridge_diagnostics()
        if last_status.get("listening") == "True":
            return
        listen_error = last_status.get("listen_error", "")
        if "listening" not in last_status and not last_listen_error:
            legacy_owner = port_listener_detail(63901)
            if "RoboticsService" in legacy_owner:
                raise CollectionError(
                    "PICO bridge 无法监听 tcp:63901：旧 RoboticsService 正在占用端口。"
                    f"占用详情：{legacy_owner}。请先退出该旧服务。"
                )
        if listen_error:
            last_listen_error = listen_error
            # The bridge retries bind every second. Let a recently stopped
            # owner's socket finish closing before declaring a port conflict.
            if "[Errno 98]" not in listen_error:
                break
        time.sleep(0.25)
    if last_listen_error:
        owner = port_listener_detail(63901)
        hint = (
            " 请先退出旧的 RoboticsService。"
            if "RoboticsService" in owner
            else " 请停止占用该端口的旧服务。"
        )
        raise CollectionError(
            "PICO bridge 无法监听 tcp:63901："
            f"{last_listen_error}。占用详情：{owner or '无法识别进程。'}。{hint}"
        )
    raise CollectionError(
        f"PICO bridge 在 {timeout:g} 秒内没有报告监听就绪。"
        f"最后状态：{last_status or '没有 pico_bridge diagnostics'}"
    )


def wait_for_vr_streaming(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_pico_bridge_diagnostics().get("message") == "streaming":
            return True
        time.sleep(0.25)
    return False


def prepare_live_devices(requested_serial: str | None, *, skip_vr: bool,
                         wait_for_devices: bool) -> str | None:
    """Keep App monitoring alive while recoverable device readiness settles.

    No Episode is created until both VR and the existing recording gate pass.
    Ctrl+C still follows main's normal resource cleanup path.
    """
    serial = None
    while True:
        try:
            if not skip_vr:
                serial = start_vr(requested_serial)
            wait_for_recording_gate_ready()
            return serial
        except CollectionError as exc:
            if not wait_for_devices:
                raise
            print(f"等待设备恢复：{exc} 监视保持运行，暂不可录制。", flush=True)
            time.sleep(3.0)


def vr_bridge_connected() -> bool:
    """Return whether the PICO bridge has a live TCP client/heartbeat."""
    return read_pico_bridge_diagnostics().get("connected") == "True"


def parse_deadline(value: str | None) -> str:
    if not value:
        deadline = datetime.now().astimezone() + timedelta(days=365)
    else:
        normalized = value.strip().replace(" ", "T")
        try:
            deadline = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise CollectionError(
                "Invalid deadline. Use YYYY-MM-DDTHH:MM, for example "
                "2026-08-31T18:00"
            ) from exc
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def wait_for_ros_task(task_name: str, timeout: float) -> bool:
    command = [
        "docker",
        "exec",
        "galaxis_core",
        "bash",
        "-lc",
        (
            "source /root/ros_entrypoint.sh >/dev/null 2>&1 && "
            "timeout 3 ros2 topic echo --once /task_sequence_manager/status"
        ),
    ]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if result.returncode == 0 and f"received_task:{task_name}" in result.stdout:
            return True
        time.sleep(1)
    return False


def open_monitor() -> str:
    url = MONITOR_URL
    launchers = (
        ["xdg-open", url],
        ["gio", "open", url],
        ["google-chrome", "--new-window", url],
        ["chromium", "--new-window", url],
        ["firefox", "--new-window", url],
    )
    for command in launchers:
        if not shutil_which(command[0]):
            continue
        try:
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print("已自动打开 Galaxis Monitor 采集监看窗口")
            break
        except OSError:
            continue
    return url


def shutil_which(command: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def recording_directories(record_format: str = "rosbag") -> set[Path]:
    base_dir = recording_base_dir(record_format)
    if not base_dir.exists():
        return set()
    results = {path.parent for path in base_dir.rglob("meta.json")}
    if record_format == "rosbag":
        results.update(path.parent for path in base_dir.rglob("metadata.yaml"))
    else:
        results.update(path.parent for path in base_dir.rglob("*.h5"))
        results.update(
            path.parent.parent
            for path in base_dir.rglob(".rosbag_staging/metadata.yaml")
        )
    return results


def wait_for_new_recording(
    known: set[Path], record_format: str = "rosbag", timeout: float = 8.0
) -> Path | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = recording_directories(record_format) - known
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime)
        time.sleep(0.25)
    return None


def read_monitor_recording_state() -> tuple[str | None, str | None]:
    """Reuse the monitor's ROS subscriptions; never launch DDS nodes in a loop."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(MONITOR_URL + "api/state", timeout=1.0) as response:
            state = json.load(response)
        if not isinstance(state, dict) or state.get("ros_connected") is not True:
            return None, None
        recording = state.get("recording")
        if not isinstance(recording, bool):
            return None, None
        return state.get("task_status"), str(recording).lower()
    except (OSError, ValueError):
        return None, None


def read_topic_once(
    topic: str, timeout: float = 3.0, *, transient_local: bool = False
) -> str | None:
    qos = " --qos-durability transient_local" if transient_local else ""
    command = [
        "docker",
        "exec",
        "galaxis_core",
        "bash",
        "-lc",
        (
            "source /root/ros_entrypoint.sh >/dev/null 2>&1 && "
            f"timeout {timeout:g} ros2 topic echo --once{qos} {topic}"
        ),
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("data:"):
            return stripped.removeprefix("data:").strip().strip("'\"")
    return None


def wait_for_recording_gate_ready(timeout: float = 25.0) -> None:
    """Fail before creating an Episode when the safety gate blocks recording."""
    deadline = time.monotonic() + timeout
    last_value: str | None = None
    while time.monotonic() < deadline:
        value = read_topic_once(
            "/recording_gate/gate_level", timeout=2.0, transient_local=True
        )
        if value == "0":
            print("采集门禁已就绪（gate_level=0）")
            return
        if value is not None:
            last_value = value
        time.sleep(0.5)
    profile = read_collection_profile()
    active_cameras = ", ".join(profile["active_cameras"])
    raise CollectionError(
        "采集门禁未就绪（gate_level="
        f"{last_value or 'unknown'}）。请检查当前 profile 所选相机"
        f"（{active_cameras}）、执行器和 VR 状态；"
        "不会创建无法采样的 Episode。"
    )


def monitor_collection(task_name: str, **kwargs) -> int:
    # Tell the independent encoder that live tracking needs CPU even while
    # waiting between rounds. flock releases on exit/crash without stale flags.
    path = DATA_DIR / 'rosbags/.capture-monitor.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        return _monitor_collection(task_name, **kwargs)


def _monitor_collection(
    task_name: str,
    *,
    record_format: str = "rosbag",
    pico_serial: str | None = None,
    stop_services_on_exit: bool = True,
) -> int:
    print()
    print("实时采样状态已启动；暂停时按 Ctrl+C 停止采集服务并断开 PICO 连接。")
    print("右手柄 A 或左手柄 X = 开始一回合；右手柄 B 或左手柄 Y = 保存并暂停。")
    print(f"数据格式: {record_format}")
    print(f"本地保存目录: {recording_base_dir(record_format)}")

    known_recordings = recording_directories(record_format)
    last_status: str | None = None
    last_recording: bool | None = None
    round_number = 0
    next_vr_health_check = time.monotonic() + 15.0
    try:
        while True:
            status, recording_value = read_monitor_recording_state()
            timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
            if recording_value is None:
                print(
                    f"[{timestamp}] 警告: 暂时未收到录制状态，正在重试…",
                    flush=True,
                )
                time.sleep(1)
                continue
            if status is not None and status != last_status:
                last_status = status
                if "No Task" in status:
                    # 回合间隙状态机会短暂清空任务;终端保持显示当前任务名
                    status = f"received_task:{task_name}"
                print(f"[{timestamp}] ROS 状态: {status}", flush=True)

            is_recording = recording_value.lower() == "true"
            now = time.monotonic()
            if (
                pico_serial
                and not is_recording
                and now >= next_vr_health_check
            ):
                recover_vr_if_needed(pico_serial)
                # Avoid a restart storm when the headset is locked or USB is
                # temporarily unavailable. A healthy stream is never restarted.
                next_vr_health_check = time.monotonic() + 30.0
            if is_recording and last_recording is not True:
                round_number += 1
                print(
                    f"[{timestamp}] 第 {round_number} 回合已开始（recording=true）",
                    flush=True,
                )
            elif not is_recording and last_recording is True:
                print(
                    f"[{timestamp}] 第 {round_number} 回合已结束，录制已暂停",
                    flush=True,
                )
                saved = wait_for_new_recording(known_recordings, record_format)
                if saved is None:
                    print(
                        f"[{timestamp}] 未在 8 秒内发现新 metadata；"
                        "请检查上方 ROS 状态和保存目录",
                        flush=True,
                    )
                else:
                    known_recordings.add(saved)
                    if record_format == "hdf5" and (
                        saved / ".rosbag_staging"
                    ).exists():
                        print(
                            f"[{timestamp}] 回合已暂停并完整落盘；"
                            f"HDF5 正在后台转换: {saved}",
                            flush=True,
                        )
                    else:
                        print(
                            f"[{timestamp}] {record_format} 数据已保存: {saved}",
                            flush=True,
                        )
                # 下一回合由采集栈离线模式的自动重复能力提供,无需外部预创建。
            elif last_recording is None and not is_recording:
                print(f"[{timestamp}] 已就绪，当前暂停（recording=false）")
            last_recording = is_recording
            time.sleep(0.25)
    except KeyboardInterrupt:
        print()
        if last_recording or recording_is_active():
            print("警告：当前仍在录制。请立即按右手柄 B 或左手柄 Y 保存本回合。")
            print(
                "需要重新观察时，可另开终端执行: "
                "docker exec -it galaxis bash -lc "
                "'source /root/ros_entrypoint.sh && "
                "ros2 topic echo /task_sequence_manager/status'"
            )
        else:
            if stop_services_on_exit:
                print("正在停止 ROS、视频监看与 Galaxis Monitor 服务（保留所有数据）…")
                try:
                    stop_collection_stacks(pico_serial)
                except CollectionError as exc:
                    print(f"警告：部分服务未能停止：{exc}", file=sys.stderr)
                    return 1
                print(
                    f"已结束任务“{task_name}”、停止全部采集服务并断开 PICO；"
                    "头显里的 VR 发送程序仍保持运行。"
                )
            else:
                print(
                    f"已退出任务“{task_name}”的终端监看；ROS、相机、Galaxis Monitor "
                    "与 PICO bridge 保持运行，可直接启动下一个任务。"
                )
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create Galaxis tasks and prepare task-specific collection Episodes."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create-task", help="Create a new collection task in the local task store"
    )
    create.add_argument("--name", required=True)
    create.add_argument("--description", default="")
    create.add_argument(
        "--deadline",
        help="Local time, YYYY-MM-DDTHH:MM. Defaults to one year from now.",
    )
    create.add_argument("--robot-type", default="galaxis_portable")

    list_parser = subparsers.add_parser("list-tasks", help="List approved tasks")
    list_parser.add_argument("--all", action="store_true", help="Include draft tasks")

    sample = subparsers.add_parser(
        "sample",
        help="Create the first Episode for a task and open the collection monitor",
    )
    sample.add_argument("--task", required=True, help="Exact task name or task ID")
    sample.add_argument("--robot", default=DEFAULT_ROBOT)
    sample.add_argument("--wait-seconds", type=float, default=20.0)
    sample.add_argument(
        "--format",
        dest="record_format",
        choices=RECORD_FORMATS,
        default="rosbag",
        help="Recording format (default: rosbag)",
    )
    sample.add_argument("--no-open", action="store_true")
    sample.add_argument(
        "--pico-serial",
        default=os.environ.get("PICO_ADB_SERIAL")
        or os.environ.get("GALAXIS_PICO_SERIAL"),
        help="PICO ADB serial; auto-detected when exactly one device is connected",
    )
    sample.add_argument(
        "--device-setup",
        action="store_true",
        help="Open Device Setup and rebuild runtime (only when hardware mapping changes).",
    )
    sample.add_argument(
        "--collection-profile",
        choices=("dual", "single-left", "single-right"),
        default="dual",
        help="Preselect the portable arm/camera profile used by Device Setup.",
    )
    sample.add_argument(
        "--skip-device-setup",
        action="store_true",
        help="Reuse saved Setup (deprecated alias; this is now the normal default).",
    )
    sample.add_argument(
        "--skip-vr",
        action="store_true",
        help="Skip PICO startup/tunnel (diagnostics only)",
    )
    sample.add_argument(
        "--wait-for-devices", action="store_true",
        help="Keep App monitoring alive while VR/camera readiness recovers; recording remains gated",
    )
    sample.add_argument(
        "--detach",
        action="store_true",
        help="Exit after preparation instead of keeping terminal status visible",
    )
    sample.add_argument(
        "--stop-services-on-exit",
        action="store_true",
        help="Deprecated: stopping services and disconnecting PICO is now the default.",
    )
    sample.add_argument(
        "--keep-services-on-exit",
        action="store_true",
        help="Keep collection services and PICO connected after Ctrl+C.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    g2_stream_active = False
    collection_stack_attempted = False
    activity_lock = None
    pico_serial = None
    try:
        if args.command == "create-task":
            task, version = create_task(
                name=args.name,
                description=args.description,
                deadline=args.deadline,
                robot_type=args.robot_type,
            )
            print(f"任务创建成功: {task['name']}")
            print(f"task_id: {task['id']}")
            print(f"task_version_id: {version['id']}")
            print("下一步:")
            print(
                f'  ./tools/galaxis_collection.py sample --task "{task["id"]}"'
            )
            return 0

        if args.command == "list-tasks":
            tasks = task_list(approved_only=not args.all)
            for task in tasks:
                print(f"{task.get('id')}\t{task.get('name')}")
            return 0

        if args.device_setup and args.skip_device_setup:
            raise CollectionError("--device-setup 与 --skip-device-setup 不能同时使用")
        if args.keep_services_on_exit and args.stop_services_on_exit:
            raise CollectionError(
                "--keep-services-on-exit 与 --stop-services-on-exit 不能同时使用"
            )
        ensure_recording_base_dir(args.record_format)
        setup_required = args.device_setup or (
            not args.skip_device_setup and not collection_profile_available()
        )
        if setup_required:
            # Device Setup is deliberately independent from PICO.  It must be
            # possible to calibrate cameras/encoders while the headset is off,
            # locked, or not yet authorized for ADB.
            run_device_setup(collection_profile=args.collection_profile)
            # The calibration GUI owns USB devices, so regenerate configuration
            # and rebuild the complete local runtime after it exits.
            refresh_collection_runtime()
        else:
            print("复用已保存的设备映射和运行时；跳过 Setup 与镜像重建。")
        profile = read_collection_profile()
        if center_camera_source(profile) == "g2_head_color":
            print("正在连接 G2 头部彩色相机…")
            start_g2_camera_stream()
            g2_stream_active = True
        else:
            stop_g2_camera_stream()
        print("正在准备本地任务与离线运行配置…")
        task = ensure_task_category(args.task)
        task["round_index"] = next_round_index(task["id"], args.record_format)
        ensure_offline_task(task)
        apply_offline_runtime(args.record_format)
        print("正在启动 ROS 采集服务与 Galaxis Monitor…")
        # Resolve the legacy process before taking ownership of a stack start.
        # A permission failure here must not stop any existing collection stack.
        terminate_legacy_robotics_service()
        activity_path = DATA_DIR / 'rosbags/.capture-monitor.lock'
        activity_path.parent.mkdir(parents=True, exist_ok=True)
        activity_lock = activity_path.open('a+')
        fcntl.flock(activity_lock, fcntl.LOCK_SH)
        collection_stack_attempted = True
        ensure_collection_stack()
        print("正在建立 PICO VR 连接并检查采集门禁…", flush=True)
        pico_serial = prepare_live_devices(args.pico_serial, skip_vr=args.skip_vr,
                                          wait_for_devices=args.wait_for_devices)
        print("正在等待 ROS 接收任务…")
        episode, task, robot = create_episode_for_task(
            task_identifier=task["id"],
            robot_identifier=args.robot,
        )
        received = wait_for_ros_task(task["name"], args.wait_seconds)
        url = MONITOR_URL if args.no_open else open_monitor()
        print(f"Episode 已创建: {episode['id']}")
        print(f"所属任务: {task['name']} ({task['id']})")
        print(f"数据格式: {args.record_format}")
        print(f"ROS 已接收: {'是' if received else '等待超时，请检查状态'}")
        if pico_serial:
            print(f"PICO VR 已连接: {pico_serial}")
        print(f"监看页面: {url}")
        if not received:
            print(
                "ROS 尚未确认任务：终端将继续监看诊断；请先不要按右手柄 A 或左手柄 X。",
                file=sys.stderr,
            )
            # A detached command is commonly used by automation.  Keep its
            # non-zero failure contract, while interactive use remains open
            # so the operator can see recovery/status messages instead of
            # being told to press A and then immediately losing the monitor.
            if args.detach:
                return 2
        else:
            print("现在按右手柄 A 或左手柄 X 开始；按右手柄 B 或左手柄 Y 保存并暂停。")
        if args.detach:
            return 0
        return monitor_collection(
            task["name"],
            record_format=args.record_format,
            pico_serial=pico_serial,
            stop_services_on_exit=not args.keep_services_on_exit,
        )
    except CollectionError as exc:
        # Startup can fail after ROS has already opened every camera and
        # encoder (for example when PICO is disconnected).  Leaving that stack
        # alive makes the browser preflight appear empty because its preview
        # workers cannot open the same USB devices.  Release those resources
        # before returning the error so "重新检测" can recover immediately.
        if collection_stack_attempted and not args.keep_services_on_exit:
            try:
                stop_collection_stacks(pico_serial)
            except CollectionError as cleanup_error:
                print(f"警告：采集服务清理失败：{cleanup_error}", file=sys.stderr)
        elif g2_stream_active:
            try:
                stop_g2_camera_stream()
            except CollectionError as cleanup_error:
                print(f"警告：G2 中心相机清理失败：{cleanup_error}", file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Cancellation can arrive before monitor_collection() installs its
        # handler. Release cameras and encoders in that startup window too.
        if collection_stack_attempted and not args.keep_services_on_exit:
            try:
                stop_collection_stacks(pico_serial)
            except CollectionError as cleanup_error:
                print(f"警告：采集服务清理失败：{cleanup_error}", file=sys.stderr)
                return 1
        elif g2_stream_active:
            try:
                stop_g2_camera_stream()
            except CollectionError as cleanup_error:
                print(f"警告：G2 中心相机清理失败：{cleanup_error}", file=sys.stderr)
                return 1
        return 0

    finally:
        if activity_lock is not None:
            activity_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
