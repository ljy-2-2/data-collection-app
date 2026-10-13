#!/usr/bin/env bash
set -euo pipefail

EXEC="docker compose exec galaxis-core bash -c"
SOURCE="source /root/ros_entrypoint.sh > /dev/null 2>&1"

echo "=== State Machine Status ==="
$EXEC "$SOURCE && ros2 topic echo /task_sequence_manager/status --once 2>/dev/null"

echo ""
echo "=== Current Task ==="
$EXEC "$SOURCE && ros2 topic echo /task_receiver/tasks --once 2>/dev/null"
