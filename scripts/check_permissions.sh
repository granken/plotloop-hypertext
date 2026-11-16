#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHAT_DB="${HOME}/Library/Messages/chat.db"

echo "============================================================"
echo "🔐 HyperText v0.6.1 — macOS TCC Permission Check"
echo "============================================================"

echo "[1/3] Checking Full Disk Access (read-only SQLite URI on chat.db) ..."
if [[ -r "${CHAT_DB}" ]]; then
    MSG_COUNT="$(/usr/bin/sqlite3 "file:${CHAT_DB}?mode=ro" "SELECT COUNT(*) FROM message;" 2>/dev/null || echo "ERR")"
    if [[ "${MSG_COUNT}" != "ERR" ]]; then
        echo "  ✅ Readable: ${CHAT_DB} (${MSG_COUNT} messages)"
    else
        echo "  ❌ SQLite open failed on ${CHAT_DB}. Grant Full Disk Access to Terminal / Python."
    fi
else
    echo "  ⚠️ ${CHAT_DB} is not readable or Messages.app has not been initialized."
    echo "     Grant Full Disk Access in System Settings -> Privacy & Security -> Full Disk Access."
fi

echo "[2/3] Checking AppleScript Automation permission for Messages.app ..."
if /usr/bin/osascript -e 'tell application "Messages" to get count of services' >/dev/null 2>&1; then
    echo "  ✅ Automation permission granted for Messages.app"
else
    echo "  ⚠️ AppleScript Automation check failed or was not yet authorized."
    echo "     Grant permission in System Settings -> Privacy & Security -> Automation."
fi

echo "[3/3] Running hypertext --doctor ..."
if [[ -x "${PROJECT_ROOT}/.venv/bin/python3" ]]; then
    PY_BIN="${PROJECT_ROOT}/.venv/bin/python3"
elif [[ -x "/opt/homebrew/bin/python3" ]]; then
    PY_BIN="/opt/homebrew/bin/python3"
else
    PY_BIN="$(command -v python3)"
fi

"${PY_BIN}" "${PROJECT_ROOT}/hypertext_agent.py" --doctor
