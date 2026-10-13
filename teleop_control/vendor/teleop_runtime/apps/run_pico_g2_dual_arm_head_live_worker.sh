#!/usr/bin/env bash
set -euo pipefail
# Internal entry only: one shared worker, no new lock or SDK lifecycle.
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$HERE/run_pico_g2_dual_arm_live_worker.sh" \
  --head-follow --control-hz 100 "$@"
