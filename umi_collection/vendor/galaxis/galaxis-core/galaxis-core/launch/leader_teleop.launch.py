from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import UnlessCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare
from launch_ros.actions import Node


def generate_launch_description():
    # ---- Robot config (single file, all node parameters) ---------------------
    robot_config_default = PathJoinSubstitution([
        FindPackageShare("galaxis_core"), "config", "robot_config.yaml"
    ])
    robot_config_arg = DeclareLaunchArgument(
        'robot_config',
        default_value=robot_config_default,
        description='Path to robot configuration YAML (see robot_config.yaml.sample)'
    )

    # ---- QoS overrides (separate file) --------------------------------------
    qos_overrides_default = PathJoinSubstitution([
        FindPackageShare("galaxis_core"), "config", "qos_overrides.yaml"
    ])
    qos_file_arg = DeclareLaunchArgument(
        'qos_overrides_file',
        default_value=qos_overrides_default,
        description='Path to QoS overrides file (empty = default QoS)'
    )

    # bridge_mode must remain a launch argument because it controls whether
    # task_receiver is spawned (UnlessCondition evaluated at launch time,
    # before YAML parameters are loaded by individual nodes).
    bridge_mode_arg = DeclareLaunchArgument(
        'bridge_mode',
        default_value='false',
        description='Bridge mode: skip task_receiver (sim_bridge provides tasks)'
    )

    robot_config = LaunchConfiguration('robot_config')

    return LaunchDescription([
        robot_config_arg,
        qos_file_arg,
        bridge_mode_arg,
        Node(
            namespace="galaxis",
            package="galaxis_core",
            executable="task_receiver",
            output="log",
            respawn=True,
            name="task_receiver",
            arguments=["--ros-args", "--log-level", "WARN"],
            parameters=[robot_config],
            condition=UnlessCondition(LaunchConfiguration('bridge_mode')),
        ),
        Node(
            package="galaxis_core",
            executable="record_manager",
            output="screen",
            respawn=True,
            name="record_manager",
            parameters=[robot_config, {
                "qos_overrides_file": LaunchConfiguration('qos_overrides_file'),
            }],
        ),
        Node(
            package="galaxis_core",
            executable="metadata_handler",
            output="screen",
            respawn=True,
            name="metadata_handler",
        ),
        Node(
            package="galaxis_core",
            executable="task_sequence_manager",
            output="screen",
            respawn=True,
            name="task_sequence_manager",
            parameters=[robot_config],
        ),
        Node(
            package="galaxis_core",
            executable="recording_gate_node",
            output="screen",
            respawn=True,
            name="recording_gate",
            parameters=[robot_config],
        ),
        Node(
            package="galaxis_core",
            executable="robot_status_node",
            output="screen",
            respawn=True,
            name="robot_status",
            parameters=[robot_config],
        ),
    ])
