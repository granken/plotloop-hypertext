# Instinct-Mac (`instinct-mac`)

**English** | [简体中文](./README_CN.md)

[![Version](https://img.shields.io/badge/version-0.4.1-blue.svg)](./pyproject.toml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![CI](https://img.shields.io/badge/CI-macOS%20%26%20Ubuntu-success.svg)](./.github/workflows/ci.yml)
[![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek%20V4.1--Flash-4D6BFE.svg)](https://platform.deepseek.com/)
[![Browser-Use](https://img.shields.io/badge/Browser--Use-1.0-orange.svg)](https://github.com/browser-use/browser-use)

Turn an always-on **Mac mini** into a native **iMessage Multimodal AI Assistant** with persistent hybrid memory, proactive scheduling, **Same-Apple-ID iPhone Self-Chat**, **Multi-User Invite Beta & Per-User Chromium Sandboxes**, and **Human-in-the-Loop 2FA** browser automation.

Zero third-party iPhone app installation required — chat directly from Apple `Messages.app` using text and photos (`.heic`/`.jpg`/`.png`).

---

## Architecture

```mermaid
flowchart TD
    subgraph iPhone["📱 iPhone / External Senders (Messages.app)"]
        SelfUser["Admin iPhone (Same Apple ID)\n'/ai check flights' or bare 2FA '654321'"]
        BetaUser["Invited Beta Friends\nDirect chat + Invite code activation"]
    end

    subgraph Bridge["💬 MacIMessageBridge (Read-Only TCC + Anti-Loop)"]
        ChatDB["~/Library/Messages/chat.db\nSQLite URI mode=ro + NSAttributedString Decoder"]
        AntiLoop["4-Layer Anti-Loop & Twin-Row Dedup\nU+2063 Marker + 180s SHA-256 Cache + 15s Mirror Filter"]
        OsaSend["Parameterized osascript ('on run argv')\n3500-char Chunking + Zero Injection"]
    end

    subgraph Core["🧠 InstinctOrchestrator (Async Event Loop)"]
        AuthQuota["Invite Gate + 24h Anti-Spam Cooldown\nDaily Member Msg & Browser Quotas"]
        Gate2FA["HumanGate (Asyncio Future)\nNon-blocking 2FA / SMS Code Wakeup"]
        LLM["DeepSeek V4.1-Flash ('deepseek-flash', 1M + Vision)\nNative Thinking Round-Trip + Web Search/Read\nAutomatic OpenAI-Compatible Fallback"]
    end

    subgraph Persistence["🗄️ StateStore & Isolated Browser Pool (~/.instinct_mac)"]
        SQLite["SQLite WAL + FTS5 Hybrid Memory\nBM25 (trigram + unicode61) + Optional Embeddings\nProactive Scheduler (One-Shot & Cron/Interval)"]
        Sandboxes["BrowserSandbox (Browser-Use 1.0)\nPer-User Chromium Profile: user_<sha256[:16]>\nPer-Sender Lock + Global Concurrency Semaphore"]
    end

    SelfUser --> ChatDB
    BetaUser --> ChatDB
    ChatDB --> AntiLoop
    AntiLoop --> AuthQuota
    AuthQuota --> Gate2FA
    Gate2FA --> LLM
    LLM <--> SQLite
    LLM <--> Sandboxes
    Sandboxes -. "ask_user_via_imessage (2FA)" .-> Gate2FA
    LLM --> OsaSend
    OsaSend --> SelfUser
    OsaSend --> BetaUser
```

---

## Core Highlights

- **Native macOS iMessage Bridge**: Strictly read-only `file:.../chat.db?mode=ro` polling, pure-Python `typedstream` / `NSAttributedString` binary decoder (`<0x80`, `0x81`, `0x82`, `0x83` length headers + `U+FFFC` stripping), automatic `.heic`/`.heif` -> JPEG conversion via `/usr/bin/sips`, and injection-proof `on run argv` AppleScript delivery.
- **Same-Apple-ID Self-Chat (`v0.3.0`)**: Use a single Apple ID across your iPhone and Mac mini without infinite reply loops or duplicate replies, powered by a **4-Layer Anti-Loop Engine** and **bare 2FA code bypass**.
- **Multi-User Invite Beta & Per-User Isolation (`v0.3.0`)**: Share your Mac mini agent safely with friends or teammates via self-service invite codes (`INST-XXXXXX`), isolated per-user SQLite memories, isolated per-user Chromium profiles (`user_<sha256[:16]>`), bounded concurrency (`BROWSER_MAX_CONCURRENCY`), 24h anti-spam cooldown, and daily usage quotas.
- **DeepSeek V4.1-Flash Native Engine + OpenAI Fallback**: Full `deepseek-flash` (1M context + multimodal vision) support with multi-turn `reasoning_content` & `server_tool_calls` round-trip persistence, native `deepseek_web_search` / `deepseek_read_url` server tools, automatic crashed tool-call history healing, and strict isolation when falling back to OpenAI-compatible endpoints.
- **Browser-Use 1.0 + Non-Blocking 2FA (`HumanGate`)**: Navigates real websites inside persistent local Chromium profiles. When a site asks for an SMS/email verification code, the browser coroutine suspends on an `asyncio.Future`, texts you via iMessage, and resumes the live page the moment you reply.
- **Hybrid Long-Term Memory & Proactive Wakeups**: SQLite WAL store with dual FTS5 indexes (`unicode61` + CJK `trigram`) fused with optional vector embeddings, plus persistent `direct_notify` and `agent_wakeup` scheduled tasks.

---

## 5-Minute Quickstart

### Prerequisites
- A Mac (Apple Silicon Mac mini recommended) signed into **Messages.app** with iMessage enabled.
- **Python 3.11+** (`brew install python@3.12`) and Google Chrome / Chromium installed for browser tasks.
- A **DeepSeek API Key** (`DEEPSEEK_API_KEY`) and/or **OpenAI API Key** (`OPENAI_API_KEY`).

### 1. Install & Initialize

```bash
git clone https://github.com/your-org/instinct-mac.git
cd instinct-mac
./scripts/install.sh
```

`install.sh` automatically locates Python 3.11+, creates `.venv`, installs dependencies, generates `.env` (`chmod 600`) from `.env.example`, renders `deploy/com.research.instinct-agent.local.plist`, and runs `--self-test` + `--doctor`.

### 2. Configure `.env`

```bash
nano .env
```

Set your API key and admin iMessage handle (phone number in E.164 format or Apple ID email):

```ini
PROVIDER_MODE=deepseek
DEEPSEEK_API_KEY=sk-your-real-deepseek-key
DEEPSEEK_MODEL=deepseek-flash

# Admin phone number(s) or Apple ID email(s), comma-separated
ALLOWED_SENDERS=+14155550123,you@icloud.com
```

### 3. Grant macOS Permissions (TCC)

Grant **Full Disk Access** (for read-only `chat.db`) and **Automation -> Messages** (for `osascript` replies) to your terminal / Python binary, then verify:

```bash
./scripts/check_permissions.sh
```

### 4. Test in Interactive CLI or Start Daemon

```bash
# Test memory, tools, and LLM routing locally in terminal
./scripts/start_cli.sh

# Start the live iMessage daemon in foreground
./scripts/start_daemon.sh
```

---

## Same-Apple-ID iPhone Self-Chat Guide

Many users want to message their Mac mini from their own iPhone **without registering a second Apple ID or SIM card**. On macOS, messaging yourself on the same Apple ID creates two classic traps in `chat.db`:
1. Your own message appears as `is_from_me = 1` **and** macOS frequently inserts a mirror `is_from_me = 0` row seconds later.
2. Every AppleScript reply sent back to yourself lands in `chat.db` as both `is_from_me = 1` and `is_from_me = 0`, which would trigger an infinite self-reply loop in naive bots.

Enable Same-Apple-ID Self-Chat in `.env`:

```ini
ALLOWED_SENDERS=+8613800138000
ALLOW_SELF_CHAT=true
SELF_CHAT_PREFIX=/ai
```

### How the 4-Layer Anti-Loop Works
1. **Layer 1 — Invisible Outbound Marker & Hash Cache**: Every bot reply is prefixed with zero-width Unicode Invisible Separator `U+2063` (invisible in iOS/macOS Messages UI) and stored in a 180s normalized SHA-256 outbound cache. Any row (`is_from_me=0` or `1`) with `U+2063`, bot status prefixes (`🔐 [`, `🌐 [`, `🖥️ `, `⏰ `, `⚠️ `, `🎉 `, `👋 `), or matching SHA-256 hash is dropped immediately.
2. **Layer 2 — Admin-Only `is_from_me=1` Scope**: `is_from_me=1` rows are inspected **only** when `sender_handle` is in `ALLOWED_SENDERS`. Normal iMessages you send to friends or colleagues are never read or triggered.
3. **Layer 3 — `/ai` Prefix Gate (with Smart 2FA Bypass)**: Prefix self-chat commands with `/ai` (case-insensitive, e.g. `/ai summarize my schedule` or `/ai` + photo) so personal notes sent to yourself (`Buy milk`) stay untouched. **Exception**: When `HumanGate` is waiting for a 2FA verification code, you can reply with the bare code (`654321`) — no `/ai` needed!
4. **Layer 4 — 15s Twin-Row Mirror Deduplication**: Deduplicates paired `is_from_me=1` + `is_from_me=0` rows by `(sender, normalized_body, image_count)` within a 15-second window so every command runs **at most once**.

---

## Multi-User Beta Invite & Per-User Browser Isolation Guide

Want to let trusted friends or beta testers message your Mac mini's iMessage address? Enable invite mode in `.env`:

```ini
ENABLE_INVITE_CODES=true
# Optional static bootstrap codes (max_uses=999, idempotently seeded on startup)
INVITE_CODES=INST-FRIENDS2026
MEMBER_DAILY_MSG_LIMIT=50
MEMBER_DAILY_BROWSER_LIMIT=10
BROWSER_MAX_CONCURRENCY=3
```

### Managing Invites & Beta Users

```bash
# Generate a 1-time random invite code (e.g. INST-7K9P2M)
.venv/bin/python3 instinct_mac_agent.py --create-invite --invite-note "Alice"

# Generate a custom 5-use code
.venv/bin/python3 instinct_mac_agent.py --create-invite --invite-code INST-TEAM01 --invite-uses 5 --invite-note "Design team"

# Inspect authorized users, today's quota usage, and invite codes
.venv/bin/python3 instinct_mac_agent.py --list-users
```

### Multi-User Isolation & Safety Guarantees
- **Direct Chat for External Members**: External invited users (`is_from_me=0`) chat naturally **without** needing the `/ai` prefix, even while `ALLOW_SELF_CHAT=true` is active for the admin's own handle.
- **Self-Service Activation + 24h Anti-Spam Cooldown**: When an unknown sender texts the bot, if their message contains a valid code (`INST-XXXXXX` or `/activate CODE`), they are activated immediately. Otherwise, the bot sends **at most 1** polite invite prompt per 24 hours (`invite_prompt_ts:<sender>`), preventing spam or Apple ID rate-limits.
- **Per-User Browser Sandbox (`user_<sha256[:16]>`)**: Each user gets a completely isolated Chromium directory under `~/.instinct_mac/browser_profiles/user_<sha256[:16]>/` (zero phone/email PII in folder names) guarded by a per-sender `asyncio.Lock` plus a global `asyncio.Semaphore(BROWSER_MAX_CONCURRENCY)`.
- **Daily Quotas**: Members are capped at `MEMBER_DAILY_MSG_LIMIT` messages/day and `MEMBER_DAILY_BROWSER_LIMIT` browser tasks/day; admins in `ALLOWED_SENDERS` are exempt.

---

## CLI Reference

| Command / Flag | Description |
| :--- | :--- |
| `instinct-agent` (no flags) | Run the 24x7 iMessage polling & proactive scheduler daemon |
| `instinct-agent --cli` | Interactive local terminal REPL (`--sender` optional) |
| `instinct-agent --self-test` | Run all 10 offline subsystem checks (zero API keys / zero network) |
| `instinct-agent --doctor` | Validate Python, SQLite FTS5, keys, TCC permissions, Self-Chat & Invite config |
| `instinct-agent --create-invite` | Generate a new beta invite code (`--invite-code`, `--invite-uses`, `--invite-note`) |
| `instinct-agent --list-users` | Display authorized beta users, today's msg/browser usage, and invite codes |
| `instinct-agent --allow-user HANDLE` | Hot-whitelist a phone/Apple ID directly in SQLite (`--user-note` optional) |
| `instinct-agent --revoke-user HANDLE` | Hot-revoke a user's access immediately without restarting the daemon |
| `instinct-agent --env-file PATH` | Load configuration from a custom `.env` file path |

---

## 24x7 `launchd` Background Service

`./scripts/install.sh` renders `deploy/com.research.instinct-agent.local.plist` with your machine's paths. To enable auto-start at login and crash recovery:

```bash
cp deploy/com.research.instinct-agent.local.plist ~/Library/LaunchAgents/com.research.instinct-agent.plist
launchctl unload ~/Library/LaunchAgents/com.research.instinct-agent.plist 2>/dev/null || true
launchctl load -w ~/Library/LaunchAgents/com.research.instinct-agent.plist

# Tail logs
tail -f ~/.instinct_mac/logs/stdout.log ~/.instinct_mac/logs/stderr.log
```

---

## Extending to Other Channels

Not on macOS, or want to connect **Feishu / Lark, Telegram, BlueBubbles, or Sendblue**? Implement the 2-method `MessageBridge` interface in `instinct_mac/bridge_imessage.py`. See [CONTRIBUTING.md](./CONTRIBUTING.md) for a complete adapter template.

---

## Trademark & Safety Disclaimer

- **Independent Open-Source Project**: `instinct-mac` is an independent, community-built open-source engineering project exploring local-first personal AI agents on macOS. It is **not** affiliated with, endorsed by, or sponsored by Apple Inc., Instinct, DeepSeek, OpenAI, or any other third party. Apple, macOS, iPhone, and iMessage are trademarks of Apple Inc.
- **Safe & Responsible Use**: This software reads your local `~/Library/Messages/chat.db` in strictly read-only mode (`mode=ro`) and sends replies through official macOS AppleEvents (`osascript`) on hardware you own and control. Do **not** use this software for unsolicited bulk messaging, commercial spam, or scraping restricted websites. Users are solely responsible for complying with Apple's Terms of Service, website terms, and applicable laws.
- **Financial & Account Safety**: Never instruct an autonomous browser agent to execute unsupervised financial transfers. Review [SECURITY.md](./SECURITY.md) before exposing multi-user beta access.
