#!/usr/bin/env bash
set -euo pipefail
app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
replay_python="$app_root/.build-cache/python/cpython-3.12.13-linux-x86_64-gnu/bin/python3.12"
if [[ ! -x "$replay_python" ]]; then replay_python="$app_root/.venv/bin/python"; fi
if [[ ! -x "$replay_python" ]]; then replay_python=python3; fi
export PYTHONPATH="$app_root/.build-cache/platform-test-libs:$app_root/scripts:$app_root/teleop_control/src${PYTHONPATH:+:$PYTHONPATH}"
if [[ "${1:-}" == "--view" ]]; then
  shift
  exec "$replay_python" "$app_root/scripts/umi_replay_sim/view.py" "$@"
fi
exec "$replay_python" "$app_root/scripts/umi_replay_sim/simulate.py" "$@"
