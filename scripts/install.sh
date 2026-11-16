#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${HOME}/.hypertext"

echo "============================================================"
echo "🚀 HyperText v0.5.4 — Automated Installer"
echo "============================================================"
echo "📂 Project root: ${PROJECT_ROOT}"

find_python() {
    local candidates=(
        "/opt/homebrew/bin/python3"
        "/usr/local/bin/python3"
        "$(command -v python3 || true)"
    )
    for py in "${candidates[@]}"; do
        if [[ -n "${py}" && -x "${py}" ]]; then
            if "${py}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                echo "${py}"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON_BIN="$(find_python || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "❌ Python 3.11+ not found. Install via Homebrew first: brew install python@3.12"
    exit 1
fi
echo "✅ Found Python >= 3.11: ${PYTHON_BIN} ($("${PYTHON_BIN}" --version))"

VENV_DIR="${PROJECT_ROOT}/.venv"
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "📦 Creating virtual environment at ${VENV_DIR} ..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

VENV_PY="${VENV_DIR}/bin/python3"
echo "📥 Installing Python dependencies into .venv ..."
"${VENV_PY}" -m pip install --upgrade pip
if ! "${VENV_PY}" -m pip install -e "${PROJECT_ROOT}[dev]"; then
    echo "⚠️ Optional [dev] install failed; falling back to core runtime dependencies..."
    "${VENV_PY}" -m pip install -e "${PROJECT_ROOT}"
fi

mkdir -p "${STATE_DIR}/logs"
chmod 700 "${STATE_DIR}"

ENV_FILE="${PROJECT_ROOT}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${PROJECT_ROOT}/.env.example" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    echo "✅ Created ${ENV_FILE} (mode 600) from .env.example — please edit API keys & ALLOWED_SENDERS."
else
    chmod 600 "${ENV_FILE}" || true
    echo "ℹ️ Preserved existing ${ENV_FILE} without overwriting."
fi

PLIST_TEMPLATE="${PROJECT_ROOT}/deploy/com.plotloop.hypertext-agent.plist.example"
PLIST_LOCAL="${PROJECT_ROOT}/deploy/com.plotloop.hypertext-agent.local.plist"
if [[ -f "${PLIST_TEMPLATE}" ]]; then
    "${VENV_PY}" - "${PLIST_TEMPLATE}" "${PLIST_LOCAL}" "${PROJECT_ROOT}" "${HOME}" <<'PYRENDER'
import pathlib, sys
tpl, out, proj_root, home_dir = sys.argv[1:5]
content = pathlib.Path(tpl).read_text(encoding="utf-8")
content = content.replace("__PROJECT_ROOT__", proj_root).replace("__HOME_DIR__", home_dir)
pathlib.Path(out).write_text(content, encoding="utf-8")
PYRENDER
    echo "✅ Rendered launchd plist -> ${PLIST_LOCAL}"
fi

echo ""
echo "🧪 Running offline subsystem self-test (--self-test) ..."
"${VENV_PY}" "${PROJECT_ROOT}/hypertext_agent.py" --self-test

echo ""
echo "🩺 Running environment & permission diagnostics (--doctor) ..."
"${VENV_PY}" "${PROJECT_ROOT}/hypertext_agent.py" --doctor || true

echo ""
echo "🎉 Installation complete!"
echo "   1. Edit configuration: nano \"${ENV_FILE}\""
echo "   2. Interactive CLI:    \"${PROJECT_ROOT}/scripts/start_cli.sh\""
echo "   3. Run iMessage daemon:\"${PROJECT_ROOT}/scripts/start_daemon.sh\""
