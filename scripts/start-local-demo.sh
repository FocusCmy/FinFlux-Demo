#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${FINFLUX_PORT:-8768}"
HOST="${FINFLUX_HOST:-127.0.0.1}"

command -v python3 >/dev/null 2>&1 || {
  echo "Python 3.10+ is required." >&2
  exit 1
}

test -f "${PROJECT_ROOT}/app/data/real_50x3_v1/manifest.json" || {
  echo "Public source-bound manifest is missing." >&2
  exit 1
}

echo "FinFlux: http://${HOST}:${PORT}/"
exec python3 "${PROJECT_ROOT}/app/app.py" --host "${HOST}" --port "${PORT}"
