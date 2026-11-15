# Contributing to Instinct-Mac

Thank you for your interest in contributing to **Instinct-Mac**! This document covers local development setup, architectural invariants, and how to extend the agent with new messaging channels (Feishu/Lark, Telegram, BlueBubbles, Sendblue, Discord, Slack, etc.).

---

## 1. Local Development Setup

```bash
git clone https://github.com/your-org/instinct-mac.git
cd instinct-mac
./scripts/install.sh
```

Run the full offline verification suite before opening a Pull Request:

```bash
# 1. Strict syntax & warning check
.venv/bin/python3 -W error -m compileall -f -q instinct_mac_agent.py instinct_mac tests

# 2. Offline 8-subsystem self-test (no API keys or macOS Messages required)
.venv/bin/python3 instinct_mac_agent.py --self-test

# 3. Unit & static audit test suite
.venv/bin/pytest -v
```

> **Cross-Platform CI Note**: All tests and `--self-test` run cleanly on both `macos-latest` and `ubuntu-latest` without requiring live `Messages.app` access or external API keys. Keep third-party SDK imports (`openai`, `browser_use`) lazy or mockable so offline tests remain zero-network.

---

## 2. Implementing a New `MessageBridge` Channel

`InstinctOrchestrator` is decoupled from macOS `Messages.app` via the abstract `MessageBridge` base class in `instinct_mac/bridge_imessage.py`:

```python
from instinct_mac.bridge_imessage import InboundMessage, MessageBridge


class CustomChannelBridge(MessageBridge):
    """Example adapter for Feishu/Lark, Telegram, BlueBubbles, or Sendblue."""

    def poll_new_messages(self) -> list[InboundMessage]:
        # Drain unread events from your webhook queue, WebSocket buffer, or local store.
        # Return normalized InboundMessage objects:
        #   InboundMessage(
        #       rowid=event_seq_id,
        #       guid=f"ext-{event_seq_id}",
        #       sender="user_open_id_or_phone",
        #       text="Help me check flights to Tokyo",
        #       timestamp_unix=time.time(),
        #       image_data_urls=[],
        #       attachment_paths=[],
        #       is_from_me=0,
        #   )
        return []

    async def send_text(self, recipient: str, text: str) -> None:
        # Deliver `text` to `recipient` via your platform SDK / HTTP client.
        ...
```

### Channel Integration Ideas
- **Feishu / Lark Bot**: Receive `im.message.receive_v1` events via long-connection WebSocket (`lark-oapi`), map `sender_id.open_id` to `InboundMessage.sender`, and reply via `im/v1/messages`.
- **Telegram Bot**: Poll `getUpdates` (or webhook), download photo attachments to `~/.instinct_mac/attachments_cache`, and send chunked replies via `sendMessage`.
- **BlueBubbles / Sendblue**: Connect via REST + Webhook to bridge iMessage when running the orchestrator on Linux or a remote cloud host paired with a Mac relay.

Because memory (`StateStore`), per-user Chromium profiles (`BrowserSandbox.profile_dir_for(sender)`), `HumanGate` 2FA suspension, invite codes, and daily quotas are all keyed by `normalize_sender(sender)`, any custom `MessageBridge` automatically inherits multi-user isolation and 2FA pause/resume out of the box!

---

## 3. Hard Technical Guardrails (PR Checklist)

1. **Zero Personal Paths or Secrets**:
   - Never commit `.env` or rendered `deploy/*.local.plist` files.
   - Never hardcode personal home directories in scripts, docs, or tests (always resolve `PROJECT_ROOT` dynamically or use `Path.home()`).
2. **DeepSeek V4.1-Flash & OpenAI Isolation**:
   - Default model is `deepseek-flash`. Do not hardcode deprecated model names.
   - Preserve `reasoning_content` (even `""`) and `server_tool_calls` across assistant tool-call turns in native DeepSeek mode, while stripping all DeepSeek-specific extension fields in OpenAI-compatible mode.
3. **Browser-Use 1.0 API**:
   - Import `from browser_use import Agent as BrowserAgent, Browser, ChatOpenAI, Tools`.
   - Always pass explicit `Browser("chromium", user_data_dir=..., headless=...)` (never call zero-arg `Browser()`, which launches a remote cloud browser).
   - Register custom actions with `@tools.action("description")` on `async def` functions returning `str`.
4. **macOS `chat.db` & AppleScript Safety**:
   - Open `chat.db` strictly read-only via `file:{path}?mode=ro` (`uri=True`).
   - Always pass recipient and message chunks as `argv` parameters to `osascript` (`on run argv`), never via string interpolation.
