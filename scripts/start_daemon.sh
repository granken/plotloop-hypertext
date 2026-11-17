#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

STATE_DIR="${HYPERTEXT_STATE_DIR:-${STATE_DIR:-${HOME}/.hypertext}}"
LOG_FILE="${STATE_DIR}/daemon.log"
DAEMON_PID_FILE="${STATE_DIR}/daemon.pid"
SUPERVISOR_PID_FILE="${STATE_DIR}/supervisor.pid"
STOP_FILE="${STATE_DIR}/supervisor.stop"

AGENT_SCRIPT="${HYPERTEXT_AGENT_SCRIPT:-${PROJECT_ROOT}/hypertext_agent.py}"
RESPAWN_DELAY_SEC="${HYPERTEXT_RESPAWN_DELAY_SEC:-2}"
MAX_LOG_BYTES=20971520

if [[ -n "${HYPERTEXT_PYTHON_BIN:-}" && -x "${HYPERTEXT_PYTHON_BIN}" ]]; then
    PY_BIN="${HYPERTEXT_PYTHON_BIN}"
elif [[ -x "${PROJECT_ROOT}/.venv/bin/python3" ]]; then
    PY_BIN="${PROJECT_ROOT}/.venv/bin/python3"
elif [[ -x "/opt/homebrew/bin/python3" ]]; then
    PY_BIN="/opt/homebrew/bin/python3"
else
    PY_BIN="$(command -v python3)"
fi

