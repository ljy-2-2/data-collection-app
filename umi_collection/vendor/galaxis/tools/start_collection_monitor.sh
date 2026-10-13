#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
MONITOR_URL="http://localhost:8770/"

# Persist recordings as the operator, even though the core recorder container
# itself needs root. The recorder applies the requested mode 777 only to each
# completed recording tree. Preserve launcher-supplied values.
if [[ -z "${GALAXIS_DATA_UID:-}" ]]; then
  GALAXIS_DATA_UID="${SUDO_UID:-$(id -u)}"
fi
if [[ -z "${GALAXIS_DATA_GID:-}" ]]; then
  GALAXIS_DATA_GID="${SUDO_GID:-$(id -g)}"
fi
export GALAXIS_DATA_UID GALAXIS_DATA_GID

echo "Starting ROS collection services, local MinIO and Galaxis Monitor…"

# Compose reuses this project's existing containers and reports any foreign
# name conflict explicitly. Force-removing every named container on each
# launch was expensive and could interrupt another checkout's recording.
docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d \
  galaxis galaxis_core web_video_server galaxis_monitor

# Wait for the monitor bridge to become healthy before opening a browser.
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://localhost:8770/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "Galaxis collection monitor: $MONITOR_URL"
if [[ "${1:-}" == "--no-open" ]]; then
  exit 0
fi

if ! command -v xdg-open >/dev/null 2>&1; then
  echo "xdg-open is unavailable; open the URL above in a browser." >&2
  exit 0
fi

xdg-open "$MONITOR_URL" >/dev/null 2>&1 &
