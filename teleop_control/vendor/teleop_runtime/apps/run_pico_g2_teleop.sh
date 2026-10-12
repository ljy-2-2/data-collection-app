#!/usr/bin/env bash
set -euo pipefail
# Background launchers need separate process groups for complete cleanup, but
# must stay in this terminal session so sudo's terminal-scoped ticket remains valid.
set -m

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="$(cd -- "$HERE/.." && pwd)"
DEFAULT_G2_NETWORK="$(cd -- "$RUNTIME_ROOT/../../g2-dp-release" && pwd)/scripts/g2-network"

PICO_CONNECTOR="${PICO_CONNECTOR:-$HERE/auto_connect_pico.sh}"
XR_PY="${XR_PY:-${HOME}/miniforge3/envs/xrobotoolkit-pybind/bin/python}"
XR_OBSERVER="${XR_OBSERVER:-$HERE/observe_xr.py}"
G2_NETWORK="${G2_NETWORK:-$DEFAULT_G2_NETWORK}"
VIDEO_LAUNCHER="${VIDEO_LAUNCHER:-$HERE/run_g2_video_bridge.sh}"
JOINT_JOG_CONTROL_LAUNCHER="${JOINT_JOG_CONTROL_LAUNCHER:-${CONTROL_LAUNCHER:-$HERE/run_pico_g2_live.sh}}"
DUAL_ARM_CONTROL_LAUNCHER="${DUAL_ARM_CONTROL_LAUNCHER:-$HERE/run_pico_g2_dual_arm_live.sh}"
SUDO="${SUDO:-sudo}"
SESSION_VIDEO_START_GRACE_SECONDS="${SESSION_VIDEO_START_GRACE_SECONDS:-2}"
SESSION_CHILD_STOP_ATTEMPTS="${SESSION_CHILD_STOP_ATTEMPTS:-600}"
SESSION_LOCK_FILE="${SESSION_LOCK_FILE:-/tmp/galaxis-pico-g2-teleop.lock}"

duration=300
fps=60
hz=""
host=127.0.0.1
port=63902
initial_joint=idx21_arm_l_joint1
xr_preflight_seconds=2
enable_motion=false
control_mode=joint-jog
video_enabled=true
head_follow=false
head_option=""

usage() {
  cat <<'EOF'
Usage: bash apps/run_pico_g2_teleop.sh --enable-motion [options]

Options:
  --mode MODE             Control mode: joint-jog or dual-arm (default: joint-jog)
  --video                 Enable video (accepted explicitly; video is on by default)
  --no-video              Disable video for control-path A/B diagnostics
  --head-follow           Add head following to dual-arm (100 Hz only; pending live acceptance)
  --no-head-follow        Disable head following (default)
  --duration SECONDS       Session duration (default: 300)
  --fps HZ                 G2 camera polling ceiling (default: 60)
  --hz HZ                  PICO/control rate (default: joint-jog 50, dual-arm 100)
  --host ADDRESS           VideoHub bind address (default: 127.0.0.1)
  --port PORT              VideoHub TCP port (default: 63902)
  --initial-joint NAME     Initially selected G2 joint (joint-jog only)
  --xr-preflight-seconds N Read-only XRFrame check duration (default: 2)
  --enable-motion          Required acknowledgement for selected live control
  -h, --help               Show this help
EOF
}

while (($#)); do
  case "$1" in
    --duration) duration="${2:?missing value for --duration}"; shift 2 ;;
    --mode) control_mode="${2:?missing value for --mode}"; shift 2 ;;
    --video) video_enabled=true; shift ;;
    --no-video) video_enabled=false; shift ;;
    --head-follow|--no-head-follow)
      [[ -z "$head_option" || "$head_option" == "$1" ]] || { echo "Conflicting head options" >&2; exit 2; }
      head_option="$1"; [[ "$1" == --head-follow ]] && head_follow=true || head_follow=false
      shift ;;
    --fps) fps="${2:?missing value for --fps}"; shift 2 ;;
    --hz) hz="${2:?missing value for --hz}"; shift 2 ;;
    --host) host="${2:?missing value for --host}"; shift 2 ;;
    --port) port="${2:?missing value for --port}"; shift 2 ;;
    --initial-joint) initial_joint="${2:?missing value for --initial-joint}"; shift 2 ;;
    --xr-preflight-seconds)
      xr_preflight_seconds="${2:?missing value for --xr-preflight-seconds}"
      shift 2
      ;;
    --enable-motion) enable_motion=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$control_mode" in
  joint-jog)
    control_launcher="$JOINT_JOG_CONTROL_LAUNCHER"
    hz="${hz:-50}"
    ;;
  dual-arm)
    control_launcher="$DUAL_ARM_CONTROL_LAUNCHER"
    hz="${hz:-100}"
    ;;
  *)
    echo "SESSION_SUPERVISOR_REFUSED: --mode must be joint-jog|dual-arm" >&2
    exit 2
    ;;
