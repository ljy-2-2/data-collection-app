#!/usr/bin/env python3
"""Data collection launch — bringup + data-collection-specific nodes.

Layers on top of `galaxis_bringup.launch.py`:
- Includes the bringup file (USB cameras, RealSense, PICO bridge, encoder)
- Spawns DATA_COLLECTION_NODE_REGISTRY entries that are present in the merged
  galaxis_devices.yaml (footpedal / task_command_dispatch)
- Hosts the rosbridge_websocket (always on, fixed-port service)
"""

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import (
    EnvironmentVariable,
    LaunchConfiguration,
    PythonExpression,
)
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare
from pathlib import Path

from galaxis_bringup.launch_registry import (
    DATA_COLLECTION_NODE_REGISTRY,
    collect_yaml_keys,
    select_nodes,
)


def _spawn_data_collection_nodes(context, *_args, **_kwargs):
    # Re-resolve paths from this launch context so the runtime file is honored.
    share = Path(FindPackageShare("galaxis_bringup").find("galaxis_bringup"))
    runtime = share / "config" / "runtime_galaxis_devices.yaml"
    paths = (
        [runtime]
        if runtime.exists()
        else [share / "config" / "galaxis_devices.yaml"]
    )
    keys = collect_yaml_keys(paths)
    params = [str(p) for p in paths if p.exists()]
    return select_nodes(DATA_COLLECTION_NODE_REGISTRY, keys, params)


def generate_launch_description():
    pkg_share = Path(FindPackageShare("galaxis_bringup").find("galaxis_bringup"))

    bringup_include = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(pkg_share / "launch" / "galaxis_bringup.launch.py")
        ),
        launch_arguments={
            "robot_variant": LaunchConfiguration("robot_variant"),
        }.items(),
    )

    rosbridge = Node(
        package="rosbridge_server",
        executable="rosbridge_websocket",
        name="rosbridge_websocket",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        parameters=[{"port": 9090}],
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "robot_variant",
                default_value=EnvironmentVariable(
                    "ROBOT_VARIANT", default_value="stationary"
                ),
                description="Robot variant: stationary | portable (selects config/<variant>/...)",
            ),
            DeclareLaunchArgument(
                "joy_remap_topic",
                default_value=PythonExpression(
                    [
                        "'/portable_joy_command' if '",
                        LaunchConfiguration("robot_variant"),
                        "' == 'portable' else '/footpedal_states'",
                    ]
                ),
                description=(
                    "Topic remapped to /joy for task_command_dispatch_node. "
                    "Stationary default: /footpedal_states (footpedal_node). "
                    "Portable default: /portable_joy_command (aggregated by "
                    "portable_joy_command_node from gripper double-click + PICO buttons)."
                ),
            ),
            bringup_include,
            rosbridge,
            OpaqueFunction(function=_spawn_data_collection_nodes),
        ]
    )
