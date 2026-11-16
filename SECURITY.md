# Security Policy & Architecture Safeguards

**HyperText** (`plotloop-hypertext`) operates at the boundary of personal messaging (`Messages.app` / pluggable `MessageBridge`), persistent memory (SQLite), and live browser automation (`BrowserSandbox`). Security, privacy isolation, and anti-loop/anti-spam safety are first-class design constraints.

---

## 1. Built-in Security Controls

### 1.1 Strictly Read-Only `chat.db` Access
- `MacIMessageBridge` connects to `~/Library/Messages/chat.db` exclusively via SQLite URI `file:{chat_db_path}?mode=ro` (`uri=True`).
- The agent **cannot** modify, delete, or corrupt Apple's `Messages.app` database tables. Outbound messages and staged attachments are handed off to macOS `Messages.app` via official AppleEvents.

### 1.2 Zero AppleScript Injection & Attachment Path Guards
- Outbound iMessages use static parameterized `on run argv` AppleScripts executed via `asyncio.create_subprocess_exec("/usr/bin/osascript", "-e", ...)`.
- User or LLM text is **never** interpolated into AppleScript source code or a shell command string, eliminating quote-breakout and command-injection vectors.
- Sensitive local paths (`.env`, `*.sqlite*`, `*.db`, SSH keys, credentials, browser cookies) are blocked by `is_sensitive_attachment_path` before any attachment staging.

### 1.3 4-Layer Self-Chat Anti-Loop Protection
When `ALLOW_SELF_CHAT=true` is enabled for same-Apple-ID iPhone <-> Mac mini setups:
1. **Outbound Echo Marker (`\u2063`) & SHA-256 Cache**: Every outbound chunk is prefixed with Unicode Invisible Separator `U+2063` and recorded in a 180-second normalized SHA-256 outbound echo cache. Any row (`is_from_me=0` or `1`) containing `U+2063`, matching the cache, or starting with system status prefixes (`🔐 [`, `🌐 [`, `🖥️ `, `⏰ `, `⚠️ `, `🎉 `, `👋 `) is dropped before orchestration.
2. **Admin-Only `is_from_me=1` Guard**: `is_from_me=1` rows are evaluated **only** when `sender_handle` matches `ALLOWED_SENDERS`. Messages you send from your iPhone/Mac to other friends or family are strictly ignored.
3. **Explicit Prefix Gate (`/ai`) with 2FA Exception**: Self-chat messages require `SELF_CHAT_PREFIX` (default `/ai`) so personal notes sent to yourself (`Buy milk`) never trigger the LLM — except when `HumanGate` is actively awaiting a 2FA code from you.
4. **Cross-Alias Twin-Row Mirror Deduplication**: macOS frequently writes both `is_from_me=1` and `is_from_me=0` rows across phone/email aliases for a single self-message; a 15-second fingerprint window ensures at most one turn executes.

### 1.4 Apple iMessage Anti-Spam & Multi-User Quotas
- **Default Closed Access**: Unknown senders are silently ignored unless `ENABLE_INVITE_CODES=true`.
- **24-Hour Anti-Spam Cooldown**: In invite-beta mode (`HT-XXXXXX`), an unauthenticated sender receives **at most one** polite invite prompt per 24 hours (`invite_prompt_ts:<sender>` in `kv_meta`), protecting your Apple ID from automated reply loops or Apple rate-limiting.
- **Daily Resource Quotas**: Invited beta members are bounded by `MEMBER_DAILY_MSG_LIMIT` (default `50/day`) and `MEMBER_DAILY_BROWSER_LIMIT` (default `10/day`).

### 1.5 Per-User Browser Profile & Memory Isolation
- Every sender's SQLite conversation history, FTS5 facts, and scheduled tasks are strictly partitioned by normalized `sender`.
- Every sender receives a dedicated Chromium `user_data_dir` at `~/.hypertext/browser_profiles/user_<sha256[:16]>/` — zero PII (phone numbers or emails) appears in directory names, path traversal is impossible, and cookies/sessions never cross between users.
- A per-sender `asyncio.Lock` prevents Chromium `SingletonLock` corruption while a global `asyncio.Semaphore(BROWSER_MAX_CONCURRENCY)` bounds Mac mini RAM usage.

### 1.6 Human-in-the-Loop Payment & 2FA Guardrail
- Browser tasks enforce system-level instructions prohibiting autonomous submission of payment, transfer, password-change, or irreversible deletion actions; the agent pauses via `ask_user_via_imessage` (`HumanGate`) and awaits explicit human confirmation.

### 1.7 Secret Hygiene
- `.env` and `deploy/*.local.plist` are excluded by `.gitignore`, and `.env` is created with `chmod 600` by `scripts/install.sh`.
- Template placeholder keys (e.g., `sk-替换为...`, `your_...`, or non-ASCII characters) are automatically rejected by `AgentConfig`, and `--doctor` never prints raw secret values.

---

## 2. Supported Versions

| Version | Supported |
| :--- | :--- |
| `0.5.x` | ✅ Active |
| `< 0.5.4` | ⚠️ Upgrade to `0.5.4+` recommended |

---

## 3. Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities (such as authorization bypass, AppleScript injection, or cross-user sandbox escape).

Instead, please report vulnerabilities privately via GitHub Security Advisories on [`https://github.com/granken/plotloop-hypertext/security/advisories`](https://github.com/granken/plotloop-hypertext/security/advisories) (**Security -> Report a vulnerability**) with:
1. Affected version and configuration flags (`ALLOW_SELF_CHAT`, `ENABLE_INVITE_CODES`, etc.).
2. Minimal reproduction steps or unit test.
3. Impact assessment.

Maintainers aim to acknowledge reports within **72 hours** and publish a patched release once verified.
