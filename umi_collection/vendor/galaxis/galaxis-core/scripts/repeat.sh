#!/usr/bin/env bash
set -euo pipefail

docker compose exec galaxis-core bash -c \
  "source /root/ros_entrypoint.sh > /dev/null 2>&1 && ros2 service call /data_collection/repeat std_srvs/srv/Trigger 2>/dev/null"
