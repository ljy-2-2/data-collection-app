#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PICO_CONNECTOR="${PICO_CONNECTOR:-$HERE/auto_connect_pico.sh}"

[[ -f "$PICO_CONNECTOR" ]] || {
  echo "PICO connector not found: $PICO_CONNECTOR" >&2
  exit 1
}

# Restore the PICO USB reverse ports before starting the two processes. This
# changes only ADB forwarding rules and does not communicate with G2 motion.
bash "$PICO_CONNECTOR" --once

# Reuse the already tested FIFO/process supervisor, but select the dual-arm
# XRFrame emitter and its strictly read-only G2 worker.
export PICO_G2_PIPELINE_MODE="dual-arm-read-and-preview"
export PICO_G2_PHYSICAL_MARKER="False"
export PICO_EMITTER="$HERE/pico_g2_dual_arm_preview.py"
export G2_WORKER="$HERE/run_pico_g2_dual_arm_preview_worker.sh"
export PICO_PY="${PICO_PY:-${HOME}/miniforge3/envs/xrobotoolkit-pybind/bin/python}"

exec bash "$HERE/run_pico_g2_preview.sh" "$@"
