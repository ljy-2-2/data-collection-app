#!/usr/bin/env bash
set -euo pipefail

if [ $# -ge 1 ] && [ -n "$1" ]; then
  REASON="$1"
  docker compose exec galaxis-core bash -c \
    "source /root/ros_entrypoint.sh > /dev/null 2>&1 && ros2 service call /data_collection/cancel_episode_with_reason airoa_data_msgs/srv/StringTrigger \"{message: '$REASON'}\" 2>/dev/null"
else
  docker compose exec galaxis-core bash -c \
    "source /root/ros_entrypoint.sh > /dev/null 2>&1 && ros2 service call /data_collection/cancel_episode std_srvs/srv/Trigger 2>/dev/null"
fi
