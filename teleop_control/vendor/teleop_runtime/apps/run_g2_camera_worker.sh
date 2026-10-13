#!/usr/bin/env bash
set -euo pipefail

echo "G2_CAMERA_WORKER_WRAPPER_PRECHECK mode=live-read-only physical_robot_command_sent=False"

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="$(cd -- "$HERE/.." && pwd)"
GDK_APP="${GDK_APP:-${HOME}/.cache/agibot/app}"
GDK_PY="${GDK_PY:-${HOME}/miniforge3/envs/g2-teleop-py310/bin/python}"

[[ -d "$GDK_APP/lib" ]] || {
  echo "GDK library directory not found: $GDK_APP/lib" >&2
  exit 1
}
[[ -d "$GDK_APP/gdk/lib" ]] || {
  echo "GDK Python library directory not found: $GDK_APP/gdk/lib" >&2
  exit 1
}
[[ -x "$GDK_PY" ]] || {
  echo "GDK Python not executable: $GDK_PY" >&2
  exit 1
}
[[ "${LOCATOR_IP:-}" == 10.42.1.* ]] || {
  echo "LOCATOR_IP is not configured for G2; use g2-network run" >&2
  exit 1
}
[[ -n "${AORTA_DISCOVERY_URI:-}" ]] || {
  echo "AORTA_DISCOVERY_URI is not configured; use g2-network run" >&2
  exit 1
}

GDK_LIBRARY_DIRS=("$GDK_APP/lib")
mapfile -t GDK_DISCOVERED_LIBRARY_DIRS < <(
  find "$GDK_APP/lib" -type f -name '*.so' -printf '%h\n' | sort -u
)
GDK_LIBRARY_DIRS+=("${GDK_DISCOVERED_LIBRARY_DIRS[@]}")
GDK_LIBRARY_PATH="$(IFS=:; echo "${GDK_LIBRARY_DIRS[*]}")"

export GLOG_logtostderr=1
export GLOG_minloglevel=2
export DYLOG_DEFAULT_LEVEL=ERROR
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$GDK_LIBRARY_PATH"
export PATH="$PATH:$GDK_APP/bin"
export PYTHONPATH="$GDK_APP/gdk/lib:$RUNTIME_ROOT:$RUNTIME_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export APP_CONF_PATH="$GDK_APP/gdk/config/app_conf.yml"

exec "$GDK_PY" "$HERE/g2_camera_worker.py" "$@"
