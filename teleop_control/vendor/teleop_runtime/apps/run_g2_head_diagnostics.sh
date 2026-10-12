#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
GDK_APP="${GDK_APP:-${HOME}/.cache/agibot/app}"
GDK_PY="${GDK_PY:-${HOME}/miniforge3/envs/g2-teleop-py310/bin/python}"
[[ -d "$GDK_APP/gdk/lib" && -d "$GDK_APP/lib" && -x "$GDK_PY" ]] || {
  echo "GDK environment missing" >&2
  exit 2
}
[[ "${LOCATOR_IP:-}" == 10.42.1.* && -n "${AORTA_DISCOVERY_URI:-}" ]] || {
  echo "Run inside g2-network run" >&2
  exit 2
}
mapfile -t GDK_EXTRA_DIRS < <(find "$GDK_APP/lib" -type f -name '*.so' -printf '%h\n' | sort -u)
GDK_LIBRARY_PATH="$(IFS=:; echo "${GDK_EXTRA_DIRS[*]}")"
export LD_LIBRARY_PATH="$GDK_APP/lib:$GDK_LIBRARY_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$GDK_APP/gdk/lib:$HERE/../src${PYTHONPATH:+:$PYTHONPATH}"
export GLOG_logtostderr=1 GLOG_minloglevel=2 DYLOG_DEFAULT_LEVEL=ERROR
echo "G2_HEAD_DIAGNOSTICS_PRECHECK read_only=True physical_robot_command_sent=False"
exec "$GDK_PY" "$HERE/diagnose_g2_head.py" "$@"