esac

if [[ "$enable_motion" != true ]]; then
  echo "SESSION_SUPERVISOR_REFUSED: live G2 control requires --enable-motion" >&2
  exit 2
fi

if [[ "$head_follow" == true ]]; then
  [[ "$control_mode" == dual-arm && "$hz" =~ ^100([.]0+)?$ ]] || {
    echo "SESSION_SUPERVISOR_REFUSED: head following requires --mode dual-arm --hz 100" >&2; exit 2;
  }
fi

[[ -f "$PICO_CONNECTOR" ]] || { echo "PICO connector not found: $PICO_CONNECTOR" >&2; exit 2; }
[[ -x "$XR_PY" ]] || { echo "XR Python not executable: $XR_PY" >&2; exit 2; }
[[ -f "$XR_OBSERVER" ]] || { echo "XR observer not found: $XR_OBSERVER" >&2; exit 2; }
[[ -x "$G2_NETWORK" ]] || { echo "G2 network tool not executable: $G2_NETWORK" >&2; exit 2; }
if [[ "$video_enabled" == true ]]; then
  [[ -f "$VIDEO_LAUNCHER" ]] || { echo "Video launcher not found: $VIDEO_LAUNCHER" >&2; exit 2; }
fi
[[ -f "$control_launcher" ]] || { echo "Control launcher not found: $control_launcher" >&2; exit 2; }
[[ "$SESSION_CHILD_STOP_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || {
  echo "SESSION_CHILD_STOP_ATTEMPTS must be a positive integer" >&2
  exit 2
}

exec 9>"$SESSION_LOCK_FILE"
if ! flock -n 9; then
  echo "SESSION_SUPERVISOR_REFUSED: another PICO + G2 session is already running" >&2
  exit 2
fi

if [[ "$video_enabled" == true ]]; then
  video_status_label=enabled
else
  video_status_label=disabled
fi
echo "SESSION_SUPERVISOR_PRECHECK control_mode=$control_mode control_hz=$hz video=$video_status_label duration=$duration physical_robot_commands_enabled=True"
echo "SESSION_SUPERVISOR_HEAD enabled=$head_follow hot_switch=False physical_combined_acceptance=pending"
if [[ "$control_mode" == joint-jog ]]; then
  echo "Safety: release right Grip or center the stick; hold B for one second; Ctrl-C stops the complete session."
else
  echo "Safety: release both Grips and Triggers; hold B for one second; Ctrl-C stops the complete session."
fi

# The connector may start the persistent ADB server. Close the supervisor lock
# descriptor here as well as for the long-lived watcher below; otherwise ADB can
# inherit descriptor 9 and keep the singleton lock after this shell exits.
if ! bash "$PICO_CONNECTOR" --once 9>&-; then
  echo "SESSION_SUPERVISOR_PREFLIGHT_FAILED: PICO ADB connection or reverse ports are unavailable" >&2
  exit 3
fi

export PYTHONPATH="$RUNTIME_ROOT:$RUNTIME_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
if ! "$XR_PY" "$XR_OBSERVER" \
  --duration "$xr_preflight_seconds" --hz 50 --print-hz 1; then
  echo "SESSION_SUPERVISOR_PREFLIGHT_FAILED: no fresh PICO/XR frames; open the PICO app and enable Send Data" >&2
  exit 3
fi

"$SUDO" -v
if ! "$SUDO" -n "$G2_NETWORK" up; then
  echo "SESSION_SUPERVISOR_PREFLIGHT_FAILED: G2 network setup failed" >&2
  exit 4
fi
export G2_SUDO_PREAUTHENTICATED=true

pico_watcher_pid=""
video_pid=""
control_pid=""

child_is_running() {
  local target_pid="$1"
  [[ -n "$target_pid" ]] && kill -0 "$target_pid" 2>/dev/null
}

child_group_is_running() {
  local group_id="$1"
  [[ -n "$group_id" ]] && kill -0 -- "-$group_id" 2>/dev/null
}

stop_child() {
  local target_pid="$1"
  local label="$2"
  local attempt
  [[ -n "$target_pid" ]] || return 0
  if child_group_is_running "$target_pid"; then
    kill -TERM -- "-$target_pid" 2>/dev/null || true
    for ((attempt = 0; attempt < SESSION_CHILD_STOP_ATTEMPTS; attempt++)); do
      child_group_is_running "$target_pid" || break
      sleep 0.02
    done
    if child_group_is_running "$target_pid"; then
      echo "SESSION_SUPERVISOR_FORCE_STOP child=$label pid=$target_pid" >&2
      kill -KILL -- "-$target_pid" 2>/dev/null || true
    fi
  fi
  wait "$target_pid" 2>/dev/null || true
}

cleanup() {
  local exit_status=$?
  trap - EXIT INT TERM
  stop_child "$control_pid" control
  stop_child "$video_pid" video
  stop_child "$pico_watcher_pid" pico-watcher
  exit "$exit_status"
}

stop_from_signal() {
  local exit_status="$1"
  echo "SESSION_SUPERVISOR_STOP signal_requested=True" >&2
  exit "$exit_status"
}

trap cleanup EXIT
trap 'stop_from_signal 130' INT
trap 'stop_from_signal 143' TERM

# Descriptor 9 belongs only to this supervisor.  Long-lived children must not
# inherit it, otherwise an orphaned watcher/video/control process can keep the
# singleton lock after an abnormal parent exit.
bash "$PICO_CONNECTOR" --watch 9>&- &
pico_watcher_pid=$!

if [[ "$video_enabled" == true ]]; then
  bash "$VIDEO_LAUNCHER" \
    --duration "$duration" \
    --fps "$fps" \
    --host "$host" \
    --port "$port" \
    --no-auto-pico 9>&- &
  video_pid=$!

  sleep "$SESSION_VIDEO_START_GRACE_SECONDS"
  if ! child_is_running "$video_pid"; then
    if wait "$video_pid"; then
      video_status=5
    else
      video_status=$?
    fi
    video_pid=""
    echo "SESSION_SUPERVISOR_CHILD_FAILED: video exited before control start status=$video_status" >&2
    exit "$video_status"
  fi
fi

if ! child_is_running "$pico_watcher_pid"; then
  if wait "$pico_watcher_pid"; then
    watcher_status=5
  else
    watcher_status=$?
  fi
  echo "SESSION_SUPERVISOR_SUMMARY result=failed first_child=pico-watcher status=$watcher_status" >&2
  exit "$watcher_status"
fi

if [[ "$control_mode" == joint-jog ]]; then
  control_args=(
    --mode joint-jog
    --initial-joint "$initial_joint"
    --duration "$duration"
    --hz "$hz"
  )
else
  control_args=(
    --enable-motion
    --duration "$duration"
    --hz "$hz"
    --no-auto-pico
  )
fi

if [[ "$head_follow" == true ]]; then control_args+=(--head-follow); fi
bash "$control_launcher" "${control_args[@]}" 9>&- &
control_pid=$!

if [[ "$video_enabled" == true ]]; then
  video_ready="pid:$video_pid"
else
  video_ready=disabled
fi
echo "SESSION_SUPERVISOR_READY video=$video_ready control_pid=$control_pid pico_watcher_pid=$pico_watcher_pid"

first_finished_pid=""
session_pids=("$pico_watcher_pid" "$control_pid")
if [[ "$video_enabled" == true ]]; then
  session_pids+=("$video_pid")
fi
if wait -n -p first_finished_pid "${session_pids[@]}"; then
  first_status=0
else
  first_status=$?
fi

case "$first_finished_pid" in
  "$control_pid") first_child=control ;;
  "$video_pid") first_child=video ;;
  "$pico_watcher_pid") first_child=pico-watcher ;;
  *) first_child=unknown ;;
esac

if [[ "$first_child" == pico-watcher ]] && ((first_status == 0)); then
  first_status=5
fi

stop_child "$control_pid" control
control_pid=""
stop_child "$video_pid" video
video_pid=""
stop_child "$pico_watcher_pid" pico-watcher
pico_watcher_pid=""
trap - EXIT INT TERM

if ((first_status != 0)); then
  echo "SESSION_SUPERVISOR_SUMMARY result=failed first_child=$first_child status=$first_status" >&2
  exit "$first_status"
fi

echo "SESSION_SUPERVISOR_SUMMARY result=success first_child=$first_child status=0"
