"""Presence-driven node registry for galaxis_bringup launch files.

Each entry maps a `galaxis_devices.yaml` top-level key to a Node factory.
Launch files load common/<variant>/local galaxis_devices.yaml, collect the set
of present keys, and spawn Nodes only for registry entries whose `yaml_key`
appears in that keyset.

There are two registries — kept aligned with the two launch files in this
package — so each launch only spawns its own concern:

- BRINGUP_NODE_REGISTRY     ← galaxis_bringup.launch.py
    Hardware/IO nodes brought up regardless of whether data collection runs.
    (USB cameras, head camera, PICO bridge, encoder.)

- DATA_COLLECTION_NODE_REGISTRY ← galaxis_data_collection.launch.py
    Nodes specific to data-collection runs (footpedal, dispatch).

Adding a new hardware node:
  1. Append an entry to the appropriate registry with the matching `yaml_key`.
  2. Add the YAML section to common/<variant>/galaxis_devices.yaml.

Structural nodes (robot_state_publisher, rosbridge_websocket) are launched
unconditionally by the launch files themselves; they are listed in
STRUCTURAL_NODE_KEYS so the test_node_registry consistency test ignores them.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable

import yaml

# ROS 2 launch / launch_ros are imported lazily inside the factories so this
# module is importable host-side (for pytest) without a ROS 2 environment.


# Top-level keys that the consistency test should NOT require to appear in the
# registry. These nodes are spawned by launch-file code outside the YAML-driven
# path (URDF-only, fixed-port services, dev-only tooling).
STRUCTURAL_NODE_KEYS = frozenset(
    {
        # No YAML keys for these in galaxis_devices.yaml today.
    }
)


def camera_remappings(namespace: str):
    """Keep every center transport private until orientation is corrected.

    ``usb_cam`` creates image-transport topics independently.  Remapping only
    the raw base topic is not sufficient on all ROS/image_transport versions:
    its compressed publisher can otherwise remain on the public topic where it
    races the corrected publisher and produces alternating upside-down frames.
    """
    if namespace != "center_camera":
        return []
    return [
        ("image_raw", "image_raw_unflipped"),
        ("image_raw/compressed", "image_raw_unflipped/compressed"),
        ("image_raw/compressedDepth", "image_raw_unflipped/compressedDepth"),
        ("image_raw/theora", "image_raw_unflipped/theora"),
        ("camera_info", "camera_info_unflipped"),
    ]


def _factory_usb_cam(namespace: str):
    def factory(params):
        from launch_ros.actions import Node  # ROS 2 runtime-only

        return Node(
            package="galaxis_bringup",
            executable="camera_supervisor",
            arguments=["--camera-role", namespace.removesuffix("_camera")],
            namespace=namespace,
            name="usb_cam",
            output="screen",
            respawn=True,
            respawn_delay=2.0,
            parameters=params,
            remappings=camera_remappings(namespace),
        )

    return factory


def with_center_camera_settings(
    params, saved_setting: Path = Path("/etc/galaxis/center_camera.yaml")
):
    """Append persisted camera-control/center-flip parameters after defaults.

    The legacy filename is deliberately retained so existing Setup selections
    continue to apply without a migration.  ROS selects only the section whose
    node name matches the launched controller.
    """
    resolved = list(params)
    if saved_setting.exists():
        resolved.append(str(saved_setting))
    return resolved


def _factory_camera_control(side: str):
    def factory(params):
        from launch_ros.actions import Node

        return Node(
            package="galaxis_bringup",
            executable="camera_control_node",
            name=f"{side}_camera_brightness_controller",
            output="screen",
            parameters=with_center_camera_settings(params),
        )

    return factory


def _factory_center_camera_flip(params):
    from launch_ros.actions import Node

    # The registry key is an executable name, but the launched ROS node is
    # center_camera_flip. ROS silently ignores the former YAML section.
    resolved = []
    for source in params:
        resolved.append(source)
        if isinstance(source, (str, Path)):
            document = yaml.safe_load(Path(source).read_text()) or {}
            legacy = document.get("center_camera_flip_node", {}).get("ros__parameters")
            current = document.get("center_camera_flip", {}).get("ros__parameters", {})
            if isinstance(legacy, dict):
                resolved.append({**legacy, **current})
    return Node(
        package="galaxis_bringup",
        executable="center_camera_flip_node",
        name="center_camera_flip",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        parameters=with_center_camera_settings(resolved),
    )


def _factory_g2_camera_bridge(params):
    from launch_ros.actions import Node

    return Node(
        package="galaxis_bringup",
        executable="g2_camera_bridge_node",
        name="g2_camera_bridge",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        parameters=params,
    )


def _factory_pico_bridge(params):
    from launch_ros.actions import Node

    return Node(
        package="galaxis_pico_bridge",
        executable="pico_bridge_node",
        name="pico_bridge_node",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        parameters=params,
    )


def _encoder_file_parameters(path: Path) -> dict:
    """Read parameters that apply to the encoder node from a ROS YAML file."""
    try:
        with path.open(encoding="utf-8") as source:
            document = yaml.safe_load(source)
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Cannot read encoder parameters from {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"Invalid encoder parameter file: {path}")

    parameters = {}
    for node_name in ("/**", "encoder_node", "/encoder_node"):
        if node_name not in document:
            continue
        section = document[node_name]
        values = section.get("ros__parameters") if isinstance(section, dict) else None
        if not isinstance(values, dict):
            raise ValueError(f"Invalid {node_name} parameters in {path}")
        parameters.update(values)
    return parameters


def with_encoder_calibration(
    params, calibration: Path = Path("/etc/galaxis/encoder_limits.yaml")
):
    """Require calibrated minima for the sides selected by the current profile.

    Only offsets are loaded from the saved calibration.  Its historical
    active_sides must not override a newly selected collection profile.
    """
    encoder_params = list(params)
    current = {}
    for source in encoder_params:
        if isinstance(source, dict):
            current.update(source)
        else:
            current.update(_encoder_file_parameters(Path(source)))
    active_sides = current.get("active_sides", ["left", "right"])
    if (
        not isinstance(active_sides, (list, tuple))
        or not active_sides
        or any(side not in ("left", "right") for side in active_sides)
        or len(set(active_sides)) != len(active_sides)
    ):
        raise ValueError(f"Invalid encoder active_sides: {active_sides!r}")

    if not calibration.is_file():
        raise RuntimeError(
            f"Encoder calibration is required: {calibration}. "
            "Calibrate the minimum angle before starting collection."
        )
    saved = _encoder_file_parameters(calibration)
    offsets = {}
    for side in active_sides:
        key = f"{side}_min_raw"
        value = saved.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Missing or invalid {key} in {calibration}; recalibrate the minimum angle")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"Non-finite {key} in {calibration}; recalibrate the minimum angle")
        offsets[key] = value
    encoder_params.append(offsets)
    return encoder_params


def _factory_encoder(params):
    from launch_ros.actions import Node

    return Node(
        package="galaxis_bringup",
        executable="encoder_node",
        name="encoder_node",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        parameters=with_encoder_calibration(params),
    )


def _factory_realsense(params):
    from launch_ros.actions import Node

    return Node(
        package="realsense2_camera",
        executable="realsense2_camera_node",
        namespace="camera",
        name="camera",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        parameters=params,
    )


def _factory_footpedal(_params):
    from launch_ros.actions import Node

    # footpedal_node takes no parameters; presence-driven via a marker YAML
    # entry (`footpedal_node: { ros__parameters: {} }`) in the variant overlay.
    return Node(
        package="footpedal_ros",
        executable="footpedal_node",
        name="footpedal_node",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
    )


def _factory_task_command_dispatch(params):
    from launch.substitutions import LaunchConfiguration
    from launch_ros.actions import Node

    return Node(
        package="galaxis_core",
        executable="task_command_dispatch_node",
        name="task_command_dispatch_node",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        parameters=params,
        remappings=[("/joy", LaunchConfiguration("joy_remap_topic"))],
    )


def _factory_gripper_double_click(params):
    from launch_ros.actions import Node

    return Node(
        package="galaxis_bringup",
        executable="gripper_double_click_node",
        name="gripper_double_click_node",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        parameters=params,
    )


def _factory_portable_joy_command(params):
    from launch_ros.actions import Node

    return Node(
        package="galaxis_bringup",
        executable="portable_joy_command_node",
        name="portable_joy_command_node",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        parameters=params,
    )


BRINGUP_NODE_REGISTRY = [
    {"yaml_key": "left_camera/usb_cam", "factory": _factory_usb_cam("left_camera")},
    {"yaml_key": "right_camera/usb_cam", "factory": _factory_usb_cam("right_camera")},
    {"yaml_key": "center_camera/usb_cam", "factory": _factory_usb_cam("center_camera")},
    {"yaml_key": "center_camera/g2_bridge", "factory": _factory_g2_camera_bridge},
    {
        "yaml_key": "left_camera_brightness_controller",
        "factory": _factory_camera_control("left"),
    },
    {
        "yaml_key": "right_camera_brightness_controller",
        "factory": _factory_camera_control("right"),
    },
    {
        "yaml_key": "center_camera_brightness_controller",
        "factory": _factory_camera_control("center"),
    },
    {"yaml_key": "center_camera_flip_node", "factory": _factory_center_camera_flip},
    {"yaml_key": "/camera/camera", "factory": _factory_realsense},
    {"yaml_key": "pico_bridge_node", "factory": _factory_pico_bridge},
    {"yaml_key": "encoder_node", "factory": _factory_encoder},
]


DATA_COLLECTION_NODE_REGISTRY = [
    {"yaml_key": "footpedal_node", "factory": _factory_footpedal},
    {
        "yaml_key": "task_command_dispatch_node",
        "factory": _factory_task_command_dispatch,
    },
    {
        "yaml_key": "gripper_double_click_node",
        "factory": _factory_gripper_double_click,
    },
    {
        "yaml_key": "portable_joy_command_node",
        "factory": _factory_portable_joy_command,
    },
]


# Combined view for consistency tests.
NODE_REGISTRY = BRINGUP_NODE_REGISTRY + DATA_COLLECTION_NODE_REGISTRY


def collect_yaml_keys(yaml_paths: Iterable[Path | str]) -> set[str]:
    """Return the union of top-level keys from given YAML files. Missing files are skipped."""
    keys: set[str] = set()
    for path in yaml_paths:
        p = Path(path)
        if not p.exists():
            continue
        with p.open("r") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            keys.update(data.keys())
    return keys


def select_nodes(
    registry: list[dict],
    yaml_keys_present: set[str],
    params: list,
) -> list:
    """Instantiate Node objects for `registry` entries whose yaml_key is present.

    Return type is list[launch_ros.actions.Node] but unannotated to keep this
    module importable host-side without ROS 2 launch installed.
    """
    return [
        entry["factory"](params)
        for entry in registry
        if entry["yaml_key"] in yaml_keys_present
    ]
