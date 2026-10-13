#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$HERE/run_pico_g2_head_follow_worker.sh" --enable-motion "$@"
