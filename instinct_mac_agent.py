#!/usr/bin/env python3
"""Unified executable entrypoint and public re-export shim for Instinct Mac mini Agent."""

from __future__ import annotations

import sys

from instinct_mac import (
    BOT_ECHO_MARKER,
    AgentConfig,
    BrowserSandbox,
    CliBridge,
    HumanGate,
    InboundMessage,
    InstinctOrchestrator,
    MacIMessageBridge,
    MessageBridge,
    StateStore,
    __version__,
    _is_placeholder_key,
    _load_dotenv,
    _simple_fetch_url,
    decode_attributed_body,
    encode_attributed_body,
    normalize_sender,
    proactive_cron_loop,
    sanitize_conversation_history,
)
from instinct_mac.cli import (
    main,
    run_cli_repl,
    run_create_invite,
    run_doctor,
    run_imessage_daemon,
    run_list_users,
)
from instinct_mac.selftest import run_self_test

__all__ = [
    "__version__",
    "BOT_ECHO_MARKER",
    "AgentConfig",
    "_is_placeholder_key",
    "_load_dotenv",
    "normalize_sender",
    "InboundMessage",
    "MessageBridge",
    "MacIMessageBridge",
    "CliBridge",
    "decode_attributed_body",
    "encode_attributed_body",
    "StateStore",
    "sanitize_conversation_history",
    "HumanGate",
    "BrowserSandbox",
    "InstinctOrchestrator",
    "_simple_fetch_url",
    "proactive_cron_loop",
    "run_self_test",
    "run_doctor",
    "run_create_invite",
    "run_list_users",
    "run_cli_repl",
    "run_imessage_daemon",
    "main",
]

if __name__ == "__main__":
    sys.exit(main())
