#!/usr/bin/env bash
set -euo pipefail
TCP_APP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TCP_PYTHON=/opt/xiaokai-lab-data-collection/runtime/python/bin/python3.12
if [[ ! -x "$TCP_PYTHON" ]]; then TCP_PYTHON="$TCP_APP_ROOT/.venv/bin/python"; fi
export PYTHONDONTWRITEBYTECODE=1
export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"
exec "$TCP_PYTHON" "$TCP_APP_ROOT/scripts/view_tianji_tcp.py" "$@"
