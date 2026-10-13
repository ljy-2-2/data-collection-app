#!/usr/bin/env python3
"""Start collection; recover only stopped containers with missing Docker layers."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

SERVICES = ("galaxis", "galaxis_core", "web_video_server", "galaxis_monitor")
OWNED_SERVICES = frozenset(("config-init", *SERVICES))
RW_LAYER_ERROR = re.compile(
    r"\brw\s*layer\b[^\r\n]*(?:not found|does not exist|missing|nil)", re.I)


class RecoveryRefused(RuntimeError):
    pass


def cli(args):
    return subprocess.run(args, text=True, capture_output=True, check=False)


def show(result):
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)


def checked(run, args):
    result = run(args)
    if result.returncode:
        show(result)
        raise RecoveryRefused("Docker 状态检查失败，未自动清理容器。")
    return result.stdout


def compatible_config(previous: Path, current: Path) -> bool:
    previous, current = previous.resolve(), current.resolve()
    if previous == current:
        return True
    # Packaged upgrades copy the same project to a new per-user workspace.
    # Compose still selects its old containers by project/service labels. Permit
    # that migration only for this App's layout inside the same user's home.
    suffix = ("umi_collection", "vendor", "galaxis", "docker-compose.yml")
    home = Path.home().resolve()
    return (previous.parts[-4:] == current.parts[-4:] == suffix
            and previous.is_relative_to(home) and current.is_relative_to(home))


def validate(container, identity, project, config):
    if not isinstance(container, dict) or container.get("Id") != identity:
        raise RecoveryRefused("容器 ID 发生变化，未自动清理。")
    labels = (container.get("Config") or {}).get("Labels") or {}
    sources = labels.get("com.docker.compose.project.config_files", "").split(",")
    working = labels.get("com.docker.compose.project.working_dir", "")
    if (labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.service") not in OWNED_SERVICES
            or len(sources) != 1 or not sources[0] or not working):
        raise RecoveryRefused("容器不属于当前采集项目，未自动清理。")
    source = Path(sources[0])
    if (not source.is_absolute() or not Path(working).is_absolute()
            or source.resolve().parent != Path(working).resolve()
            or not compatible_config(source, config)):
        raise RecoveryRefused("容器配置路径不属于当前采集项目，未自动清理。")


def stopped(container):
    state = container.get("State") or {}
    return (state.get("Status") in {"created", "exited", "dead"}
            and not any(state.get(key) for key in ("Running", "Paused", "Restarting")))


def inspect(run, identities):
    records = json.loads(checked(run, ["docker", "inspect", *identities]))
    if not isinstance(records, list) or len(records) != len(identities):
        raise RecoveryRefused("Docker 返回的容器状态不完整，未自动清理。")
    by_id = {record.get("Id"): record for record in records}
    if set(by_id) != set(identities):
        raise RecoveryRefused("Docker 返回的容器 ID 不匹配，未自动清理。")
    return by_id


def broken_layer(run, identity):
    # Unlike State.Error (populated only after a failed start), diff checks the
    # writable layer without starting a stopped ROS/camera service.
    result = run(["docker", "diff", identity])
    if not result.returncode:
        return False
    if RW_LAYER_ERROR.search(result.stderr + result.stdout):
        return True
    show(result)
    raise RecoveryRefused("无法确认容器可写层故障，未自动清理。")


def recover(config, compose, run):
    metadata = json.loads(checked(run, [*compose, "config", "--format", "json"]))
    project = metadata.get("name")
    if not isinstance(project, str) or not project:
        raise RecoveryRefused("无法确认 Compose 项目名称，未自动清理。")
    selected = checked(run, [*compose, "ps", "-a", "-q", *sorted(OWNED_SERVICES)]).split()
    identities = list(dict.fromkeys(selected))
    if not identities or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in identities):
        raise RecoveryRefused("未找到当前项目的完整容器 ID，未自动清理。")
    records = inspect(run, identities)
    failed = []
    # Finish all ownership/error checks before removing even the first one.
    for identity in identities:
        record = records[identity]
        validate(record, identity, project, config)
        if str(record["Config"]["Labels"].get("com.docker.compose.oneoff", "false")).lower() == "true":
            continue
        if stopped(record) and broken_layer(run, identity):
            failed.append(identity)
    if not failed:
        raise RecoveryRefused("未发现可安全恢复的已停止采集容器。")
    for identity in failed:
        record = inspect(run, [identity])[identity]
        validate(record, identity, project, config)
        if not stopped(record):
            raise RecoveryRefused("采集容器已启动，已停止自动恢复。")
        if not broken_layer(run, identity):
            continue
        service = record["Config"]["Labels"]["com.docker.compose.service"]
        print(f"恢复已停止的采集服务 {service}：Docker 可写层丢失；保留采集目录和所有卷。", flush=True)
        # No name lookup, force, or volume deletion. Docker itself also refuses
        # this command if the container starts after the last state check.
        checked(run, ["docker", "rm", identity])


def start(config: Path, run=cli) -> int:
    config = config.resolve()
    compose = ["docker", "compose", "-f", str(config)]
    initial = run([*compose, "up", "-d", *SERVICES])
    show(initial)
    if not initial.returncode:
        return 0
    if not RW_LAYER_ERROR.search(initial.stdout + initial.stderr):
        return initial.returncode
    try:
        recover(config, compose, run)
    except (RecoveryRefused, OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"采集容器恢复未完成：{exc}", file=sys.stderr, flush=True)
        return initial.returncode
    # Exactly one retry, creating only missing containers. Healthy running
    # services are never force-recreated or restarted during this recovery.
    retried = run([*compose, "up", "-d", "--no-recreate", *SERVICES])
    show(retried)
    return retried.returncode


if __name__ == "__main__":
    raise SystemExit(start(Path(sys.argv[1])))