is_pid_alive() {
    local pid="${1:-}"
    [[ -n "${pid}" && "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null
}

read_alive_pid() {
    local pid_file="${1:-}"
    if [[ -f "${pid_file}" ]]; then
        local raw
        raw="$(tr -d '[:space:]' < "${pid_file}" 2>/dev/null || true)"
        if is_pid_alive "${raw}"; then
            echo "${raw}"
            return 0
        fi
    fi
    return 1
}

find_matching_worker_pids() {
    local candidate cmdline
    while IFS= read -r candidate; do
        [[ -z "${candidate}" || "${candidate}" == "$$" ]] && continue
        cmdline="$(ps -p "${candidate}" -o command= 2>/dev/null || true)"
        [[ -z "${cmdline}" ]] && continue
        case "${cmdline}" in
            *"--doctor"*|*"--self-test"*|*"--cli"*|*"--list-users"*|*"--create-invite"*|*"--allow-user"*|*"--revoke-user"*|*"_supervise"*|*"start_daemon.sh"*)
                continue
                ;;
        esac
        echo "${candidate}"
    done < <(pgrep -f "${AGENT_SCRIPT}" 2>/dev/null || true)
}

get_active_worker_pid() {
    local pid=""
    pid="$(read_alive_pid "${DAEMON_PID_FILE}" || true)"
    if [[ -n "${pid}" ]]; then
        echo "${pid}"
        return 0
    fi
    pid="$(find_matching_worker_pids | head -n 1 || true)"
    if [[ -n "${pid}" ]]; then
        echo "${pid}"
        return 0
    fi
    return 1
}

get_file_size_bytes() {
    local f="${1:-}"
    [[ ! -f "${f}" ]] && { echo 0; return 0; }
    if stat -f%z "${f}" >/dev/null 2>&1; then
        stat -f%z "${f}"
    elif stat -c%s "${f}" >/dev/null 2>&1; then
        stat -c%s "${f}"
    else
        wc -c < "${f}" | tr -d '[:space:]'
    fi
}

print_redacted_tail() {
    local f="${1:-}"
    [[ ! -f "${f}" ]] && return 0
    "${PY_BIN}" - "${f}" <<'PYREDACT'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
if not p.is_file():
    raise SystemExit(0)
lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
for line in lines:
    line = re.sub(r"\bsk-[A-Za-z0-9_\-.]{4,}", "sk-***", line)
    line = re.sub(r"\bxn_[A-Za-z0-9_\-.]{4,}", "xn_***", line)
    line = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[redacted-email]", line)
    line = re.sub(r"(?<!\d)(?:\+\d{7,15}|1[3-9]\d{9})(?!\d)", "[redacted-phone]", line)
    print("  " + line)
PYREDACT
}

print_usage() {
    cat <<'EOF'
Usage: ./scripts/start_daemon.sh [start|stop|restart|status|logs|fg] [extra_args...]

Commands:
  start    Launch detached self-healing supervisor + single-instance worker (default)
  stop     Gracefully stop supervisor and any running HyperText daemon worker
  restart  Stop existing daemon and start a fresh supervised worker
  status   Show supervisor, caffeinate, worker uptime, and redacted recent logs
  logs     Follow ~/.hypertext/daemon.log (tail -n 50 -f)
  fg       Run HyperText daemon in foreground (guarded by ~/.hypertext/daemon.lock)
EOF
}

cmd_supervise() {
    trap '' HUP
    mkdir -p "${STATE_DIR}/logs"
    chmod 700 "${STATE_DIR}" 2>/dev/null || true
    cd "${PROJECT_ROOT}"
    echo "$$" > "${SUPERVISOR_PID_FILE}"

    CAFF_PID=""
    if [[ -x "/usr/bin/caffeinate" && "${HYPERTEXT_DISABLE_CAFFEINATE:-0}" != "1" ]]; then
        /usr/bin/caffeinate -is -w "$$" >/dev/null 2>&1 &
        CAFF_PID="$!"
    fi

    CHILD_PID=""
    SLEEP_PID=""

    _cleanup_supervisor() {
        touch "${STOP_FILE}" 2>/dev/null || true
        if [[ -n "${SLEEP_PID}" ]] && is_pid_alive "${SLEEP_PID}"; then
            kill -TERM "${SLEEP_PID}" 2>/dev/null || true
        fi
        if [[ -n "${CHILD_PID}" ]] && is_pid_alive "${CHILD_PID}"; then
            kill -TERM "${CHILD_PID}" 2>/dev/null || true
            wait "${CHILD_PID}" 2>/dev/null || true
        fi
        if [[ -n "${CAFF_PID}" ]] && is_pid_alive "${CAFF_PID}"; then
            kill -TERM "${CAFF_PID}" 2>/dev/null || true
        fi
        if [[ -f "${SUPERVISOR_PID_FILE}" && "$(tr -d '[:space:]' < "${SUPERVISOR_PID_FILE}" 2>/dev/null || true)" == "$$" ]]; then
            rm -f "${SUPERVISOR_PID_FILE}"
        fi
        exit 0
    }
    trap _cleanup_supervisor INT TERM

    while [[ ! -f "${STOP_FILE}" ]]; do
        local_sz="$(get_file_size_bytes "${LOG_FILE}")"
        if [[ "${local_sz}" -gt "${MAX_LOG_BYTES}" ]]; then
            mv -f "${LOG_FILE}" "${LOG_FILE}.1" 2>/dev/null || true
        fi

        printf '%s [Supervisor] Starting HyperText worker (supervisor PID=%s)...\n' \
            "$(date '+%Y-%m-%d %H:%M:%S')" "$$" >> "${LOG_FILE}"

        set +e
        "${PY_BIN}" -u "${AGENT_SCRIPT}" "$@" >> "${LOG_FILE}" 2>&1 </dev/null &
        CHILD_PID="$!"
        wait "${CHILD_PID}"
        EXIT_CODE="$?"
        set -e
        LAST_WRK_PID="${CHILD_PID}"
        CHILD_PID=""

        if [[ -f "${STOP_FILE}" ]]; then
            break
        fi

        printf '%s [Supervisor] Worker PID %s exited (code=%s); respawning in %ss...\n' \
            "$(date '+%Y-%m-%d %H:%M:%S')" "${LAST_WRK_PID}" "${EXIT_CODE}" "${RESPAWN_DELAY_SEC}" >> "${LOG_FILE}"

        set +e
        sleep "${RESPAWN_DELAY_SEC}" &
        SLEEP_PID="$!"
        wait "${SLEEP_PID}"
        set -e
        SLEEP_PID=""
    done

    if [[ -f "${SUPERVISOR_PID_FILE}" && "$(tr -d '[:space:]' < "${SUPERVISOR_PID_FILE}" 2>/dev/null || true)" == "$$" ]]; then
        rm -f "${SUPERVISOR_PID_FILE}"
    fi
}

cmd_start() {
    mkdir -p "${STATE_DIR}/logs"
    chmod 700 "${STATE_DIR}" 2>/dev/null || true

    local sup_pid="" wrk_pid=""
    sup_pid="$(read_alive_pid "${SUPERVISOR_PID_FILE}" || true)"
    wrk_pid="$(get_active_worker_pid || true)"

    if [[ -n "${sup_pid}" && -n "${wrk_pid}" ]]; then
        echo "✅ HyperText daemon already running (supervisor PID=${sup_pid}, worker PID=${wrk_pid})"
        return 0
    fi

    if [[ -z "${sup_pid}" && -n "${wrk_pid}" ]]; then
        echo "ℹ️ Standalone HyperText worker already running (worker PID=${wrk_pid}, no supervisor)."
        echo "   Run './scripts/start_daemon.sh restart' to adopt under the self-healing supervisor."
        return 0
    fi

    if [[ -n "${sup_pid}" && -z "${wrk_pid}" ]]; then
        echo "✅ HyperText supervisor already running (supervisor PID=${sup_pid}); worker is starting..."
        return 0
    fi

    rm -f "${STOP_FILE}"
    nohup "${SCRIPT_PATH}" _supervise "$@" >/dev/null 2>&1 </dev/null &
    local new_sup_pid="$!"
    echo "${new_sup_pid}" > "${SUPERVISOR_PID_FILE}"

    local waited=0
    while [[ "${waited}" -lt 30 ]]; do
        wrk_pid="$(read_alive_pid "${DAEMON_PID_FILE}" || true)"
        if [[ -n "${wrk_pid}" ]]; then
            break
        fi
        sleep 0.1
        waited=$((waited + 1))
    done
    if [[ -z "${wrk_pid}" ]]; then
        wrk_pid="$(get_active_worker_pid || true)"
    fi

    echo "============================================================"
    echo "🚀 HyperText v0.6.3 — Self-Healing Background Daemon Started"
    echo "============================================================"
    echo "  Supervisor PID : ${new_sup_pid}"
    echo "  Worker PID     : ${wrk_pid:-starting}"
    echo "  Log File       : ${LOG_FILE}"
    echo "  Manage         : ./scripts/start_daemon.sh status | logs | restart | stop"
}

cmd_stop() {
    mkdir -p "${STATE_DIR}"
    touch "${STOP_FILE}"

    local pids=()
    local sup_pid="" wrk_pid="" p=""
    sup_pid="$(read_alive_pid "${SUPERVISOR_PID_FILE}" || true)"
    [[ -n "${sup_pid}" ]] && pids+=("${sup_pid}")

    wrk_pid="$(read_alive_pid "${DAEMON_PID_FILE}" || true)"
    [[ -n "${wrk_pid}" ]] && pids+=("${wrk_pid}")

    while IFS= read -r p; do
        [[ -n "${p}" ]] && pids+=("${p}")
    done < <(find_matching_worker_pids)

    if [[ "${#pids[@]}" -gt 0 ]]; then
        for p in "${pids[@]}"; do
            if is_pid_alive "${p}"; then
                kill -TERM "${p}" 2>/dev/null || true
            fi
        done

        local waited=0
        while [[ "${waited}" -lt 50 ]]; do
            local any_alive=0
            for p in "${pids[@]}"; do
                if is_pid_alive "${p}"; then
                    any_alive=1
                    break
                fi
            done
            [[ "${any_alive}" -eq 0 ]] && break
            sleep 0.1
            waited=$((waited + 1))
        done

        for p in "${pids[@]}"; do
            if is_pid_alive "${p}"; then
                kill -KILL "${p}" 2>/dev/null || true
            fi
        done
    fi

    rm -f "${SUPERVISOR_PID_FILE}" "${DAEMON_PID_FILE}"
    echo "🛑 HyperText daemon stopped."
}

cmd_status() {
    local sup_pid="" wrk_pid="" caff_pid="" uptime_str=""
    sup_pid="$(read_alive_pid "${SUPERVISOR_PID_FILE}" || true)"
    wrk_pid="$(get_active_worker_pid || true)"
    if [[ -n "${sup_pid}" ]]; then
        caff_pid="$(pgrep -P "${sup_pid}" caffeinate 2>/dev/null | head -n 1 || true)"
    fi

    echo "============================================================"
    echo "📡 HyperText v0.6.3 — Daemon Status"
    echo "============================================================"
    if [[ -n "${sup_pid}" ]]; then
        echo "  Supervisor : running (PID=${sup_pid})"
    else
        echo "  Supervisor : stopped"
    fi

    if [[ -n "${caff_pid}" ]]; then
        echo "  Caffeinate : active (PID=${caff_pid}, bound to supervisor)"
    else
        echo "  Caffeinate : inactive"
    fi

    if [[ -n "${wrk_pid}" ]]; then
        uptime_str="$(ps -p "${wrk_pid}" -o etime= 2>/dev/null | tr -d '[:space:]' || true)"
        echo "  Worker     : running (PID=${wrk_pid}, uptime=${uptime_str:-unknown})"
    else
        echo "  Worker     : stopped"
    fi
    echo "  State Dir  : ${STATE_DIR}"
    echo "  Log File   : ${LOG_FILE}"

    if [[ -f "${LOG_FILE}" ]]; then
        echo "------------------------------------------------------------"
        echo "Recent Log Tail (redacted):"
        print_redacted_tail "${LOG_FILE}"
    fi

    [[ -n "${wrk_pid}" ]] && return 0 || return 1
}

cmd_logs() {
    mkdir -p "${STATE_DIR}"
    touch "${LOG_FILE}"
    exec tail -n 50 -f "${LOG_FILE}"
}

cmd_fg() {
    mkdir -p "${STATE_DIR}/logs"
    chmod 700 "${STATE_DIR}" 2>/dev/null || true
    cd "${PROJECT_ROOT}"
    exec "${PY_BIN}" -u "${AGENT_SCRIPT}" "$@"
}

ACTION="start"
if [[ "$#" -gt 0 ]]; then
    case "${1}" in
        start|stop|restart|status|logs|fg|_supervise)
            ACTION="${1}"
            shift
            ;;
        -h|--help|help)
            print_usage
            exit 0
            ;;
        -*)
            ACTION="start"
            ;;
        *)
            echo "❌ Unknown subcommand: ${1}" >&2
            print_usage >&2
            exit 2
            ;;
    esac
fi

case "${ACTION}" in
    start)
        cmd_start "$@"
        ;;
    _supervise)
        cmd_supervise "$@"
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_stop
        cmd_start "$@"
        ;;
    status)
        cmd_status
        ;;
    logs)
        cmd_logs
        ;;
    fg)
        cmd_fg "$@"
        ;;
esac
