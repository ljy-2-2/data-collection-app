#!/usr/bin/env bash
set -euo pipefail

echo "G2_READONLY_WRAPPER_PRECHECK mode=live-read-only physical_robot_command_sent=False"

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="$(cd -- "$HERE/.." && pwd)"
GDK_APP="${GDK_APP:-${HOME}/.cache/agibot/app}"
GDK_PY="${GDK_PY:-${HOME}/miniforge3/envs/g2-teleop-py310/bin/python}"

[[ -f "$GDK_APP/env.sh" ]] || {
  echo "GDK environment script not found: $GDK_APP/env.sh" >&2
  exit 1
}
[[ -x "$GDK_PY" ]] || {
  echo "GDK Python not executable: $GDK_PY" >&2
  exit 1
}

export GLOG_logtostderr=1
export GLOG_minloglevel=2
export DYLOG_DEFAULT_LEVEL=ERROR
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$RUNTIME_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# The installed environment script reads positional argument $1.
set +u
source "$GDK_APP/env.sh" "$GDK_APP"
set -u

exec "$GDK_PY" "$HERE/read_g2_state.py" "$@"
