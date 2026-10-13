#!/usr/bin/env bash
set -euo pipefail
APP_BUILD_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$APP_BUILD_ROOT/scripts/build_deb.py" "$@"
