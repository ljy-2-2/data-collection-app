# Base image is built from galaxis-core (see ./galaxis-core). `make docker` and
# `docker compose build` produce it as galaxis-core:latest before this image.
ARG GALAXIS_CORE_IMAGE=galaxis-core:latest
FROM ${GALAXIS_CORE_IMAGE}

# ROS distro for subsequent layers (apt installs, sourcing).
ARG ROS_DISTRO=jazzy
ENV ROS_DISTRO=${ROS_DISTRO}

ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-c"]

# galaxis-specific hardware dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libhidapi-dev \
        v4l-utils && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# galaxis-specific ROS packages
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
        python3-hid \
        python3-serial \
        ros-$ROS_DISTRO-rviz2 \
        ros-$ROS_DISTRO-joint-state-publisher \
        ros-$ROS_DISTRO-joint-state-publisher-gui \
        ros-$ROS_DISTRO-robot-state-publisher \
        ros-$ROS_DISTRO-xacro \
        ros-$ROS_DISTRO-usb-cam \
        ros-$ROS_DISTRO-rosbridge-suite && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy galaxis packages into existing workspace
COPY ./footpedal_ros /root/ros2_ws/src/footpedal_ros
COPY ./galaxis_bringup /root/ros2_ws/src/galaxis_bringup
COPY ./third_party/usb_cam /root/ros2_ws/src/usb_cam
COPY ./galaxis_description /root/ros2_ws/src/galaxis_description
COPY ./galaxis_pico /root/ros2_ws/src/galaxis_pico

# The portable collection profile only uses USB/G2 cameras.  Do not make every
# portable setup depend on a GitHub clone of the optional RealSense wrapper.
# Stationary deployments that actually launch realsense2_camera can opt in via
# ``--build-arg INCLUDE_REALSENSE=1``.
ARG INCLUDE_REALSENSE=0
WORKDIR /root/ros2_ws/src
RUN if [[ "${INCLUDE_REALSENSE}" == "1" ]]; then \
        for attempt in 1 2 3 4 5; do \
            echo "Cloning RealSense ROS wrapper (attempt ${attempt}/5)…"; \
            rm -rf realsense-ros; \
            if git clone --depth 1 --branch 4.56.4 \
                https://github.com/IntelRealSense/realsense-ros realsense-ros; then \
                break; \
            fi; \
            if [[ "${attempt}" == "5" ]]; then exit 1; fi; \
            sleep 5; \
        done; \
    else \
        echo "Skipping optional RealSense ROS wrapper (portable/G2 profile)."; \
    fi

# Install dependencies and build
WORKDIR /root/ros2_ws
RUN apt-get update -y && \
    for i in 1 2 3 4 5 6; do \
        echo "rosdep update 尝试 $i/6 …"; \
        if rosdep update; then break; fi; \
        echo "rosdep update 失败(网络),8 秒后重试…"; \
        sleep 8; \
    done; \
    rosdep install --from-paths src --ignore-src -r -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
RUN source /opt/ros/$ROS_DISTRO/setup.bash && \
    source ./install/setup.bash && \
    colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Release

# entrypoint
COPY ./docker/ros_entrypoint.sh /root/ros_entrypoint.sh
RUN chmod +x /root/ros_entrypoint.sh
RUN echo "source /root/ros_entrypoint.sh" >> /root/.bashrc

ARG GALAXIS_GIT_HASH
ARG GALAXIS_GIT_BRANCH
ENV GALAXIS_GIT_HASH=$GALAXIS_GIT_HASH
ENV GALAXIS_GIT_BRANCH=$GALAXIS_GIT_BRANCH

WORKDIR /root/ros2_ws
CMD ["/bin/bash"]
