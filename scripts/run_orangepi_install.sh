#!/usr/bin/env bash
# Keep this installation's terminal open across recoverable build failures.
set -u
OPI_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OPI_STATE="$OPI_ROOT/.build-cache/install-session"
mkdir -p "$OPI_STATE"
rm -f "$OPI_STATE/retry" "$OPI_STATE/stop" "$OPI_STATE/complete"
sudo -v || exit 1
( while sleep 60; do sudo -n -v || exit; done ) &
OPI_AUTH_PID=$!
trap 'kill "$OPI_AUTH_PID" 2>/dev/null || true' EXIT
while true; do
  bash "$OPI_ROOT/scripts/install_orangepi.sh"
  OPI_RESULT=$?
  printf '%s\n' "$OPI_RESULT" > "$OPI_STATE/result"
  if [[ $OPI_RESULT == 0 ]]; then
    touch "$OPI_STATE/complete"
    exit 0
  fi
  echo '构建暂未完成，保留本次终端认证，等待修复后重试（最多 15 分钟）。'
  OPI_DEADLINE=$((SECONDS + 900))
  while [[ ! -f "$OPI_STATE/retry" ]]; do
    if [[ -f "$OPI_STATE/stop" || $SECONDS -ge $OPI_DEADLINE ]]; then
      exit "$OPI_RESULT"
    fi
    sleep 2
  done
  rm -f "$OPI_STATE/retry"
done
