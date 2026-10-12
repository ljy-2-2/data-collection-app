#!/bin/bash

source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
source "/root/ros2_ws/install/setup.bash"
source "/usr/share/colcon_cd/function/colcon_cd.sh"
export ROS_VERSION=2
export ROS_PYTHON_VERSION=3
export ROS_DISTRO=${ROS_DISTRO:-jazzy}

# RMW middleware selection: set RMW_IMPLEMENTATION=rmw_fastrtps_cpp to use FastDDS, otherwise CycloneDDS
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}

if [[ "$RMW_IMPLEMENTATION" == "rmw_fastrtps_cpp" ]]; then
    export FASTRTPS_DEFAULT_PROFILES_FILE="/root/fastdds_profile.xml"
else
    if [[ -z "$CYCLONEDDS_URI" ]]; then
        export CYCLONEDDS_URI="file:///root/cyclonedds_profile.xml"
    fi
fi

export _colcon_cd_root="/root/ros2_ws/"

# ROS_DOMAIN_ID is passed from docker-compose.yml, default to 0 if not set
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-0}

sysctl -w net.core.rmem_max=2147483647 net.core.rmem_default=2147483647 >/dev/null 2>&1 || true
sysctl -w net.core.wmem_max=2147483647 net.core.wmem_default=2147483647 >/dev/null 2>&1 || true

exec "$@"
