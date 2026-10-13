#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export G2_WORKER="${G2_WORKER:-$HERE/run_pico_g2_live_worker.sh}"
export PICO_EMITTER="${PICO_EMITTER:-$HERE/pico_g2_live.py}"
export PICO_G2_PIPELINE_MODE="live-direct-joint"
export PICO_G2_PHYSICAL_MARKER="enabled"
exec bash "$HERE/run_pico_g2_preview.sh" "$@"
