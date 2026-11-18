#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

if [[ -n "${HYPERTEXT_PYTHON_BIN:-}" && -x "${HYPERTEXT_PYTHON_BIN}" ]]; then
    PY_BIN="${HYPERTEXT_PYTHON_BIN}"
elif [[ -x "${PROJECT_ROOT}/.venv/bin/python3" ]]; then
    PY_BIN="${PROJECT_ROOT}/.venv/bin/python3"
elif [[ -x "/opt/homebrew/bin/python3" ]]; then
    PY_BIN="/opt/homebrew/bin/python3"
else
    PY_BIN="$(command -v python3)"
fi

exec "${PY_BIN}" -m hypertext.daemon_control "$@"
