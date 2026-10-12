#!/usr/bin/env bash
set -euo pipefail

ADB="${ADB:-adb}"
mode=once
interval_seconds=2

while (($#)); do
  case "$1" in
    --once) mode=once; shift ;;
    --watch) mode=watch; shift ;;
    --interval-seconds) interval_seconds="${2:?missing value for --interval-seconds}"; shift 2 ;;
    -h|--help)
      echo "Usage: bash apps/auto_connect_pico.sh [--once|--watch] [--interval-seconds 2]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$ADB" == */* ]]; then
  [[ -x "$ADB" ]] || { echo "PICO_ADB_WAIT reason=adb_not_executable path=$ADB" >&2; exit 1; }
else
  command -v "$ADB" >/dev/null 2>&1 || { echo "PICO_ADB_WAIT reason=adb_not_found" >&2; exit 1; }
fi

find_pico_serial() {
  local output line serial state
  local -a candidates=()
  output="$("$ADB" devices -l 2>/dev/null)" || return 1
  while IFS= read -r line; do
    [[ "$line" == *" device "* ]] || continue
    read -r serial state _ <<<"$line"
    [[ "$state" == "device" ]] || continue
    if [[ "$line" == *"product:sparrow"* || "$line" == *"model:A9210"* ]]; then
      candidates+=("$serial")
    fi
  done <<<"$output"

  if [[ -n "${PICO_SERIAL:-}" ]]; then
    for serial in "${candidates[@]}"; do
      [[ "$serial" == "$PICO_SERIAL" ]] && { printf '%s\n' "$serial"; return 0; }
    done
    return 1
  fi
  ((${#candidates[@]} == 1)) || return 1
  printf '%s\n' "${candidates[0]}"
}

connect_once() {
  local serial mappings changed=false
  serial="$(find_pico_serial)" || { echo "PICO_ADB_WAIT reason=no_single_authorized_pico"; return 1; }
  mappings="$("$ADB" -s "$serial" reverse --list 2>/dev/null || true)"
  if [[ "$mappings" != *"tcp:63901 tcp:63901"* ]]; then
    "$ADB" -s "$serial" reverse tcp:63901 tcp:63901 >/dev/null
    changed=true
  fi
  if [[ "$mappings" != *"tcp:63902 tcp:63902"* ]]; then
    "$ADB" -s "$serial" reverse tcp:63902 tcp:63902 >/dev/null
    changed=true
  fi
  echo "PICO_ADB_READY serial=$serial ports=63901,63902 changed=$changed"
}

if [[ "$mode" == "once" ]]; then
  connect_once
  exit $?
fi

trap 'exit 0' INT TERM
last_message=""
while true; do
  message="$(connect_once 2>&1 || true)"
  if [[ "$message" != "$last_message" ]]; then
    printf '%s\n' "$message"
    last_message="$message"
  fi
  sleep "$interval_seconds"
done
