#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${HOME}/.instinct_mac"
mkdir -p "${STATE_DIR}/logs"
cd "${PROJECT_ROOT}"

if [[ -x "${PROJECT_ROOT}/.venv/bin/python3" ]]; then
    PY_BIN="${PROJECT_ROOT}/.venv/bin/python3"
elif [[ -x "/opt/homebrew/bin/python3" ]]; then
    PY_BIN="/opt/homebrew/bin/python3"
else
    PY_BIN="$(command -v python3)"
fi

exec "${PY_BIN}" "${PROJECT_ROOT}/instinct_mac_agent.py" "$@"
