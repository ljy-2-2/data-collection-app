#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="$(cd -- "$HERE/.." && pwd)"
DEFAULT_G2_NETWORK="$(cd -- "$RUNTIME_ROOT/../../g2-dp-release" && pwd)/scripts/g2-network"
G2_NETWORK="${G2_NETWORK:-$DEFAULT_G2_NETWORK}"
G2_WORKER="${G2_WORKER:-$HERE/run_pico_g2_preview_worker.sh}"
PICO_EMITTER="${PICO_EMITTER:-$HERE/pico_g2_preview.py}"
PICO_PY="${PICO_PY:-${HOME}/miniforge3/envs/g2-teleop-py310/bin/python}"
SUDO="${SUDO:-sudo}"
G2_SETTLE_SECONDS="${G2_SETTLE_SECONDS:-2}"

[[ -x "$PICO_PY" ]] || {
  echo "PICO Python not executable: $PICO_PY" >&2
  exit 1
}
[[ -x "$G2_NETWORK" ]] || {
  echo "g2-network tool not executable: $G2_NETWORK" >&2
  exit 1
}
[[ -f "$G2_WORKER" ]] || {
  echo "G2 worker wrapper not found: $G2_WORKER" >&2
  exit 1
}
[[ -f "$PICO_EMITTER" ]] || {
  echo "PICO emitter not found: $PICO_EMITTER" >&2
  exit 1
}

PIPELINE_MODE="${PICO_G2_PIPELINE_MODE:-read-and-preview}"
PHYSICAL_MARKER="${PICO_G2_PHYSICAL_MARKER:-False}"
echo "PICO_G2_PIPELINE_PRECHECK transport=fifo mode=$PIPELINE_MODE physical_robot_commands_enabled=$PHYSICAL_MARKER"
echo "PICO stays on host loopback; GDK stays inside g2ns; Ctrl-C stops both sides."

if [[ "${G2_SUDO_PREAUTHENTICATED:-false}" != true ]]; then
  "$SUDO" -v
fi

export PYTHONPATH="$RUNTIME_ROOT:$RUNTIME_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

session_dir="$(mktemp -d /tmp/pico-g2-preview.XXXXXX)"
intent_fifo="$session_dir/intents.fifo"
mkfifo "$intent_fifo"
emitter_pid=""
worker_pid=""
last_child_status=0

cleanup_files() {
  if [[ -p "$intent_fifo" ]]; then
    rm -f -- "$intent_fifo"
  fi
  if [[ -d "$session_dir" ]]; then
    rmdir -- "$session_dir" 2>/dev/null || true
  fi
}

child_is_running() {
  local target_pid="$1"
  local running_pid
  [[ -n "$target_pid" ]] || return 1
  while read -r running_pid; do
    if [[ "$running_pid" == "$target_pid" ]]; then
      return 0
    fi
  done < <(jobs -pr)
  return 1
}

reap_child() {
  local target_pid="$1"
  if wait "$target_pid"; then
    last_child_status=0
  else
    last_child_status=$?
  fi
}

stop_and_reap_child() {
  local target_pid="$1"
  local signal_scope="${2:-group}"
  local signal_target
  local attempt
  [[ -n "$target_pid" ]] || {
    last_child_status=0
    return
  }
  if [[ "$signal_scope" == "group" ]]; then
    signal_target="-$target_pid"
  else
    signal_target="$target_pid"
  fi
  if child_is_running "$target_pid"; then
    kill -TERM -- "$signal_target" 2>/dev/null || true
    for attempt in {1..200}; do
      child_is_running "$target_pid" || break
      sleep 0.05
    done
    if child_is_running "$target_pid"; then
      echo "PICO_G2_PIPELINE_FORCE_STOP target_pid=$target_pid cleanup_timeout=True" >&2
      kill -KILL -- "$signal_target" 2>/dev/null || true
    fi
  fi
  reap_child "$target_pid"
}

cleanup_on_exit() {
  local exit_status=$?
  trap - EXIT INT TERM
  # PICO owns an isolated process group.  G2's g2-network supervisor receives
  # one direct signal and forwards it to the namespace-side GDK child.
  stop_and_reap_child "$emitter_pid" group
  stop_and_reap_child "$worker_pid" pid
  cleanup_files
  exit "$exit_status"
}

stop_from_signal() {
  local signal_status="$1"
  echo "PICO_G2_PIPELINE_STOP requested=True physical_robot_commands_enabled=$PHYSICAL_MARKER" >&2
  exit "$signal_status"
}

trap cleanup_on_exit EXIT
trap 'stop_from_signal 130' INT
trap 'stop_from_signal 143' TERM

# Keep sudo in this terminal session so the credential accepted by `sudo -v`
# remains valid.  -n prevents an impossible background password prompt.
"$SUDO" -n "$G2_NETWORK" run -- \
  bash "$G2_WORKER" --settle-seconds "$G2_SETTLE_SECONDS" \
  <"$intent_fifo" &
worker_pid=$!

setsid "$PICO_PY" "$PICO_EMITTER" emit --intent-fd 3 "$@" \
  3>"$intent_fifo" &
emitter_pid=$!

first_finished_pid=""
if wait -n -p first_finished_pid "$emitter_pid" "$worker_pid"; then
  first_status=0
else
  first_status=$?
fi

if [[ "$first_finished_pid" == "$emitter_pid" ]]; then
  emitter_status=$first_status
  emitter_pid=""

  for _ in {1..60}; do
    child_is_running "$worker_pid" || break
    sleep 0.05
  done
  if child_is_running "$worker_pid"; then
    stop_and_reap_child "$worker_pid" pid
  else
    reap_child "$worker_pid"
  fi
  worker_status=$last_child_status
  worker_pid=""

  if (( emitter_status != 0 )); then
    final_status=$emitter_status
  else
    final_status=$worker_status
  fi
else
  worker_status=$first_status
  worker_pid=""
  stop_and_reap_child "$emitter_pid" group
  emitter_status=$last_child_status
  emitter_pid=""

  if (( worker_status != 0 )); then
    final_status=$worker_status
  else
    final_status=$emitter_status
  fi
fi

cleanup_files
trap - EXIT INT TERM
exit "$final_status"
