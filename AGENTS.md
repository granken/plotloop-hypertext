# AGENTS.md — HyperText (`plotloop-hypertext`) Engineering & Collaboration Guide

This guide defines the architecture map, non-negotiable behavioral invariants, security red lines, and pre-commit verification workflow for AI coding agents and human contributors working on **HyperText** (`v0.6.3+`).

---

## 1. Project Overview & Module Map

**HyperText** turns an always-on Mac mini into a local-first, ambient multimodal AI personal agent over native Apple Messages (`iMessage`), requiring zero third-party iPhone apps or BlueBubbles relays.

| File / Module | Responsibility |
| :--- | :--- |
| `hypertext_agent.py` | Executable CLI entrypoint and public API re-export shim |
| `hypertext/cli.py` | CLI flags (`--cli`, `--self-test`, `--doctor`, invite/user admin), `SIGHUP`-immune daemon runner, and single-instance `flock` (`acquire_daemon_lock` on `~/.hypertext/daemon.lock`) |
| `hypertext/config.py` | Stdlib `.env` parser, placeholder API key guard, cross-alias admin canonicalization, and `AgentConfig` |
| `hypertext/bridge_imessage.py` | Read-only `file:.../chat.db?mode=ro` poller, pure-Python `NSAttributedString` `typedstream` decoder, `.heic` -> JPEG `sips` converter, 4-layer Same-Apple-ID Self-Chat anti-loop, and parameterized `on run argv` `osascript` sender |
| `hypertext/orchestrator.py` | `HyperTextOrchestrator` async turn lifecycle, Instant Ack + capped 60s reassurance, non-blocking live interruption (`查进度` / `停`), DeepSeek V4.1-Flash native thinking + OpenAI fallback, and Multi-Agent Arena synthesis |
| `hypertext/store_sqlite.py` | SQLite WAL state store, dual FTS5 hybrid memory (`unicode61` + CJK `trigram` + optional embeddings), crashed tool-call history healer, invite/quota ledger, and proactive task scheduler |
| `hypertext/sandbox_browser.py` | Browser-Use 1.0 local Chromium runner with per-sender `user_<sha256[:16]>` profile isolation, per-user lock + global semaphore, and blocking `HumanGate` 2FA/payment escalation |
| `hypertext/formatter.py` | Zero-Markdown SMS formatter, standalone iOS Rich Link splitter, XSS-safe mobile HTML/PDF renderer, and sensitive attachment path blocker |
| `hypertext/human_gate.py` | Non-blocking `asyncio.Future` 2FA / human confirmation gate unified across admin phone & email aliases |
| `hypertext/xiaona_client.py` | Explicit-only owner-gated cloud `XiaoNa / 小蜜罐` `/ask` relay client with hot-reloaded credentials and `<= 25 MB` media attachment delivery |
| `hypertext/workbuddy_client.py` | Explicit-only owner-gated local `WorkBuddy` (`codebuddy`) headless CLI client with `.nvm` discovery and dual config-dir fallback |
| `hypertext/harnesses_client.py` | Explicit-only owner-gated local `DSH`, `Codex`, and `AGY` headless CLI clients, channel order detector (`detect_explicit_channels`), Magpie/proxy auto-detection, and localhost `NO_PROXY` guard |
| `hypertext/dreamina_client.py` | Explicit-only owner-gated local `Dreamina Canvas` (`dreamina-canvas` / `即梦画布`) client with domestic direct-network proxy stripping, 4-layer canvas state persistence, and `t2i`/`i2i`/`t2v`/`m2v`/`first_last_frame`/`music`/`tts` orchestration |
| `hypertext/selftest.py` | Offline 15-subsystem end-to-end verification suite (`--self-test`, zero network / zero API keys required) |
| `scripts/start_daemon.sh` | Pure-bash self-healing background supervisor (`start` \| `stop` \| `restart` \| `status` \| `logs` \| `fg`) with `caffeinate -is -w $$` and log rotation |
| `scripts/codex_review.sh` | Automated pre-commit / post-commit code review wrapper around `codex exec review` |

---

## 2. Non-Negotiable Architectural Invariants

