#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

if [[ -x "${PROJECT_ROOT}/.venv/bin/python3" ]]; then
    PY_BIN="${PROJECT_ROOT}/.venv/bin/python3"
elif [[ -x "/opt/homebrew/bin/python3" ]]; then
    PY_BIN="/opt/homebrew/bin/python3"
else
    PY_BIN="$(command -v python3)"
fi

discover_codex_bin() {
    if [[ -n "${CODEX_CLI_PATH:-}" && -x "${CODEX_CLI_PATH}" ]]; then
        echo "${CODEX_CLI_PATH}"
        return 0
    fi

    local which_bin=""
    which_bin="$(command -v codex 2>/dev/null || true)"
    if [[ -n "${which_bin}" && -x "${which_bin}" ]]; then
        echo "${which_bin}"
        return 0
    fi

    if [[ -x "${HOME}/.local/bin/codex" ]]; then
        echo "${HOME}/.local/bin/codex"
        return 0
    fi

    if [[ -d "${HOME}/.nvm/versions/node" ]]; then
        local nvm_cand=""
        while IFS= read -r nvm_cand; do
            if [[ -n "${nvm_cand}" && -x "${nvm_cand}" ]]; then
                echo "${nvm_cand}"
                return 0
            fi
        done < <(ls -1d "${HOME}"/.nvm/versions/node/*/bin/codex 2>/dev/null | sort -V -r || true)
    fi

    local static_cands=(
        "/Applications/ChatGPT.app/Contents/Resources/codex"
        "/opt/homebrew/bin/codex"
        "/usr/local/bin/codex"
    )
    local sc=""
    for sc in "${static_cands[@]}"; do
        if [[ -x "${sc}" ]]; then
            echo "${sc}"
            return 0
        fi
    done
    return 1
}

CODEX_BIN="$(discover_codex_bin || true)"
if [[ -z "${CODEX_BIN}" ]]; then
    echo "❌ OpenAI Codex CLI ('codex') not found. Set CODEX_CLI_PATH or install @openai/codex." >&2
    exit 127
fi

CODEX_DIR="$(dirname "${CODEX_BIN}")"
export PATH="${CODEX_DIR}:${PATH}"

# Guarantee localhost bypass for local services (gRPC sidecars / Magpie / local tools)
MERGE_NO_PROXY="$("${PY_BIN}" - "${NO_PROXY:-}" "${no_proxy:-}" <<'PYNOPROXY'
import sys
raw = f"{sys.argv[1]},{sys.argv[2]}"
items, seen = [], set()
for tok in [p.strip() for p in raw.split(",") if p.strip()] + ["127.0.0.1", "localhost", "::1"]:
    if tok.lower() not in seen:
        seen.add(tok.lower())
        items.append(tok)
print(",".join(items))
PYNOPROXY
)"
export NO_PROXY="${MERGE_NO_PROXY}"
export no_proxy="${MERGE_NO_PROXY}"

if [[ -z "${HTTPS_PROXY:-}" && -z "${https_proxy:-}" ]]; then
    CHOSEN_PROXY="${HYPERTEXT_LOCAL_PROXY:-}"
    if [[ -z "${CHOSEN_PROXY}" ]]; then
        if "${PY_BIN}" -c 'import socket, sys; s = socket.create_connection(("127.0.0.1", 7897), timeout=0.3); s.close()' >/dev/null 2>&1; then
            CHOSEN_PROXY="http://127.0.0.1:7897"
        fi
    fi
    if [[ -n "${CHOSEN_PROXY}" ]]; then
        export http_proxy="${CHOSEN_PROXY}"
        export https_proxy="${CHOSEN_PROXY}"
        export HTTP_PROXY="${CHOSEN_PROXY}"
        export HTTPS_PROXY="${CHOSEN_PROXY}"
    fi
fi

REVIEW_ARGS=()
if [[ "$#" -gt 0 ]]; then
    REVIEW_ARGS=("$@")
elif [[ -n "$(git -C "${PROJECT_ROOT}" status --porcelain 2>/dev/null || true)" ]]; then
    REVIEW_ARGS=("--uncommitted")
else
    REVIEW_ARGS=("--commit" "HEAD")
fi

if [[ "${HYPERTEXT_CODEX_REVIEW_DRY_RUN:-0}" == "1" ]]; then
    printf 'CODEX_BIN=%s\nNO_PROXY=%s\nHTTPS_PROXY=%s\nARGS=%s\n' \
        "${CODEX_BIN}" "${NO_PROXY}" "${HTTPS_PROXY:-}" "${REVIEW_ARGS[*]}"
    exit 0
fi

exec "${CODEX_BIN}" exec review -c 'notify=[]' -c 'mcp_servers={}' "${REVIEW_ARGS[@]}"
