#!/usr/bin/env python3
"""Galaxis bringup launch — presence-driven hardware node selection.

Loads the profile-filtered runtime galaxis_devices file when present, with the
unified-config snapshot (config/galaxis_devices.yaml, generated from
galaxis_config.yaml) as a compatibility fallback, and spawns only those
BRINGUP_NODE_REGISTRY entries whose `yaml_key` appears in the merged keyset.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare
from pathlib import Path
import xacro

from galaxis_bringup.launch_registry import (
    BRINGUP_NODE_REGISTRY,
    collect_yaml_keys,
    select_nodes,
)


def variant_yaml_paths(context, share=None):
    """Resolve the runtime galaxis_devices.yaml path (unified-config era).

    The compose runtime bind-mounts the generated runtime_galaxis_devices.yaml
    over the package share directory, so prefer it when present; otherwise fall
    back to the snapshot installed from config/_runtime/single.
    """
    if share is None:
        share = Path(FindPackageShare("galaxis_bringup").find("galaxis_bringup"))
    runtime = share / "config" / "runtime_galaxis_devices.yaml"
    if runtime.exists():
        return (runtime,)
    return (share / "config" / "galaxis_devices.yaml",)


def _spawn_bringup_nodes(context, *_args, **_kwargs):
    paths = variant_yaml_paths(context)
    keys = collect_yaml_keys(paths)
    # ROS 2 multi-params: later files override earlier per-key.
    params = [str(p) for p in paths if p.exists()]
    return select_nodes(BRINGUP_NODE_REGISTRY, keys, params)


def generate_launch_description():
    description_share = Path(
        FindPackageShare("galaxis_description").find("galaxis_description")
    )
    urdf_file = description_share / "urdf" / "galaxis_hand.urdf.xacro"
    rviz_config = description_share / "rviz" / "galaxis_display.rviz"
    robot_description = {
        "robot_description": xacro.process_file(str(urdf_file)).toxml()
    }

    # Structural nodes (always launched, no YAML config requirement)
    robot_state_publisher = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        name="galaxis_robot_state_publisher",
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        parameters=[robot_description],
    )

    rviz = Node(
        package="rviz2",
        executable="rviz2",
        name="galaxis_bringup_rviz",
        arguments=["-d", str(rviz_config)],
        output="screen",
        condition=IfCondition(LaunchConfiguration("use_rviz")),
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
                "use_rviz",
                default_value="false",
                description="Launch RViz2 visualization",
            ),
            robot_state_publisher,
            rviz,
            OpaqueFunction(function=_spawn_bringup_nodes),
        ]
    )