### 2.1 Conversational & Progress UX Four Principles
1. **Single-Exit Quiet Tools**: Every underlying tool (`BrowserSandbox`, web search, document renderer, and all specialist CLI/HTTP runners) is a **Quiet Tool**. Sub-tools must **never** send unprompted progress bubbles directly to the user; only `HyperTextOrchestrator` emits conversational replies (plus `HumanGate` when blocking on 2FA/payment approval and final deliverable attachments).
2. **Zero Plumbing on Screen**: Never leak step numbers (`Step 12`), visited domains, intermediate `tool_calls` assistant chatter, internal stage identifiers, or raw Markdown syntax (`**bold**`, `### headings`, fenced code blocks, Markdown tables, `[label](url)`) to iMessage. Keep outbound text concise and formatted via `format_for_imessage`, with trailing URLs on standalone lines so iOS renders native Rich Link cards.
3. **One Fast Receipt + Capped `60s` Reassurance**: Tasks exceeding `FAST_ACK_DELAY_SEC` (`1.5s`, `0.4s` for heavy browser/specialist turns) send **at most 1** natural receipt, then anchor strictly on `ack_sent_at` and wait `HYPERTEXT_HEARTBEAT_INTERVAL_SEC` (`60.0s`) before sending **at most 1** (`HYPERTEXT_MAX_HEARTBEAT_PER_TURN=1`) calm, jargon-free reassurance.
4. **Non-Blocking Introspection & Instant Stop**: Status queries (`进度`, `查进度`, `到哪了`, `status`) and stop commands (`停`, `算了`, `stop`) must always execute immediately against `_active_turns` without queueing behind an in-flight turn, and bare 2FA codes must resolve `HumanGate` before locks or quotas.

### 2.2 Explicit-Only & Owner-Only Specialist Gate (6 Channels)
- The 6 specialist channels — **`XiaoNa / 小蜜罐`**, **`WorkBuddy`**, **`DSH`**, **`Codex`**, **`AGY`**, and **`Dreamina / 即梦画布`** — must maintain **`0%` implicit tool exposure** on ordinary user messages.
- Every specialist channel (both direct fast-path and compound LLM tool-call path) is strictly restricted to `_is_admin_sender(sender) == True` (`ALLOWED_SENDERS`). Invited beta members must be blocked immediately with zero subprocess spawns and zero relay requests.
- URL-stripping and strict word-boundary regexes must prevent accidental triggers when users paste ordinary links (e.g., `https://jimeng.jianying.com/...` or `.../dsh/...`) or type unrelated prefixes (`/jmbot`, `/wbbot`, `/codexbot`).

### 2.3 Public-Repo Security Red Lines & Version Synchronization
- **Public Repository**: Never commit, print, or log `.env`, `deploy/*.local.plist`, real API/relay keys, phone numbers, Apple ID emails, private relay domains, or hardcoded host personal paths (always write `~/.hypertext` or `/Users/<username>/...` in docs and use `Path("~/.hypertext").expanduser()` in code).
- **Modern Model & Browser-Use 1.0 Contract**: Default to `deepseek-flash` and Browser-Use `1.0` (`Browser(user_data_dir=..., headless=...)`, `Tools()`, plain string returns from `@tools.action`). Never reintroduce removed `0.x` Browser-Use classes or deprecated model identifiers.
- **Version Sync Checklist**: Whenever bumping the release version, update all version surfaces consistently:
  - `hypertext/__init__.py` (`__version__`), `hypertext_agent.py`, `pyproject.toml`, `hypertext/cli.py` (`_CLI_VERSION`), `hypertext/selftest.py`
  - `scripts/install.sh`, `scripts/check_permissions.sh`, `scripts/start_daemon.sh`, `.env.example`
  - `README.md`, `README_CN.md`, `SECURITY.md`
  - Static version assertions in `tests/test_self_chat_and_multiuser.py` and `tests/test_v041_fixes.py`

### 2.4 Daemon Lifecycle Operations (No `launchd` / `cron`)
- **Do NOT** create, load, or modify macOS `launchd` (`launchctl`) plists or `crontab` entries.
- Always manage the background daemon via `./scripts/start_daemon.sh`:
  - `./scripts/start_daemon.sh status` — inspect supervisor, `caffeinate`, worker PID/uptime, and redacted log tail
  - `./scripts/start_daemon.sh restart` — cleanly stop any running supervisor/worker and start a fresh supervised worker
  - `./scripts/start_daemon.sh stop` — stop supervisor and worker cleanly
  - `./scripts/start_daemon.sh logs` — follow `~/.hypertext/daemon.log`
- Single-instance safety is enforced at the kernel level via non-inheritable `fcntl.flock` on `~/.hypertext/daemon.lock` inside `run_imessage_daemon` only — one-shot CLI commands (`--doctor`, `--self-test`, `--list-users`, `--allow-user`, `--revoke-user`, `--cli`) must remain usable while the daemon is running.

---

## 3. Mandatory Pre-Commit Verification

Run all five checks from the repository root before committing or releasing:

```bash
# 1. Bytecode & warning-clean syntax check
.venv/bin/python3 -W error -m compileall -f -q hypertext_agent.py hypertext tests
bash -n scripts/start_daemon.sh scripts/codex_review.sh scripts/install.sh scripts/check_permissions.sh

# 2. Full unit & static security audit test suite
.venv/bin/pytest -v

# 3. Offline 15-subsystem end-to-end self-test
.venv/bin/python3 hypertext_agent.py --self-test

# 4. Redacted environment & permission diagnostics
.venv/bin/python3 hypertext_agent.py --doctor

# 5. Automated Codex diff review (runs --uncommitted when working tree has changes, else --commit HEAD)
./scripts/codex_review.sh
```
