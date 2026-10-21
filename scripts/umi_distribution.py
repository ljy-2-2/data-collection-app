"""Package-only UMI configuration: prebuilt runtime, no downloads at capture time."""
from pathlib import Path
import json
import shutil

import yaml


def prepare_umi_distribution(package: Path, resources: Path):
    manifest = json.loads((resources / "manifest.json").read_text())
    runtime = package / "umi-runtime"
    runtime.mkdir()
    shutil.copy2(resources / manifest["archive"], runtime / manifest["archive"])
    shutil.copy2(resources / "manifest.json", runtime / "manifest.json")
    project = package / "app/umi_collection/vendor/galaxis"
    (project / "umi-runtime.json").write_text(json.dumps(manifest, indent=2))
    # Source .env files are deliberately excluded from packages. Compose still
    # requires this file; provide portable defaults without copying host secrets.
    (project / ".env").write_text(
        "# Packaged UMI defaults; no account or host-specific credentials.\n"
        "ROS_DISTRO=jazzy\n"
        "ROS_DOMAIN_ID=0\n"
        "RMW_IMPLEMENTATION=rmw_cyclonedds_cpp\n"
        "ROBOT_VARIANT=portable\n"
        "WEB_VIDEO_SERVER_PORT=9091\n"
        "SENTRY_DSN=\n"
    )
    configure_umi_runtime(project, manifest)


def configure_umi_runtime(project: Path, manifest: dict):
    """Apply runtime bindings after copying source, including incremental releases."""
    path = project / "docker-compose.yml"
    compose = yaml.safe_load(path.read_text())
    services = compose["services"]
    for name in list(services):
        if name not in {"config-init", "galaxis", "galaxis_core", "web_video_server", "galaxis_monitor"}:
            del services[name]
            continue
        service = services[name]
        service["image"] = manifest["image"]
        service["pull_policy"] = "never"
        service.pop("build", None)
        service["volumes"] = [v for v in service.get("volumes", []) if ":/root/ros2_ws/src/" not in v and not v.startswith("./monitor:")]
    services["config-init"]["entrypoint"] = ["python3"]
    services["config-init"]["command"] = [
        "/work/galaxis_bringup/tools/build_runtime_configs.py", "--single-config",
        "/work/galaxis_bringup/config/galaxis_config.yaml", "--profile-file", "/etc/galaxis/collection_profile.yaml",
    ]
    services["galaxis_core"]["command"] = ["ros2", "launch", "galaxis_core", "leader_teleop.launch.py"]
    volumes = services["galaxis_core"].setdefault("volumes", [])
    if '/etc/galaxis:/etc/galaxis:ro' not in volumes:
        volumes.append('/etc/galaxis:/etc/galaxis:ro')
    # Runtime images use symlink-installed Python modules. Bind reviewed patches
    # at the real source paths, otherwise a deb source update is ignored by ROS.
    # Bind the whole Python package, including new recovery modules and their
    # callers. Updating just the gate left the image's destructive stop handler
    # active even though the desktop workspace contained the new implementation.
    volumes.append("./galaxis-core/galaxis-core/galaxis_core:"
                   "/root/ros2_ws/src/galaxis-core/galaxis_core:ro")
    for name in ('encoder_protocol.py', 'encoder_node.py'):
        services["galaxis"].setdefault("volumes", []).append(
            f"./galaxis_bringup/galaxis_bringup/{name}:"
            f"/root/ros2_ws/src/galaxis_bringup/galaxis_bringup/{name}:ro")
    for name in ("pico_bridge_node.py", "transport/galaxis_tcp.py"):
        services["galaxis"].setdefault("volumes", []).append(
            f"./galaxis_pico/galaxis_pico_bridge/galaxis_pico_bridge/{name}:"
            f"/root/ros2_ws/src/galaxis_pico/galaxis_pico_bridge/galaxis_pico_bridge/{name}:ro")
    monitor_volumes = services["galaxis_monitor"].setdefault("volumes", [])
    monitor_binding = ("./monitor/bridge/galaxis_studio_bridge/ros_state.py:"
                       "/app/monitor/bridge/galaxis_studio_bridge/ros_state.py:ro")
    if monitor_binding not in monitor_volumes:
        monitor_volumes.append(monitor_binding)

    services["galaxis_monitor"]["working_dir"] = "/app/monitor/bridge"
    services["galaxis_monitor"]["command"] = ["python3", "-m", "galaxis_studio_bridge", "--host", "127.0.0.1", "--port", "8770"]
    path.write_text(yaml.safe_dump(compose, allow_unicode=True, sort_keys=False))
