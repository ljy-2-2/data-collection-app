#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UMI_ENV="$APP_ROOT/umi_collection/vendor/galaxis/.env"
if [[ ! -f "$UMI_ENV" ]] && [[ -f "$UMI_ENV.example" ]]; then
  install -m 600 "$UMI_ENV.example" "$UMI_ENV"
fi
APP_PYTHON="$APP_ROOT/.runtime/python/bin/python3.12"
if [[ ! -x "$APP_PYTHON" ]]; then APP_PYTHON="$APP_ROOT/.venv/bin/python"; fi
if [[ ! -x "$APP_PYTHON" ]] || ! "$APP_PYTHON" -c 'import PySide6, fastapi' 2>/dev/null; then
  shopt -s nullglob
  APP_MACHINE=$(uname -m)
  APP_CACHED_PYTHONS=("$APP_ROOT"/.build-cache/python/cpython-3.12.*-linux-"$APP_MACHINE"-gnu/bin/python3.12)
  shopt -u nullglob
  if (( ${#APP_CACHED_PYTHONS[@]} > 0 )); then APP_PYTHON="${APP_CACHED_PYTHONS[-1]}"; fi
fi
if [[ ! -x "$APP_PYTHON" ]] || ! "$APP_PYTHON" -c 'import PySide6, fastapi' 2>/dev/null; then
  echo "首次使用请先运行：$APP_ROOT/install-app.sh" >&2
  exit 1
fi
for APP_ARG in "$@"; do
  if [[ "$APP_ARG" == "--backend-only" ]]; then
    exec "$APP_PYTHON" "$APP_ROOT/scripts/run_app.py" "$@"
  fi
done
exec "$APP_PYTHON" "$APP_ROOT/scripts/desktop_app.py" "$@"
