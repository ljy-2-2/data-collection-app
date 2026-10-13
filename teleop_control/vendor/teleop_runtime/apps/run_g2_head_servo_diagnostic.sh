#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="$(cd -- "$HERE/.." && pwd)"
GDK_APP="${GDK_APP:-${HOME}/.cache/agibot/app}"
GDK_PY="${GDK_PY:-${HOME}/miniforge3/envs/g2-teleop-py310/bin/python}"
[[ -d "$GDK_APP/gdk/lib" && -d "$GDK_APP/lib" && -x "$GDK_PY" ]] || {
  echo 'GDK environment missing' >&2; exit 2;
}
[[ "${LOCATOR_IP:-}" == 10.42.1.* && -n "${AORTA_DISCOVERY_URI:-}" && -n "${AORTA_DISPATCHER_THREAD_NUM:-}" ]] || {
  echo 'Run inside g2-network run' >&2; exit 2;
}
mapfile -t GDK_EXTRA_DIRS < <(find "$GDK_APP/lib" -type f -name '*.so' -printf '%h\n' | sort -u)
GDK_LIBRARY_DIRS=("$GDK_APP/lib" "${GDK_EXTRA_DIRS[@]}")
GDK_LIBRARY_PATH="$(IFS=:; echo "${GDK_LIBRARY_DIRS[*]}")"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$GDK_LIBRARY_PATH"
export PYTHONPATH="$GDK_APP/gdk/lib:$RUNTIME_ROOT:$RUNTIME_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$PATH:$GDK_APP/bin"
export APP_CONF_PATH="$GDK_APP/gdk/config/app_conf.yml"
export GLOG_logtostderr=1 GLOG_minloglevel=2 DYLOG_DEFAULT_LEVEL=ERROR
exec "$GDK_PY" "$HERE/check_g2_head_servo.py" "$@"
