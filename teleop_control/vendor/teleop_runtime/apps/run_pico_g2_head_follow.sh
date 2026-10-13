#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
live=false
duration=60
while (($#)); do
  case "$1" in
    --enable-motion) live=true; shift ;;
    --duration) duration="${2:?duration value required}"; shift 2 ;;
    --help|-h) echo 'Usage: run_pico_g2_head_follow.sh [--duration SECONDS] [--enable-motion]'; exit 0 ;;
    *) echo "Unknown argument: $1 (head servo runs at 100 Hz)" >&2; exit 2 ;;
  esac
done
[[ "$duration" =~ ^[0-9]+([.][0-9]+)?$ && "$duration" =~ [1-9] ]] || {
  echo 'duration must be positive seconds' >&2; exit 2;
}
exec 9>"${SESSION_LOCK_FILE:-/tmp/galaxis-pico-g2-teleop.lock}"
flock -n 9 || { echo 'HEAD_FOLLOW_REFUSED: another session is running' >&2; exit 2; }
bash "${PICO_CONNECTOR:-$HERE/auto_connect_pico.sh}" --once 9>&-
export PICO_EMITTER="$HERE/pico_g2_head_follow.py"
export G2_WORKER="$HERE/run_pico_g2_head_follow_worker.sh"
export PICO_G2_PIPELINE_MODE=head-read-and-validate PICO_G2_PHYSICAL_MARKER=False
if [[ "$live" == true ]]; then
  export G2_WORKER="$HERE/run_pico_g2_head_follow_worker_live.sh"
  export PICO_G2_PIPELINE_MODE=head-live-servo PICO_G2_PHYSICAL_MARKER=True
fi
export PICO_PY="${PICO_PY:-${HOME}/miniforge3/envs/xrobotoolkit-pybind/bin/python}"
exec bash "$HERE/run_pico_g2_preview.sh" --duration "$duration" --hz 100
