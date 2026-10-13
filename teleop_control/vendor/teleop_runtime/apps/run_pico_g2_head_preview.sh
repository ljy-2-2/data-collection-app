#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PICO_CONNECTOR="${PICO_CONNECTOR:-$HERE/auto_connect_pico.sh}"

[[ -f "$PICO_CONNECTOR" ]] || { echo "PICO connector not found: $PICO_CONNECTOR" >&2; exit 1; }

# Restore only ADB reverse forwarding. Closing fd 9 prevents a long-lived ADB
# daemon from inheriting a parent session lock descriptor.
bash "$PICO_CONNECTOR" --once 9>&-

export PICO_G2_PIPELINE_MODE="head-read-and-preview"
export PICO_G2_PHYSICAL_MARKER="False"
export PICO_EMITTER="$HERE/pico_g2_head_preview.py"
export G2_WORKER="$HERE/run_pico_g2_head_preview_worker.sh"
export PICO_PY="${PICO_PY:-${HOME}/miniforge3/envs/xrobotoolkit-pybind/bin/python}"

exec bash "$HERE/run_pico_g2_preview.sh" "$@"
