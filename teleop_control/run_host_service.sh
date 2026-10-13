#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"

# This process owns the browser computer's USB/VR hardware. Import only values
# needed to allow the cloud UI origin and upload completed collections; live
# device traffic remains on 127.0.0.1 and never uses the cloud API.
PLATFORM_ENV="$HERE/../.env"
if [[ -f "$PLATFORM_ENV" ]]; then
  while IFS='=' read -r key value; do
    case "$key" in
      AUTH_USERNAME|AUTH_PASSWORD_HASH|AUTH_SECRET_KEY|PUBLIC_HOST|DATA_PLATFORM_API_URL|TELEOP_CORS_ORIGINS)
        value="${value%$'\r'}"
        value="${value#\"}"
        value="${value%\"}"
        value="${value#\'}"
        value="${value%\'}"
        if [[ -z "${!key:-}" ]]; then
          export "$key=$value"
        fi
        ;;
    esac
  done < "$PLATFORM_ENV"
fi

if [[ -z "${DATA_PLATFORM_API_URL:-}" && -n "${PUBLIC_HOST:-}" ]]; then
  export DATA_PLATFORM_API_URL="http://${PUBLIC_HOST}:8000"
fi

if [[ -n "${TELEOP_SERVICE_PYTHON:-}" ]]; then
  SERVICE_PYTHON="$TELEOP_SERVICE_PYTHON"
else
  SERVICE_PYTHON=""
  for candidate in \
    "$HOME/miniforge3/bin/python" \
    "$HOME/miniconda3/bin/python" \
    "$HOME/.conda/bin/python" \
    python \
    python3; do
    if "$candidate" -c 'import fastapi, httpx, serial, uvicorn, yaml' >/dev/null 2>&1; then
      SERVICE_PYTHON="$candidate"
      break
    fi
  done
fi

if [[ -z "$SERVICE_PYTHON" ]]; then
  echo "Set TELEOP_SERVICE_PYTHON to a Python with FastAPI, HTTPX, Uvicorn and PyYAML" >&2
  exit 127
fi

exec "$SERVICE_PYTHON" -m teleop_control.main "$@"
