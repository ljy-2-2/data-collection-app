#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="$(cd -- "$HERE/.." && pwd)"
DEFAULT_G2_NETWORK="$(cd -- "$RUNTIME_ROOT/../../g2-dp-release" && pwd)/scripts/g2-network"
G2_NETWORK="${G2_NETWORK:-$DEFAULT_G2_NETWORK}"
G2_WORKER="${G2_WORKER:-$HERE/run_g2_camera_worker.sh}"
HOST_PY="${HOST_PY:-${HOME}/miniforge3/envs/g2-teleop-py310/bin/python}"
SUDO="${SUDO:-sudo}"

duration=300
fps=15
host=127.0.0.1
port=63902
settle_seconds=2
timeout_ms=250
max_frames=""
auto_pico=true

while (($#)); do
  case "$1" in
    --duration) duration="${2:?missing value for --duration}"; shift 2 ;;
    --fps) fps="${2:?missing value for --fps}"; shift 2 ;;
    --host) host="${2:?missing value for --host}"; shift 2 ;;
    --port) port="${2:?missing value for --port}"; shift 2 ;;
    --settle-seconds) settle_seconds="${2:?missing value for --settle-seconds}"; shift 2 ;;
    --timeout-ms) timeout_ms="${2:?missing value for --timeout-ms}"; shift 2 ;;
    --max-frames) max_frames="${2:?missing value for --max-frames}"; shift 2 ;;
    --no-auto-pico) auto_pico=false; shift ;;
    -h|--help)
      echo "Usage: bash apps/run_g2_video_bridge.sh [--duration 300] [--fps 15] [--host 127.0.0.1] [--port 63902] [--max-frames N] [--no-auto-pico]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -x "$HOST_PY" ]] || { echo "Host Python not executable: $HOST_PY" >&2; exit 1; }
[[ -x "$G2_NETWORK" ]] || { echo "g2-network tool not executable: $G2_NETWORK" >&2; exit 1; }
[[ -f "$G2_WORKER" ]] || { echo "G2 camera worker not found: $G2_WORKER" >&2; exit 1; }
[[ -f "$HERE/auto_connect_pico.sh" ]] || { echo "PICO auto connector not found: $HERE/auto_connect_pico.sh" >&2; exit 1; }

echo "G2_VIDEO_PIPELINE_PRECHECK camera=kHeadColor transport=unix-to-tcp host=$host port=$port queue=latest-only physical_robot_command_sent=False"
echo "GDK stays inside g2ns; TCP $host:$port serves the selected interface; Ctrl-C stops both sides."

if [[ "${G2_SUDO_PREAUTHENTICATED:-false}" != true ]]; then
  "$SUDO" -v
fi
export PYTHONPATH="$RUNTIME_ROOT:$RUNTIME_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

session_dir="$(mktemp -d /tmp/g2-video-bridge.XXXXXX)"
camera_socket="$session_dir/camera.sock"
bridge_pid=""
worker_pid=""
pico_connector_pid=""

stop_child() {
  local target_pid="$1"
  [[ -n "$target_pid" ]] || return 0
  if kill -0 "$target_pid" 2>/dev/null; then
    kill -TERM "$target_pid" 2>/dev/null || true
  fi
  wait "$target_pid" 2>/dev/null || true
}

cleanup() {
  local exit_status=$?
  trap - EXIT INT TERM
  stop_child "$worker_pid"
  stop_child "$bridge_pid"
  stop_child "$pico_connector_pid"
  if [[ -S "$camera_socket" ]]; then
    rm -f -- "$camera_socket"
  fi
  rmdir -- "$session_dir" 2>/dev/null || true
  exit "$exit_status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$auto_pico" == true ]]; then
  bash "$HERE/auto_connect_pico.sh" --watch &
  pico_connector_pid=$!
fi

"$HOST_PY" "$HERE/video_bridge.py" \
  --socket-path "$camera_socket" --host "$host" --port "$port" &
bridge_pid=$!

for _ in {1..100}; do
  [[ -S "$camera_socket" ]] && break
  if ! kill -0 "$bridge_pid" 2>/dev/null; then
    wait "$bridge_pid"
    exit $?
  fi
  sleep 0.05
done
[[ -S "$camera_socket" ]] || {
  echo "VideoHub did not create its Unix socket" >&2
  exit 1
}

worker_args=(
  --socket-path "$camera_socket"
  --duration "$duration"
  --fps "$fps"
  --settle-seconds "$settle_seconds"
  --timeout-ms "$timeout_ms"
)
if [[ -n "$max_frames" ]]; then
  worker_args+=(--max-frames "$max_frames")
fi

"$SUDO" -n "$G2_NETWORK" run -- \
  bash "$G2_WORKER" "${worker_args[@]}" &
worker_pid=$!

if wait "$worker_pid"; then
  worker_status=0
else
  worker_status=$?
fi
worker_pid=""

stop_child "$bridge_pid"
bridge_pid=""
stop_child "$pico_connector_pid"
pico_connector_pid=""
trap - EXIT INT TERM
if [[ -S "$camera_socket" ]]; then
  rm -f -- "$camera_socket"
fi
rmdir -- "$session_dir" 2>/dev/null || true
exit "$worker_status"
