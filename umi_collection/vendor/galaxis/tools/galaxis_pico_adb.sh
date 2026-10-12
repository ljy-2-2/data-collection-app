#!/usr/bin/env bash
set -euo pipefail

serial="${1:-}"
adb_args=()
if [[ -n "$serial" ]]; then
  adb_args=(-s "$serial")
fi

command -v adb >/dev/null 2>&1 || {
  echo "ERROR: adb is not installed or not on PATH." >&2
  exit 1
}

# A retired PC bridge used the same port and may survive a disconnected
# terminal. Match the executable path exactly: never kill an arbitrary port
# owner or another process that merely has a similar name.
legacy_executable="/opt/apps/roboticsservice/RoboticsServiceProcess"
legacy_pids=()
for executable_link in /proc/[0-9]*/exe; do
  executable="$(readlink "$executable_link" 2>/dev/null || true)"
  if [[ "${executable% (deleted)}" == "$legacy_executable" ]]; then
    process_dir="${executable_link%/exe}"
    legacy_pids+=("${process_dir##*/}")
  fi
done
if ((${#legacy_pids[@]})); then
  echo "Stopping retired RoboticsService: ${legacy_pids[*]}"
  for pid in "${legacy_pids[@]}"; do
    if ! kill -TERM "$pid" 2>/dev/null; then
      executable="$(readlink "/proc/$pid/exe" 2>/dev/null || true)"
      if [[ "${executable% (deleted)}" == "$legacy_executable" ]]; then
        echo "ERROR: cannot stop retired RoboticsService: $pid" >&2
        exit 1
      fi
    fi
  done
  for _attempt in {1..30}; do
    remaining=()
    for pid in "${legacy_pids[@]}"; do
      executable="$(readlink "/proc/$pid/exe" 2>/dev/null || true)"
      [[ "${executable% (deleted)}" == "$legacy_executable" ]] && remaining+=("$pid")
    done
    ((${#remaining[@]} == 0)) && break
    sleep 0.1
  done
  if ((${#remaining[@]})); then
    kill -KILL "${remaining[@]}" 2>/dev/null || {
      echo "ERROR: cannot force-stop retired RoboticsService: ${remaining[*]}" >&2
      exit 1
    }
  fi
fi

# Keep this sequence aligned with the known-good manual PICO startup procedure.
# Do not hide its output: it is useful when USB debugging has been revoked.
adb start-server
adb "${adb_args[@]}" wait-for-device
state="$(adb "${adb_args[@]}" get-state 2>/dev/null || true)"
if [[ "$state" != "device" ]]; then
  echo "ERROR: PICO is not an authorized ADB device (state: ${state:-missing})." >&2
  echo "Run 'adb devices -l', unlock the headset, and accept USB debugging." >&2
  exit 1
fi

adb "${adb_args[@]}" reverse --remove tcp:63901 >/dev/null 2>&1 || true
adb "${adb_args[@]}" reverse tcp:63901 tcp:63901

reverse_list="$(adb "${adb_args[@]}" reverse --list)"
printf '%s\n' "$reverse_list"
if ! grep -q 'tcp:63901 tcp:63901' <<<"$reverse_list"; then
  echo "ERROR: ADB reverse verification failed." >&2
  exit 1
fi

echo "Galaxis PICO USB tunnel ready: headset tcp:63901 -> PC tcp:63901"
